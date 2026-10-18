"""Explicit, opt-in reuse of registered bytes under a NEW admitted task.

The selector is deliberately not natural-language search. Quoted references only
use persisted upstream IDs; quoted text and model-supplied IDs grant no authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from cf_agent_gateway.access import AccessPolicyService, RequestFacts, evaluate_access
from cf_agent_gateway.access.policy_models import GatewayAccessPolicy, UserAccessPolicy
from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
from cf_agent_gateway.admission.models import MessageAdmissionOutcome
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.store import authorized_dispatch_source, aware, source_for
from cf_agent_gateway.message.models import Attachment, Message

_COMMAND = re.compile(r"/attachment ([1-9][0-9]{0,18}|quoted) (read|read-return)\Z")
_SCOPE = ("source", "source_account_id", "conversation_id", "conversation_type", "sender_id")
_RECORD_SCOPE = ("enterprise_identity_id", "workspace_id", "ai_thread_id")


def parse(message):
    first, _, instruction = message.content.partition("\n")
    match = _COMMAND.fullmatch(first)
    if (
        message.source != "wechat"
        or message.is_self
        or not (
            message.raw_type == 1 or (message.raw_type == 49 and message.message_type == "reply")
        )
        or message.conversation_type != "private"
        or message.message_type not in {"text", "reply"}
        or match is None
        or len(instruction) > 4096
    ):
        raise MediaFetchError("media_followup_reference_required")
    selector, action = match.groups()
    if selector != "quoted" and int(selector) > 2**63 - 1:
        raise MediaFetchError("media_followup_reference_required")
    return selector, action


def request_fingerprint(message):
    values = {key: getattr(message, key) for key in _SCOPE}
    values.update(
        message_id=message.id,
        local=message.source_local_id,
        server=message.source_server_id,
        raw_type=message.raw_type,
        content=message.content,
        reply=message.reply_context,
        occurred_at=aware(message.occurred_at).isoformat(),
    )
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _select_original(session, message, record, selector):
    query = (
        select(InboundMediaJob)
        .join(Attachment, Attachment.id == InboundMediaJob.attachment_id)
        .join(Message, Message.id == Attachment.message_id)
        .where(
            InboundMediaJob.source_job_id.is_(None),
            InboundMediaJob.state == "ready",
            Message.id < message.id,
            *(getattr(Message, key) == getattr(message, key) for key in _SCOPE),
        )
    )
    if selector == "quoted":
        reply = message.reply_context or {}
        ids = [
            (key, reply.get(key))
            for key in ("source_local_id", "source_server_id")
            if reply.get(key) is not None
        ]
        if (
            not ids
            or any(not isinstance(value, str) or not value for _, value in ids)
            or reply.get("sender_id") not in (None, message.sender_id)
        ):
            raise MediaFetchError("media_followup_reference_required")
        query = query.where(*(getattr(Message, key) == value for key, value in ids))
    else:
        query = query.where(Attachment.id == int(selector))
    candidates = list(session.scalars(query.execution_options(populate_existing=True).limit(2)))
    if len(candidates) != 1:
        raise MediaFetchError("media_followup_reference_required")
    original = candidates[0]
    original_message, original_record = authorized_dispatch_source(
        session, message_id=original.message_id, dispatch_id=original.dispatch_id
    )
    if (
        any(getattr(record, key) != getattr(original_record, key) for key in _RECORD_SCOPE)
        or source_for(original_message).fingerprint != original.source_fingerprint
    ):
        raise MediaFetchError("media_followup_reference_required")
    _registry(session, original)
    return original


def _registry(session, original):
    attachment = session.get(Attachment, original.attachment_id, populate_existing=True)
    manifest = original.manifest
    if (
        attachment is None
        or manifest is None
        or original.state != "ready"
        or attachment.message_id != original.message_id
        or attachment.hash != manifest.get("sha256")
        or attachment.file_size != manifest.get("size")
        or attachment.storage_path != "inbound-media:" + manifest.get("reference", "")
        or manifest.get("reference")
        != hashlib.sha256(
            (original.source_fingerprint + ":" + manifest.get("sha256", "")).encode("ascii")
        ).hexdigest()
    ):
        raise MediaFetchError("media_registry_conflict")
    return manifest


def _return_type(manifest):
    types = {"pdf": ("file", "application/pdf"), "png": ("image", "image/png")}
    result = types.get(manifest.get("signature"))
    if result is None:
        raise MediaFetchError("media_followup_operation_unavailable")
    return result


def stage_followup(session, record, *, wait_seconds):
    message = session.get(Message, record.message_id)
    if message is None or not message.content.startswith("/attachment"):
        return
    now = datetime.now(UTC)
    job = InboundMediaJob(
        message_id=message.id,
        dispatch_id=record.id,
        source_fingerprint=request_fingerprint(message),
        state="unsupported",
        last_error_code="media_followup_reference_required",
        created_at=now,
        deadline_at=now + timedelta(seconds=wait_seconds),
        next_attempt_at=now,
    )
    try:
        selector, action = parse(message)
        original = _select_original(session, message, record, selector)
        if action != "read":
            _return_type(original.manifest)
        job.source_job_id = original.id
        job.followup_action = action
        job.manifest = copy.deepcopy(original.manifest)
        job.attachment_id = original.attachment_id
        job.state, job.last_error_code = "ready", None
    except MediaFetchError as error:
        if error.code == "media_followup_operation_unavailable":
            job.last_error_code = error.code
    session.add(job)
    session.flush()


def _current_policy(session, message, record):
    admission = session.scalar(
        select(MessageAdmissionOutcome)
        .where(MessageAdmissionOutcome.message_id == message.id)
        .execution_options(populate_existing=True)
    )
    if (
        admission is None
        or admission.requested_scope is None
        or admission.requested_skill_ids is None
        or admission.risk_level is None
    ):
        raise MediaFetchError("media_identity_unavailable")
    # Do not reuse cached policy rows across reads in a long-lived host request.
    list(session.scalars(select(GatewayAccessPolicy).execution_options(populate_existing=True)))
    list(
        session.scalars(
            select(UserAccessPolicy)
            .where(UserAccessPolicy.enterprise_identity_id == record.enterprise_identity_id)
            .execution_options(populate_existing=True)
        )
    )
    service = AccessPolicyService(session)
    decision = evaluate_access(
        service.resolve_source_identity_facts(
            source=message.source,
            source_account_id=message.source_account_id,
            sender_id=message.sender_id,
        ),
        service.resolve_conversation_facts(
            conversation_type=message.conversation_type, is_mentioned=message.is_mentioned
        ),
        RequestFacts(
            requested_scope=admission.requested_scope,
            requested_skill_ids=admission.requested_skill_ids,
            risk_level=admission.risk_level,
        ),
        service.resolve_gateway_policy_facts(),
    )
    if not decision.allowed or decision.enterprise_identity_id != record.enterprise_identity_id:
        raise MediaFetchError("media_identity_unavailable")


def authorize_followup(session, job):
    job = session.get(InboundMediaJob, job.id, populate_existing=True)
    if job is None:
        raise MediaFetchError("media_followup_unavailable")
    message, record = authorized_dispatch_source(
        session, message_id=job.message_id, dispatch_id=job.dispatch_id
    )
    if (
        job.state != "ready"
        or job.source_job_id is None
        or aware(job.deadline_at) <= datetime.now(UTC)
        or request_fingerprint(message) != job.source_fingerprint
    ):
        raise MediaFetchError("media_followup_unavailable")
    selector, action = parse(message)
    original = _select_original(session, message, record, selector)
    if (
        original.id != job.source_job_id
        or action != job.followup_action
        or original.attachment_id != job.attachment_id
        or original.manifest != job.manifest
    ):
        raise MediaFetchError("media_registry_conflict")
    _current_policy(session, message, record)
    validate_profile(session, job)
    return message, record


def validate_profile(session, job, *, profile_reference=None, profile_revision=None):
    """Use durable original execution/admission evidence, never the model's profile."""
    original = session.get(InboundMediaJob, job.source_job_id, populate_existing=True)
    if original is None:
        raise MediaFetchError("media_followup_profile_unavailable")
    profiles = set(
        session.execute(
            select(InboundHostBinding.profile_reference, InboundHostBinding.profile_revision).where(
                InboundHostBinding.job_id == original.id
            )
        ).all()
    )
    admission = session.scalar(
        select(MessageAdmissionOutcome)
        .where(MessageAdmissionOutcome.message_id == original.message_id)
        .execution_options(populate_existing=True)
    )
    if admission is not None and admission.agent_profile_reference is not None:
        profiles.add((admission.agent_profile_reference, admission.agent_profile_revision))
    if len(profiles) != 1:
        raise MediaFetchError("media_followup_profile_unavailable")
    approved = next(iter(profiles))
    if profile_reference is not None and (profile_reference, profile_revision) != approved:
        raise MediaFetchError("media_followup_profile_unavailable")
    current = session.scalar(
        select(MessageAdmissionOutcome)
        .where(MessageAdmissionOutcome.message_id == job.message_id)
        .execution_options(populate_existing=True)
    )
    if (
        current is not None
        and current.agent_profile_reference is not None
        and (current.agent_profile_reference, current.agent_profile_revision) != approved
    ):
        raise MediaFetchError("media_followup_profile_unavailable")
    if any(
        tuple(row) != approved
        for row in session.execute(
            select(InboundHostBinding.profile_reference, InboundHostBinding.profile_revision).where(
                InboundHostBinding.job_id == job.id
            )
        )
    ):
        raise MediaFetchError("media_followup_profile_unavailable")


def verify_available(session, job, *, staging_root, staging=None):
    """Before a new host binding: verify stored bytes without fetching the old message."""
    authorize_followup(session, job)
    manifest = dict(job.manifest)
    cache = staging if staging is not None else InboundMediaStaging(Path(staging_root))
    cache.read(manifest["reference"], size=manifest["size"], sha256=manifest["sha256"])
    authorize_followup(session, job)
    if job.manifest != manifest:
        raise MediaFetchError("media_registry_conflict")


def return_constraint(session, record):
    job = session.scalar(
        select(InboundMediaJob)
        .where(InboundMediaJob.dispatch_id == record.id)
        .execution_options(populate_existing=True)
    )
    if job is None or job.source_job_id is None:
        return None
    authorize_followup(session, job)
    if job.followup_action == "read":
        return {"action": "read"}
    kind, mime = _return_type(job.manifest)
    return {
        "action": job.followup_action,
        "attachment_id": job.attachment_id,
        "size": job.manifest["size"],
        "sha256": job.manifest["sha256"],
        "kind": kind,
        "mime_type": mime,
    }
