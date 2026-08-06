from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from cf_agent_gateway.access import RequestFacts, RiskLevel
from cf_agent_gateway.adapters.wechat import (
    NormalizedWechatMessage,
    wechat_message_to_event,
)
from cf_agent_gateway.admission import AdmissionCandidate, AdmissionOrchestrator, AdmissionReason
from cf_agent_gateway.hermes import (
    HermesDispatcher,
    HermesDispatchOutboxExecutor,
    HermesResponseRelay,
)
from cf_agent_gateway.ingestion.errors import PersistedMessageNotFoundError
from cf_agent_gateway.ingestion.models import (
    MessageIngestionOutcome,
    PersistedAttachmentSnapshot,
    PersistedMessageSnapshot,
)
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.task.model import (
    HermesDispatchRecordStore,
    HermesDispatchStatus,
    build_hermes_dispatch_idempotency_key,
)


class AdmissionRequestResolver(Protocol):
    """Resolve request facts from a read-only snapshot of a persisted message."""

    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts: ...


class DefaultAdmissionRequestResolver:
    """Return conservative V1 request facts without interpreting message content."""

    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts:
        return RequestFacts(
            requested_scope=frozenset(),
            requested_skill_ids=frozenset(),
            risk_level=RiskLevel.NORMAL,
        )


class MessageAdmissionService:
    """Persist a normalized message before independently running admission.

    Message storage and admission intentionally do not share one transaction. A successful
    ``MessageStore.create`` has committed before admission starts, so admission failures leave
    message history intact for at-least-once redelivery. Allowed admissions commit a durable
    dispatch record before the optional synchronous compatibility dispatcher is invoked.
    """

    def __init__(
        self,
        session: Session,
        request_resolver: AdmissionRequestResolver | None = None,
        *,
        message_store: MessageStore | None = None,
        admission_orchestrator: AdmissionOrchestrator | None = None,
        hermes_dispatcher: HermesDispatcher | None = None,
        dispatch_record_store: HermesDispatchRecordStore | None = None,
    ) -> None:
        self._session = session
        self._message_store = message_store if message_store is not None else MessageStore(session)
        self._request_resolver = (
            request_resolver if request_resolver is not None else DefaultAdmissionRequestResolver()
        )
        self._admission_orchestrator = (
            admission_orchestrator
            if admission_orchestrator is not None
            else AdmissionOrchestrator(session)
        )
        self._dispatch_record_store = (
            dispatch_record_store
            if dispatch_record_store is not None
            else HermesDispatchRecordStore(session)
        )
        self._hermes_dispatcher = _outbox_managed_dispatcher(
            session,
            hermes_dispatcher,
            record_store=self._dispatch_record_store,
        )

    def process(self, message: NormalizedWechatMessage) -> MessageIngestionOutcome:
        event = wechat_message_to_event(message)
        stored_message, message_created = self._message_store.create(event)

        persisted_message = self._message_store.get(stored_message.id)
        if persisted_message is None:
            raise PersistedMessageNotFoundError(stored_message.id)

        self._session.refresh(persisted_message)
        message_id = persisted_message.id
        source = persisted_message.source
        source_account_id = persisted_message.source_account_id
        conversation_id = persisted_message.conversation_id
        conversation_type = persisted_message.conversation_type
        sender_type = persisted_message.sender_type
        sender_id = persisted_message.sender_id
        is_self = persisted_message.is_self
        is_mentioned = persisted_message.is_mentioned
        message_type = persisted_message.message_type
        request_message = self._request_snapshot(persisted_message)
        request = self._request_resolver.resolve(request_message)

        candidate = AdmissionCandidate(
            message_id=message_id,
            source=source,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            sender_type=sender_type,
            sender_id=sender_id,
            is_self=is_self,
            is_mentioned=is_mentioned,
            message_type=message_type,
            requested_scope=request.requested_scope,
            requested_skill_ids=request.requested_skill_ids,
            risk_level=request.risk_level,
        )
        admission = self._admission_orchestrator.admit(candidate)
        dispatch_record = None
        dispatch_record_created = False
        if admission.admitted and admission.reason is AdmissionReason.ALLOWED:
            if message_created:
                dispatch_record, dispatch_record_created = self._dispatch_record_store.enqueue(
                    admission
                )
            else:
                dispatch_record = self._dispatch_record_store.get_by_idempotency_key(
                    build_hermes_dispatch_idempotency_key(message_id)
                )

        hermes_dispatch = None
        if (
            dispatch_record_created
            and dispatch_record is not None
            and dispatch_record.status is HermesDispatchStatus.QUEUED
            and self._hermes_dispatcher is not None
        ):
            hermes_dispatch = self._hermes_dispatcher.dispatch(admission)
        return MessageIngestionOutcome(
            message_id=message_id,
            message_created=message_created,
            admission=admission,
            should_create_task=admission.should_create_task,
            workspace_id=admission.workspace_id,
            ai_thread_id=admission.ai_thread_id,
            dispatch_record_id=(dispatch_record.id if dispatch_record is not None else None),
            hermes_dispatch=hermes_dispatch,
        )

    @staticmethod
    def _request_snapshot(message: Message) -> PersistedMessageSnapshot:
        return PersistedMessageSnapshot(
            message_id=message.id,
            event_id=message.event_id,
            source=message.source,
            source_account_id=message.source_account_id,
            source_message_id=message.source_message_id,
            conversation_id=message.conversation_id,
            conversation_type=message.conversation_type,
            is_mentioned=message.is_mentioned,
            is_self=message.is_self,
            sender_type=message.sender_type,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            message_type=message.message_type,
            raw_type=message.raw_type,
            content=message.content,
            timestamp=message.timestamp,
            occurred_at=message.occurred_at,
            received_at=message.received_at,
            direction=message.direction,
            source_local_id=message.source_local_id,
            source_server_id=message.source_server_id,
            source_message_id_is_fallback=message.source_message_id_is_fallback,
            reply_context=message.reply_context,
            reply_to_message_id=message.reply_to_message_id,
            created_at=message.created_at,
            attachments=tuple(
                PersistedAttachmentSnapshot(
                    id=attachment.id,
                    message_id=attachment.message_id,
                    filename=attachment.filename,
                    file_type=attachment.file_type,
                    mime_type=attachment.mime_type,
                    file_size=attachment.file_size,
                    storage_path=attachment.storage_path,
                    hash=attachment.hash,
                    created_at=attachment.created_at,
                )
                for attachment in message.attachments
            ),
        )


def _outbox_managed_dispatcher(
    session: Session,
    dispatcher: HermesDispatcher | None,
    *,
    record_store: HermesDispatchRecordStore,
) -> HermesDispatcher | None:
    if dispatcher is None or getattr(dispatcher, "manages_dispatch_records", False) is True:
        return dispatcher
    if isinstance(dispatcher, HermesResponseRelay):
        return dispatcher.map_dispatcher(
            lambda inner: HermesDispatchOutboxExecutor(
                session,
                inner,
                record_store=record_store,
            )
        )
    return HermesDispatchOutboxExecutor(
        session,
        dispatcher,
        record_store=record_store,
    )
