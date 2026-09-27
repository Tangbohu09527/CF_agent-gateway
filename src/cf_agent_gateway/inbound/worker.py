from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import case, or_, select, update

from cf_agent_gateway.adapters.wechat.inbound_media import (
    InboundMediaResult,
    MediaReadiness,
    OriginalComparison,
)
from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult, MediaFetchError
from cf_agent_gateway.inbound.models import TERMINAL_STATES, InboundMediaJob
from cf_agent_gateway.inbound.store import authorized_source, aware, source_for
from cf_agent_gateway.message.models import Attachment

logger = logging.getLogger(__name__)
LEASE_SECONDS = 180  # Greater than the HTTP client's maximum 120 second deadline.
MAX_ATTEMPTS = 32


class InboundMediaWorker:
    """Independent durable byte intake. Never calls Hermes or sends a message."""

    def __init__(self, session_factory, client, staging, *, clock=None):
        self.sessions = session_factory
        self.client = client
        self.staging = staging
        self.clock = clock or (lambda: datetime.now(UTC))

    def claim_once(self):
        now = self.clock()
        token = uuid4().hex
        eligible = (
            InboundMediaJob.state.not_in(TERMINAL_STATES),
            InboundMediaJob.next_attempt_at <= now,
            or_(InboundMediaJob.claim_token.is_(None), InboundMediaJob.lease_expires_at <= now),
        )
        with self.sessions() as session:
            job_id = session.scalar(
                select(InboundMediaJob.id)
                .where(*eligible)
                .order_by(
                    InboundMediaJob.next_attempt_at,
                    InboundMediaJob.id,
                )
                .limit(1)
            )
            if job_id is None:
                return None
            changed = session.execute(
                update(InboundMediaJob)
                .where(
                    InboundMediaJob.id == job_id,
                    *eligible,
                )
                .values(
                    state=case(
                        (InboundMediaJob.manifest.is_not(None), "publishing"), else_="fetching"
                    ),
                    claim_token=token,
                    lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                    attempt_count=InboundMediaJob.attempt_count + 1,
                )
            )
            session.commit()
            return (job_id, token) if changed.rowcount == 1 else None

    def _owned(self, session, claim):
        job = session.scalar(
            select(InboundMediaJob)
            .where(
                InboundMediaJob.id == claim[0],
                InboundMediaJob.claim_token == claim[1],
                InboundMediaJob.lease_expires_at > self.clock(),
            )
            .with_for_update()
        )
        if job is None:
            raise MediaFetchError("media_lease_lost")
        return job

    def process_claim(self, claim):
        try:
            with self.sessions() as session:
                job = self._owned(session, claim)
                message, _ = authorized_source(session, job)
                source = source_for(message)
                manifest = job.manifest
                expired = aware(job.deadline_at) <= self.clock() or job.attempt_count > MAX_ATTEMPTS
            bound = None
            if manifest is not None:
                try:
                    data = self.staging.read(
                        manifest["reference"], size=manifest["size"], sha256=manifest["sha256"]
                    )
                    bound = _bound_from_manifest(source.fingerprint, manifest, data)
                except MediaFetchError as error:
                    if error.code != "media_staging_missing":
                        raise
            if bound is None:
                if expired:
                    return self._finish_wait(claim, "timed_out", "media_wait_expired")
                bound = self.client.fetch(source)
                if bound.source_fingerprint != source.fingerprint:
                    raise MediaFetchError("media_source_binding_changed")
                if bound.media.readiness is MediaReadiness.PENDING:
                    return self._finish_wait(claim, "pending", "media_pending")
                if bound.media.readiness is MediaReadiness.UNSUPPORTED:
                    return self._finish_wait(claim, "unsupported", "media_unsupported")
                manifest = _manifest(bound)
                # Commit the publication intent before disk I/O. Recovery can
                # register already published bytes even if upstream is now gone.
                with self.sessions() as session:
                    job = self._owned(session, claim)
                    authorized_source(session, job)
                    job.manifest = manifest
                    job.state = "publishing"
                    session.commit()
            staged = self.staging.publish(bound)
            if staged.reference != manifest["reference"]:
                raise MediaFetchError("media_staging_conflict")
            with self.sessions() as session:
                job = self._owned(session, claim)
                authorized_source(session, job)
                media = bound.media
                attachment = Attachment(
                    message_id=job.message_id,
                    filename=media.filename or "attachment",
                    file_type=media.media_type,
                    mime_type=_mime(media.signature),
                    file_size=media.size,
                    hash=media.sha256,
                    storage_path="inbound-media:" + staged.reference,
                )
                session.add(attachment)
                session.flush()
                job.attachment_id = attachment.id
                job.state = "ready"
                job.last_error_code = None
                job.claim_token = None
                job.lease_expires_at = None
                session.commit()
            return "ready"
        except MediaFetchError as error:
            if error.code == "media_lease_lost":
                return "lease_lost"
            return self._finish_wait(claim, "pending" if error.retryable else "failed", error.code)
        except (KeyError, ValueError, TypeError):
            return self._finish_wait(claim, "failed", "media_registry_conflict")
        # Unexpected/DB/process failures retain the claim/intent for lease recovery.

    def _finish_wait(self, claim, state, error):
        with self.sessions() as session:
            try:
                job = self._owned(session, claim)
            except MediaFetchError:
                return "lease_lost"
            now = self.clock()
            if state == "pending" and (
                aware(job.deadline_at) <= now or job.attempt_count >= MAX_ATTEMPTS
            ):
                state, error = "timed_out", "media_wait_expired"
            job.state = state
            job.last_error_code = error
            job.claim_token = None
            job.lease_expires_at = None
            job.next_attempt_at = min(
                aware(job.deadline_at),
                now + timedelta(seconds=min(60, 2 ** min(job.attempt_count, 6))),
            )
            session.commit()
        logger.info(
            "inbound media state",
            extra={
                "fields": {
                    "media_job_id": claim[0],
                    "state": state,
                    "error_code": error,
                }
            },
        )
        return state

    def run_once(self):
        claim = self.claim_once()
        return self.process_claim(claim) if claim is not None else None


def _manifest(bound):
    result = asdict(bound.media)
    result.pop("data")
    result["readiness"] = bound.media.readiness.value
    result["original_comparison"] = bound.media.original_comparison.value
    result["reference"] = hashlib.sha256(
        (bound.source_fingerprint + ":" + bound.media.sha256).encode("ascii")
    ).hexdigest()
    return result


def _bound_from_manifest(fingerprint, manifest, data):
    fields = {k: v for k, v in manifest.items() if k != "reference"}
    fields["readiness"] = MediaReadiness(fields["readiness"])
    fields["original_comparison"] = OriginalComparison(fields["original_comparison"])
    return BoundMediaResult(fingerprint, InboundMediaResult(data=data, **fields))


def _mime(signature):
    return {"pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png"}.get(
        signature,
        "application/octet-stream",
    )
