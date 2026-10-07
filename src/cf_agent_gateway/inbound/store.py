from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError, MediaSource
from cf_agent_gateway.agent_profile import AgentProfile, AgentProfileStatus
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model.models import HermesDispatchRecord
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadStatus,
    ThreadType,
    WorkspaceStatus,
)
from cf_agent_gateway.workspace.thread_keys import build_v2_thread_key


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_media_message(message: Message) -> bool:
    if message.source != "wechat" or message.is_self:
        return False
    if message.raw_type == 3 or (message.raw_type == 49 and message.message_type == "file"):
        return True
    # The deployed adapter can expose appmsg files as a bare filename. This is
    # only a retrieval candidate: actual type/bytes must still pass HTTP binding.
    return bool(
        message.raw_type == 49
        and message.message_type == "app"
        and len(message.content) <= 255
        and message.content.isprintable()
        and re.fullmatch(
            r"[^<>/\\:\r\n]+\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|jpe?g|png|txt|csv)",
            message.content,
            re.IGNORECASE,
        )
    )


def source_for(message: Message) -> MediaSource:
    return MediaSource(
        account_id=message.source_account_id,
        chat_id=message.conversation_id,
        sender_id=message.sender_id,
        local_id=message.source_local_id,
        server_id=message.source_server_id,
        raw_type=message.raw_type,
        occurred_at=aware(message.occurred_at),
        content_sha256=hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
    )


def stage_job(session: Session, record: HermesDispatchRecord, *, wait_seconds: int) -> None:
    message = session.get(Message, record.message_id)
    if message is None or not is_media_message(message):
        return
    now = datetime.now(UTC)
    try:
        fingerprint = source_for(message).fingerprint
        state, error = "pending", None
    except MediaFetchError as exc:
        fingerprint, state, error = "0" * 64, "failed", exc.code
    session.add(
        InboundMediaJob(
            message_id=message.id,
            dispatch_id=record.id,
            source_fingerprint=fingerprint,
            state=state,
            last_error_code=error,
            created_at=now,
            deadline_at=now + timedelta(seconds=wait_seconds),
            next_attempt_at=now,
        )
    )
    session.flush()


def authorized_source(
    session: Session, job: InboundMediaJob
) -> tuple[Message, HermesDispatchRecord]:
    """Recheck current source identity, not merely a historical allowed outcome."""
    message, record = authorized_dispatch_source(
        session,
        message_id=job.message_id,
        dispatch_id=job.dispatch_id,
    )
    if source_for(message).fingerprint != job.source_fingerprint:
        raise MediaFetchError("media_source_binding_changed")
    return message, record


def authorized_dispatch_source(
    session: Session,
    *,
    message_id: int,
    dispatch_id: int,
) -> tuple[Message, HermesDispatchRecord]:
    """Shared current identity/source check for inbound and explicit task outputs."""
    message = session.get(Message, message_id, populate_existing=True)
    record = session.get(HermesDispatchRecord, dispatch_id, populate_existing=True)
    if message is None or record is None or record.message_id != message.id:
        raise MediaFetchError("media_source_binding_changed")
    mapping = session.scalar(
        select(SourceIdentityMapping)
        .where(
            SourceIdentityMapping.platform == message.source,
            SourceIdentityMapping.account_id == message.source_account_id,
            SourceIdentityMapping.sender_id == message.sender_id,
        )
        .execution_options(populate_existing=True)
    )
    identity = session.get(
        EnterpriseIdentity, record.enterprise_identity_id, populate_existing=True
    )
    workspace = session.get(EmployeeWorkspace, record.workspace_id, populate_existing=True)
    thread = session.get(AIThread, record.ai_thread_id, populate_existing=True)
    if (
        mapping is None
        or not mapping.enabled
        or mapping.enterprise_identity_id != record.enterprise_identity_id
        or identity is None
        or identity.status is not IdentityStatus.ACTIVE
        or workspace is None
        or workspace.status is not WorkspaceStatus.ACTIVE
        or workspace.enterprise_identity_id != identity.id
        or thread is None
        or thread.status is not ThreadStatus.ACTIVE
        or (thread.thread_type is ThreadType.PRIVATE and thread.workspace_id != workspace.id)
    ):
        raise MediaFetchError("media_identity_unavailable")
    if thread.agent_profile_id is not None:
        profile = session.get(AgentProfile, thread.agent_profile_id, populate_existing=True)
        if profile is None or profile.status is not AgentProfileStatus.ACTIVE:
            raise MediaFetchError("media_identity_unavailable")
        expected_key = build_v2_thread_key(
            platform=message.source,
            account_id=message.source_account_id,
            physical_conversation_id=message.conversation_id,
            conversation_type=message.conversation_type,
            sender_identity_id=identity.id,
            agent_profile_id=profile.id,
            agent_profile_revision=profile.revision,
            thread_policy=thread.thread_policy,
        )
        if expected_key != thread.thread_key:
            raise MediaFetchError("media_identity_unavailable")
    return message, record
