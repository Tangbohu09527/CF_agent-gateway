from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.gateway.security import require_api_token
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.store import authorized_source, aware
from cf_agent_gateway.inbound.worker import _mime
from cf_agent_gateway.message.models import Attachment
from cf_agent_gateway.task.model.models import HermesDispatchStatus

router = APIRouter()
DatabaseSession = Annotated[Session, Depends(get_database_session)]


@router.get(
    "/messages/{message_id}/inbound-media",
    tags=["inbound-media"],
    dependencies=[Depends(require_api_token)],
)
def get_message_inbound_status(message_id: int, session: DatabaseSession):
    job_id = session.scalar(
        select(InboundMediaJob.id).where(
            InboundMediaJob.message_id == message_id,
        )
    )
    if job_id is None:
        raise HTTPException(status_code=404, detail="inbound media unavailable")
    return get_inbound_status(job_id, session)


def grant_read(session, job, *, public_base_url):
    """Issue one short lived capability for this exact active dispatch claim."""
    _, record = authorized_source(session, job)
    now = datetime.now(UTC)
    if (
        job.state != "ready"
        or job.manifest is None
        or job.attachment_id is None
        or record.status is not HermesDispatchStatus.RUNNING
        or record.claim_token is None
        or record.lease_expires_at is None
        or aware(record.lease_expires_at) <= now
    ):
        raise MediaFetchError("media_read_not_authorized")
    token = secrets.token_urlsafe(32)
    job.read_token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    job.read_claim_token = record.claim_token
    job.read_expires_at = now + timedelta(seconds=3660)
    manifest = job.manifest
    session.flush()
    return {
        "schema": "cf-inbound-read/v1",
        "message_id": job.message_id,
        "attachment_id": job.attachment_id,
        "thread_id": record.ai_thread_id,
        "enterprise_identity_id": record.enterprise_identity_id,
        "url": public_base_url.rstrip("/") + f"/inbound-media/{job.id}/content",
        "authorization": "Bearer " + token,
        "expires_at": job.read_expires_at.isoformat(),
        "download_policy": {
            "max_attempts": 4,
            "total_timeout_seconds": 30,
            "retryable_status_codes": [503],
            "retry_after_seconds": 1,
        },
        "size": manifest["size"],
        "sha256": manifest["sha256"],
        "mime_type": _mime(manifest["signature"]),
        "filename": manifest["filename"],
        "declared_quality": manifest["declared_quality"],
        "original_comparison": manifest["original_comparison"],
        "formal_archive": False,
    }


def _authorized_manifest(session, job_id, header):
    job = session.get(InboundMediaJob, job_id, populate_existing=True)
    now = datetime.now(UTC)
    if not isinstance(header, str) or not header.startswith("Bearer ") or len(header) > 128:
        raise MediaFetchError("media_read_not_authorized")
    supplied = hashlib.sha256(header[7:].encode("utf-8")).hexdigest()
    if (
        job is None
        or job.state != "ready"
        or not job.read_token_hash
        or not hmac.compare_digest(supplied, job.read_token_hash)
        or job.read_expires_at is None
        or aware(job.read_expires_at) <= now
    ):
        raise MediaFetchError("media_read_not_authorized")
    _, record = authorized_source(session, job)
    if (
        record.status is not HermesDispatchStatus.RUNNING
        or record.claim_token != job.read_claim_token
        or record.lease_expires_at is None
        or aware(record.lease_expires_at) <= now
    ):
        raise MediaFetchError("media_read_not_authorized")
    attachment = session.get(Attachment, job.attachment_id, populate_existing=True)
    manifest = job.manifest
    if (
        attachment is None
        or manifest is None
        or attachment.message_id != job.message_id
        or attachment.hash != manifest["sha256"]
        or attachment.file_size != manifest["size"]
        or attachment.storage_path != "inbound-media:" + manifest["reference"]
    ):
        raise MediaFetchError("media_registry_conflict")
    if (
        manifest["reference"]
        != hashlib.sha256(
            (job.source_fingerprint + ":" + manifest["sha256"]).encode("ascii")
        ).hexdigest()
    ):
        raise MediaFetchError("media_registry_conflict")
    return dict(manifest)


def read_authorized(session, job_id, header, staging):
    manifest = _authorized_manifest(session, job_id, header)
    data = staging.read(manifest["reference"], size=manifest["size"], sha256=manifest["sha256"])
    # Disk access may outlast a claim/lease or an identity change. Revalidate before
    # releasing the buffered bytes; no database lock spans file or network I/O.
    if _authorized_manifest(session, job_id, header) != manifest:
        raise MediaFetchError("media_registry_conflict")
    return data, _mime(manifest["signature"])


@router.get("/inbound-media/{job_id}/content", tags=["inbound-media"])
def get_inbound_content(job_id: int, request: Request, session: DatabaseSession):
    settings = request.app.state.settings.inbound_media
    headers = {"Cache-Control": "no-store"}
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="inbound media unavailable", headers=headers)
    try:
        data, mime = read_authorized(
            session,
            job_id,
            request.headers.get("authorization"),
            InboundMediaStaging(Path(settings.staging_root)),
        )
    except MediaFetchError as error:
        # A ready-but-missing/corrupt file is not a temporary read opportunity.
        # Only explicit storage transients after authorization offer retry.
        if error.retryable and error.code in {"media_staging_busy", "media_staging_io_failed"}:
            raise HTTPException(
                status_code=503,
                detail="attachment read unavailable",
                headers={**headers, "Retry-After": "1"},
            ) from None
        raise HTTPException(
            status_code=403, detail="attachment read unavailable", headers=headers
        ) from None
    except (KeyError, ValueError, TypeError):
        # Do not disclose which identity, file, lease or credential was wrong.
        raise HTTPException(
            status_code=403, detail="attachment read unavailable", headers=headers
        ) from None
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'attachment; filename="attachment"',
        },
    )


@router.get(
    "/inbound-media/{job_id}", tags=["inbound-media"], dependencies=[Depends(require_api_token)]
)
def get_inbound_status(job_id: int, session: DatabaseSession):
    job = session.get(InboundMediaJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="inbound media unavailable")
    return {
        "dispatch_kind": (
            "local_failure_notice"
            if job.state in {"failed", "timed_out", "unsupported"}
            else "hermes_request"
            if job.state == "ready"
            else "blocked_on_media"
        ),
        "id": job.id,
        "message_id": job.message_id,
        "dispatch_id": job.dispatch_id,
        "state": job.state,
        "attempt_count": job.attempt_count,
        "deadline_at": job.deadline_at,
        "next_attempt_at": job.next_attempt_at,
        "last_error_code": job.last_error_code,
        "attachment_id": job.attachment_id,
        "original_comparison": (job.manifest or {}).get("original_comparison"),
        "formal_archive": False,
    }
