"""Current-claim output capabilities and atomic response sealing.

Only a trusted host's explicit return operation uploads bytes. This module never
opens a host path, fetches a URL, or places credentials in a model message.
"""

import base64
import hashlib
import hmac
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update

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


def _snapshot(session, record, settings):
    message = session.get(Message, record.message_id, populate_existing=True)
    if message is None or message.source != "wechat" or message.is_self:
        raise ReturnDenied()
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
    return {
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
        "profile": settings.profile_reference,
        "revision": settings.profile_revision,
        "expires": int(
            (aware(record.claimed_at) + timedelta(seconds=settings.ttl_seconds)).timestamp()
        ),
        "max_bytes": settings.max_bytes,
        "max_artifacts": settings.max_artifacts,
    }


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
    snapshot = _snapshot(session, record, settings)
    if snapshot["expires"] <= datetime.now(UTC).timestamp():
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


def authorize(session, settings, dispatch_id, authorization):
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
        snapshot = _snapshot(session, record, settings)
        if claims != {"schema": SCHEMA, "session_id": claims["session_id"], **snapshot}:
            raise ReturnDenied()
        if snapshot["expires"] <= datetime.now(UTC).timestamp():
            raise ReturnDenied()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ReturnDenied() from None
    return record, response_id_for(record, snapshot)


def seal_returns(session, record_id, claim_token, outcome, *, settings, storage_root):
    """Merge only this claim's explicitly uploaded artifacts under the completion lock.

    The caller commits the resulting dispatch response in this SAME transaction.
    No upload can be acknowledged after the success CAS clears the claim.
    """
    from cf_agent_gateway.hermes.errors import HermesResponseError
    from cf_agent_gateway.hermes.models import ArtifactRefPart, ResponseEnvelope

    if not settings.enabled:
        return outcome
    try:
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
        expected_slots = [slot_id(response_id, slot) for slot in range(settings.max_artifacts)]
        if any(
            a.response_id != response_id
            or a.artifact_id not in expected_slots
            or a.status is not ArtifactStatus.READY
            for a in candidates
        ):
            raise ReturnDenied()
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
