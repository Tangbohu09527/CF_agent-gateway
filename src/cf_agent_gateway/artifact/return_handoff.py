"""Current-claim output capabilities and atomic response sealing.

Only a trusted host's explicit return operation uploads bytes. This module never
opens a host path, fetches a URL, or places credentials in a model message.
"""

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.agent_profile import AgentProfile
from cf_agent_gateway.artifact.models import Artifact, ArtifactStatus
from cf_agent_gateway.artifact.repository import ArtifactRepository
from cf_agent_gateway.inbound.store import authorized_dispatch_source, aware
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread
from cf_agent_gateway.workspace.store import WorkspaceStore

SCHEMA = "cf-artifact-return/v1"


class ReturnDenied(ValueError):
    def __init__(self):
        super().__init__("artifact return unavailable")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key(settings):
    key = os.environ.get(settings.signing_key_env, "")
    if len(key) < 32:
        raise ReturnDenied()
    return key.encode("utf-8")


def lock_claim(session, dispatch_id, claim_token):
    result = session.execute(
        update(HermesDispatchRecord)
        .where(
            HermesDispatchRecord.id == dispatch_id,
            HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
            HermesDispatchRecord.claim_token == claim_token,
            HermesDispatchRecord.lease_expires_at > datetime.now(UTC),
        )
        .values(status=HermesDispatchStatus.RUNNING)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ReturnDenied()
    record = session.get(HermesDispatchRecord, dispatch_id, populate_existing=True)
    if aware(record.lease_expires_at) <= datetime.now(UTC):
        raise ReturnDenied()
    return record


def _followup_constraint(session, record):
    # This is server-derived current authorization, never a model-selected ID.
    from cf_agent_gateway.inbound.followup import return_constraint

    try:
        constraint = return_constraint(session, record)
    except MediaFetchError:
        raise ReturnDenied() from None
    if constraint is None or constraint == {"action": "read"}:
        return constraint
    if (
        not isinstance(constraint, dict)
        or set(constraint) != {"action", "attachment_id", "size", "sha256", "kind", "mime_type"}
        or constraint["action"] != "read-return"
        or type(constraint["attachment_id"]) is not int
        or constraint["attachment_id"] <= 0
        or type(constraint["size"]) is not int
        or constraint["size"] <= 0
        or not isinstance(constraint["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", constraint["sha256"])
        or type(constraint["kind"]) is not str
        or type(constraint["mime_type"]) is not str
        or (constraint["kind"], constraint["mime_type"])
        not in {("file", "application/pdf"), ("image", "image/png")}
    ):
        raise ReturnDenied()
    return dict(constraint)


def validate_artifact_constraint(constraint, *, slot, kind, mime_type, size, sha256):
    """An original-file continuation can publish one byte-identical original only."""
    if constraint is not None and (
        constraint["action"] != "read-return"
        or slot != 0
        or (kind, mime_type, size, sha256)
        != (constraint["kind"], constraint["mime_type"], constraint["size"], constraint["sha256"])
    ):
        raise ReturnDenied()


def _followup_binding(session, record, settings, session_id):
    from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
    from cf_agent_gateway.inbound.models import InboundMediaJob

    job = session.scalar(
        select(InboundMediaJob)
        .where(InboundMediaJob.dispatch_id == record.id)
        .execution_options(populate_existing=True)
    )
    if job is None or job.source_job_id is None:
        return None
    bindings = list(
        session.scalars(
            select(InboundHostBinding)
            .where(
                InboundHostBinding.dispatch_id == record.id,
                InboundHostBinding.claim_token_hash == _hash(record.claim_token),
            )
            .execution_options(populate_existing=True)
            .limit(2)
        )
    )
    if len(bindings) != 1:
        raise ReturnDenied()
    binding = bindings[0]
    if (
        binding.job_id != job.id
        or binding.ai_thread_id != record.ai_thread_id
        or binding.profile_reference != settings.profile_reference
        or binding.profile_revision != settings.profile_revision
        or not isinstance(binding.session_id, str)
        or not binding.session_id
        or (session_id is not None and binding.session_id != session_id)
    ):
        raise ReturnDenied()
    # A closed/revoked host lease may legitimately precede result sealing. Bind
    # immutable intent/session identity, not its changing lifecycle state.
    return {"binding_id": binding.id, "session_sha256": _hash(binding.session_id)}


def _snapshot(session, record, settings, *, session_id=None):
    message = session.get(Message, record.message_id, populate_existing=True)
    if message is None or message.is_self:
        raise ReturnDenied()
    if message.source != "wechat":
        from cf_agent_gateway.controlled_test import validate_controlled_message

        try:
            validate_controlled_message(session, message, settings.controlled_test_bindings)
        except ValueError:
            raise ReturnDenied() from None
    authorized_dispatch_source(
        session,
        message_id=record.message_id,
        dispatch_id=record.id,
    )
    # Inbound MediaSource deliberately accepts only raw media types 3/49.
    # Return tasks also start from ordinary text; bind its full persisted source.
    fingerprint = hashlib.sha256(
        _json(
            {
                "source": message.source,
                "account": message.source_account_id,
                "chat": message.conversation_id,
                "conversation_type": message.conversation_type,
                "sender": message.sender_id,
                "local_id": message.source_local_id,
                "server_id": message.source_server_id,
                "raw_type": message.raw_type,
                "occurred_at": aware(message.occurred_at).isoformat(),
                "content_sha256": _hash(message.content),
            }
        )
    ).hexdigest()
    thread = session.get(AIThread, record.ai_thread_id, populate_existing=True)
    if thread.agent_profile_id is not None:
        profile = session.get(AgentProfile, thread.agent_profile_id, populate_existing=True)
        if (profile.external_profile_ref, profile.revision) != (
            settings.profile_reference,
            settings.profile_revision,
        ):
            raise ReturnDenied()
    else:
        binding = WorkspaceStore(session).get_source_binding(
            platform=message.source,
            account_id=message.source_account_id,
            physical_conversation_id=message.conversation_id,
            sender_id=message.sender_id,
        )
        if binding is None or binding.ai_thread_id != record.ai_thread_id:
            raise ReturnDenied()
    snapshot = {
        "dispatch_id": record.id,
        "claim": _hash(record.claim_token),
        "message_id": record.message_id,
        "identity_id": record.enterprise_identity_id,
        "workspace_id": record.workspace_id,
        "thread_id": record.ai_thread_id,
        "thread_key": thread.thread_key,
        "thread_policy": thread.thread_policy,
        "agent_profile_id": thread.agent_profile_id,
        "source": fingerprint,
        "account_id": message.source_account_id,
        "conversation_id": message.conversation_id,
        "conversation_type": message.conversation_type,
        "profile": settings.profile_reference,
        "revision": settings.profile_revision,
        "expires": int(
            (aware(record.claimed_at) + timedelta(seconds=settings.ttl_seconds)).timestamp()
        ),
        "max_bytes": settings.max_bytes,
        "max_artifacts": settings.max_artifacts,
    }
    constraint = _followup_constraint(session, record)
    if constraint is not None:
        snapshot["original_attachment_constraint"] = constraint
        snapshot["max_artifacts"] = 1
        binding = _followup_binding(session, record, settings, session_id)
        if binding is not None:
            snapshot["original_attachment_binding"] = binding
    return snapshot


def _prefix(record, claim_token):
    return f"return:{record.id}:{_hash(claim_token)}:"


def response_id_for(record, snapshot):
    return _prefix(record, record.claim_token) + hashlib.sha256(_json(snapshot)).hexdigest()


def slot_id(response_id, slot):
    return str(uuid5(NAMESPACE_URL, f"{SCHEMA}/{response_id}/{slot}"))


def issue_context(session, record, settings, *, session_id):
    if not settings.enabled or not settings.host_contract_confirmed:
        raise ReturnDenied()
    record = lock_claim(session, record.id, record.claim_token)
    snapshot = _snapshot(session, record, settings, session_id=session_id)
    if (
        snapshot.get("original_attachment_constraint") == {"action": "read"}
        or snapshot["expires"] <= datetime.now(UTC).timestamp()
    ):
        raise ReturnDenied()
    payload = base64.urlsafe_b64encode(
        _json(
            {
                "schema": SCHEMA,
                "session_id": session_id,
                **snapshot,
            }
        )
    ).rstrip(b"=")
    signature = hmac.new(_key(settings), payload, hashlib.sha256).hexdigest()
    return {
        "url": settings.public_base_url.rstrip("/") + f"/internal/hermes/returns/{record.id}",
        "authorization": "Bearer " + payload.decode("ascii") + "." + signature,
    }


def authorize_snapshot(session, settings, dispatch_id, authorization):
    """Verify the signed snapshot, then serialize upload with dispatch completion."""
    if not settings.enabled or not isinstance(authorization, str) or len(authorization) > 4096:
        raise ReturnDenied()
    try:
        scheme, token = authorization.split(" ", 1)
        payload, signature = token.split(".")
        expected = hmac.new(_key(settings), payload.encode("ascii"), hashlib.sha256).hexdigest()
        if scheme != "Bearer" or not hmac.compare_digest(signature, expected):
            raise ReturnDenied()
        claims = json.loads(
            base64.b64decode(
                payload + "=" * (-len(payload) % 4),
                altchars=b"-_",
                validate=True,
            )
        )
        if claims["schema"] != SCHEMA or claims["dispatch_id"] != dispatch_id:
            raise ReturnDenied()
        record = session.get(HermesDispatchRecord, dispatch_id, populate_existing=True)
        if record is None or not record.claim_token or _hash(record.claim_token) != claims["claim"]:
            raise ReturnDenied()
        record = lock_claim(session, dispatch_id, record.claim_token)
        snapshot = _snapshot(session, record, settings, session_id=claims["session_id"])
        if claims != {"schema": SCHEMA, "session_id": claims["session_id"], **snapshot}:
            raise ReturnDenied()
        if (
            snapshot.get("original_attachment_constraint") == {"action": "read"}
            or snapshot["expires"] <= datetime.now(UTC).timestamp()
        ):
            raise ReturnDenied()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ReturnDenied() from None
    return record, snapshot


def authorize(session, settings, dispatch_id, authorization):
    record, snapshot = authorize_snapshot(session, settings, dispatch_id, authorization)
    return record, response_id_for(record, snapshot)


def seal_returns(session, record_id, claim_token, outcome, *, settings, storage_root):
    """Merge only this claim's explicitly uploaded artifacts under the completion lock.

    The caller commits the resulting dispatch response in this SAME transaction.
    No upload can be acknowledged after the success CAS clears the claim.
    """
    from cf_agent_gateway.hermes.errors import HermesResponseError
    from cf_agent_gateway.hermes.models import ArtifactRefPart, ResponseEnvelope

    try:
        if not settings.enabled:
            record = session.get(HermesDispatchRecord, record_id, populate_existing=True)
            constraint = _followup_constraint(session, record)
            if constraint is not None:
                record = lock_claim(session, record_id, claim_token)
                candidate = session.scalar(
                    select(Artifact)
                    .where(Artifact.response_id.startswith(_prefix(record, claim_token)))
                    .limit(1)
                )
                if candidate is not None or any(
                    part.type == "artifact_ref" for part in outcome.parts
                ):
                    raise ReturnDenied()
            return outcome
        record = lock_claim(session, record_id, claim_token)
        snapshot = _snapshot(session, record, settings)
        response_id = response_id_for(record, snapshot)
        candidates = list(
            session.scalars(
                select(Artifact).where(
                    Artifact.response_id.startswith(_prefix(record, claim_token)),
                )
            )
        )
        constraint = snapshot.get("original_attachment_constraint")
        if constraint == {"action": "read"} and candidates:
            raise ReturnDenied()
        expected_slots = [slot_id(response_id, slot) for slot in range(snapshot["max_artifacts"])]
        if any(
            a.response_id != response_id
            or a.artifact_id not in expected_slots
            or a.status is not ArtifactStatus.READY
            for a in candidates
        ):
            raise ReturnDenied()
        for artifact in candidates:
            validate_artifact_constraint(
                constraint,
                slot=expected_slots.index(artifact.artifact_id),
                kind=artifact.kind.value,
                mime_type=artifact.mime_type,
                size=artifact.size,
                sha256=artifact.sha256,
            )
        artifact_ids = [
            aid for aid in expected_slots if any(a.artifact_id == aid for a in candidates)
        ]
        supplied = outcome.response.artifact_ids if outcome.response else ()
        if supplied and (
            outcome.response.response_id != response_id or tuple(artifact_ids) != supplied
        ):
            raise ReturnDenied()
        if not candidates:
            return outcome
        repository = ArtifactRepository(
            session, storage_root, max_artifact_bytes=settings.max_bytes
        )
        for artifact_id in artifact_ids:
            repository.read(artifact_id)
        if _snapshot(session, record, settings) != snapshot:
            raise ReturnDenied()
        if supplied:
            # A valid existing envelope owns its text/media interleaving. Do not
            # reorder its segments when reusing the structured response contract.
            return outcome
        # Ordinary OpenAI completion works as well as an existing structured response.
        # Selection is the trusted host's explicit PUT, never a path/URL parsed from text.
        text_parts = tuple(part for part in outcome.parts if part.type == "text")
        envelope = ResponseEnvelope(
            response_id=response_id,
            parts=(*text_parts, *(ArtifactRefPart(artifact_id=aid) for aid in artifact_ids)),
        )
        return replace(outcome, response=envelope, assistant_content=envelope.assistant_content)
    except Exception:
        # Do not include SQL, paths, host metadata, or capability values in worker logs.
        raise HermesResponseError(operation="artifact_return_seal") from None
