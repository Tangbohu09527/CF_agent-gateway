"""Explicit private-thread history transition, not a legacy runtime attestation.

The binding intent is committed before create. The existing fenced result CAS is
the only operation that changes the thread tip. Closed grants are not evidence
of successful execution; a committed Dispatch and Response are also required.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.hermes.errors import HermesDispatchError
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.inbound.host_binding import digest, runtime_digest
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.store import authorized_dispatch_source, aware
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import ThreadType

POLICY = "fresh_runtime_v1"
MAX_INSTRUCTION_CHARS = 16000


def current_claim(session, message, thread, expected_claim_token):
    record = session.scalar(
        select(HermesDispatchRecord).where(
            HermesDispatchRecord.message_id == message.id,
            HermesDispatchRecord.claim_token == expected_claim_token,
        )
    )
    if (
        not expected_claim_token
        or record is None
        or record.ai_thread_id != thread.id
        or record.status is not HermesDispatchStatus.RUNNING
        or record.lease_expires_at is None
        or aware(record.lease_expires_at) <= datetime.now(UTC)
    ):
        raise HermesDispatchError(reason="inbound_execution_claim_unavailable")
    try:
        authorized_dispatch_source(session, message_id=message.id, dispatch_id=record.id)
    except MediaFetchError:
        raise HermesDispatchError(reason="inbound_execution_identity_changed") from None
    return record


def managed_parent(session, thread, message, record, settings, *, pending=None):
    """Return a proven exact tip or reject a broken lineage; never silently reset."""
    binding = session.scalar(
        select(InboundHostBinding).where(
            InboundHostBinding.session_id == thread.hermes_thread_id,
            InboundHostBinding.execution_policy.is_not(None),
        )
    )
    if binding is None:
        history = select(InboundHostBinding.id).where(
            InboundHostBinding.ai_thread_id == thread.id,
            InboundHostBinding.execution_policy.is_not(None),
        )
        if pending is not None:
            history = history.where(InboundHostBinding.id != pending.id)
        if session.scalar(history.limit(1)) is not None:
            raise HermesDispatchError(reason="inbound_execution_lineage_unverified")
        return None
    creator = session.get(HermesDispatchRecord, binding.dispatch_id)
    original = session.get(Message, creator.message_id) if creator else None
    response = session.scalar(
        select(HermesDispatchResponse.id).where(
            HermesDispatchResponse.dispatch_record_id == binding.dispatch_id,
        )
    )
    if (
        binding.execution_policy != POLICY
        or binding.ai_thread_id != thread.id
        or creator is None
        or creator.status is not HermesDispatchStatus.SUCCESS
        or response is None
        or original is None
        or creator.enterprise_identity_id != record.enterprise_identity_id
        or creator.workspace_id != record.workspace_id
        or creator.ai_thread_id != thread.id
        or not same_source(original, message)
        or not binding.preparation_started_at
        or not binding.history_digest
        or not settings.enabled
        or not settings.dedicated_endpoint_confirmed
        or not settings.runtime_fallback_disabled_confirmed
        or binding.host_id != settings.host_id
        or binding.profile_reference != settings.profile_reference
        or binding.profile_revision != settings.profile_revision
        or binding.runtime_config_digest != runtime_digest(settings)
    ):
        raise HermesDispatchError(reason="inbound_execution_lineage_unverified")
    return binding


def same_source(left, right):
    return all(
        getattr(left, field) == getattr(right, field)
        for field in (
            "source",
            "source_account_id",
            "conversation_id",
            "conversation_type",
            "sender_id",
        )
    )


def fresh_allowed(thread, message, settings):
    return (
        settings.enabled
        and thread.id in settings.fresh_session_thread_ids
        and thread.thread_type is ThreadType.PRIVATE
        and message.conversation_type == "private"
        and message.source == "wechat"
    )


def instruction(session, message, record, *, existing=None):
    """Copy one complete prior user instruction; never synthesize old history.

    Select the latest successful same-source dispatch, without skipping an
    intervening successful attachment. A failed old attachment is not replayed.
    Recovery uses the frozen ID and digest, not a fresh latest-message query.
    """
    query = (
        select(Message, HermesDispatchRecord)
        .join(HermesDispatchRecord, HermesDispatchRecord.message_id == Message.id)
        .join(
            HermesDispatchResponse,
            HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
        )
        .where(
            or_(
                HermesDispatchRecord.created_at < record.created_at,
                and_(
                    HermesDispatchRecord.created_at == record.created_at,
                    HermesDispatchRecord.id < record.id,
                ),
            ),
            HermesDispatchRecord.ai_thread_id == record.ai_thread_id,
            HermesDispatchRecord.workspace_id == record.workspace_id,
            HermesDispatchRecord.enterprise_identity_id == record.enterprise_identity_id,
            HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
            Message.source == message.source,
            Message.source_account_id == message.source_account_id,
            Message.conversation_id == message.conversation_id,
            Message.conversation_type == "private",
            Message.sender_id == message.sender_id,
            Message.sender_type == "human",
            Message.direction == "inbound",
            Message.is_self.is_(False),
        )
    )
    if existing is not None:
        query = query.where(Message.id == existing.instruction_message_id)
    row = session.execute(
        query.order_by(
            HermesDispatchRecord.created_at.desc(), HermesDispatchRecord.id.desc()
        ).limit(1)
    ).first()
    if row is None or row[0].message_type != "text" or row[0].raw_type != 1:
        raise HermesDispatchError(reason="inbound_execution_instruction_missing")
    previous, dispatched = row
    try:
        authorized_dispatch_source(session, message_id=previous.id, dispatch_id=dispatched.id)
    except MediaFetchError:
        raise HermesDispatchError(reason="inbound_execution_identity_changed") from None
    if not previous.content.strip() or len(previous.content) > MAX_INSTRUCTION_CHARS:
        raise HermesDispatchError(reason="inbound_execution_instruction_invalid")
    proof = digest(
        json.dumps(
            {
                "message_id": previous.id,
                "dispatch_id": dispatched.id,
                "thread_id": dispatched.ai_thread_id,
                "workspace_id": dispatched.workspace_id,
                "identity_id": dispatched.enterprise_identity_id,
                "source": previous.source,
                "account": previous.source_account_id,
                "chat": previous.conversation_id,
                "sender": previous.sender_id,
                "text": previous.content,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if existing is not None and proof != existing.instruction_digest:
        raise HermesDispatchError(reason="inbound_execution_instruction_changed")
    return previous.id, proof, {"message_id": previous.id, "text": previous.content}
