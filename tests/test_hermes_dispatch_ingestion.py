from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.adapters.wechat import (
    NormalizedWechatMessage,
    WechatConversationType,
    WechatMessageType,
    WechatSenderType,
)
from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import HermesDispatchOutcome, HermesResponseRelay
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import MessageAdmissionService, MessageIngestionOutcome
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchStatus,
)
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace

SOURCE_ACCOUNT_ID = "wxid-gateway"
CONVERSATION_ID = "wxid-alice"
SENDER_ID = "wxid-alice"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as database_session:
            yield database_session
    finally:
        engine.dispose()


def normalized_message(**overrides: object) -> NormalizedWechatMessage:
    values: dict[str, object] = {
        "source_account_id": SOURCE_ACCOUNT_ID,
        "source_message_id": "server-001",
        "source_local_id": "local-001",
        "source_server_id": "server-001",
        "source_message_id_is_fallback": False,
        "event_id": "wechat:event-001",
        "conversation_id": CONVERSATION_ID,
        "conversation_type": WechatConversationType.PRIVATE,
        "conversation_name": "Alice",
        "sender_type": WechatSenderType.HUMAN,
        "sender_id": SENDER_ID,
        "sender_name": "Alice",
        "message_type": WechatMessageType.TEXT,
        "raw_type": 1,
        "content": "summarize the release notes",
        "timestamp": datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
        "is_mentioned": None,
        "is_self": False,
        "reply": None,
    }
    values.update(overrides)
    return NormalizedWechatMessage.model_validate(values)


def allow_sender(session: Session) -> None:
    identity_service = IdentityService(session)
    identity = identity_service.create_identity(employee_id="employee-alice")
    identity_service.create_mapping(
        platform="wechat",
        account_id=SOURCE_ACCOUNT_ID,
        sender_id=SENDER_ID,
        enterprise_identity_id=identity.id,
    )
    access_policy_service = AccessPolicyService(session)
    access_policy_service.upsert_user_policy(
        enterprise_identity_id=identity.id,
        enabled=True,
    )
    access_policy_service.upsert_gateway_policy(
        enabled=True,
        allowed_risk_levels={RiskLevel.NORMAL},
    )


class _RecordingDispatcher:
    def __init__(self, session: Session) -> None:
        self._session = session
        self.admissions: list[AdmissionOutcome] = []
        self.messages: list[Message] = []
        self.results: list[HermesDispatchOutcome] = []

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        self.admissions.append(admission)
        message = self._session.get(Message, admission.message_id)
        assert message is not None
        assert admission.workspace_id is not None
        assert admission.ai_thread_id is not None
        self.messages.append(message)
        result = HermesDispatchOutcome(
            message_id=message.id,
            workspace_id=admission.workspace_id,
            ai_thread_id=admission.ai_thread_id,
            assistant_content="Hermes response",
        )
        self.results.append(result)
        return result


class _NeverDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        del admission
        self.calls += 1
        raise AssertionError("denied admissions must not be dispatched")


class _DispatchFailure(RuntimeError):
    pass


class _FailingDispatcher:
    def __init__(self) -> None:
        self.admissions: list[AdmissionOutcome] = []

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        self.admissions.append(admission)
        raise _DispatchFailure("Hermes dispatch failed")


def test_ingestion_outcome_preserves_legacy_positional_dispatch_field() -> None:
    admission = AdmissionOutcome(
        message_id=1,
        admitted=True,
        should_create_task=True,
        reason=AdmissionReason.ALLOWED,
        enterprise_identity_id="identity-1",
        workspace_id="workspace-1",
        ai_thread_id="thread-1",
    )
    dispatch = HermesDispatchOutcome(
        message_id=1,
        workspace_id="workspace-1",
        ai_thread_id="thread-1",
        assistant_content="Hermes response",
    )

    outcome = MessageIngestionOutcome(1, True, admission, True, "workspace-1", "thread-1", dispatch)

    assert outcome.hermes_dispatch is dispatch
    assert outcome.dispatch_record_id is None


def test_allowed_admission_dispatches_authoritative_persisted_message(session: Session) -> None:
    allow_sender(session)
    message = normalized_message()
    dispatcher = _RecordingDispatcher(session)

    outcome = MessageAdmissionService(
        session,
        hermes_dispatcher=dispatcher,
    ).process(message)

    assert outcome.admission.admitted is True
    assert outcome.admission.reason is AdmissionReason.ALLOWED
    assert dispatcher.admissions == [outcome.admission]
    assert dispatcher.admissions[0] is outcome.admission
    assert dispatcher.messages[0].id == outcome.message_id == outcome.admission.message_id
    assert dispatcher.messages[0].event_id == message.event_id
    assert dispatcher.messages[0].content == message.content
    assert outcome.hermes_dispatch is dispatcher.results[0]
    assert outcome.hermes_dispatch.message_id == outcome.message_id
    assert outcome.hermes_dispatch.workspace_id == outcome.workspace_id
    assert outcome.hermes_dispatch.ai_thread_id == outcome.ai_thread_id
    assert outcome.hermes_dispatch.assistant_content == "Hermes response"
    assert outcome.dispatch_record_id is not None
    dispatch_record = session.get(HermesDispatchRecord, outcome.dispatch_record_id)
    assert dispatch_record is not None
    assert dispatch_record.status is HermesDispatchStatus.SUCCESS
    assert session.get(EmployeeWorkspace, outcome.workspace_id) is not None
    assert session.get(AIThread, outcome.ai_thread_id) is not None


def test_raw_dispatcher_is_managed_and_not_replayed(session: Session) -> None:
    allow_sender(session)
    message = normalized_message()
    dispatcher = _RecordingDispatcher(session)
    service = MessageAdmissionService(session, hermes_dispatcher=dispatcher)

    first = service.process(message)
    duplicate = service.process(message)

    assert first.hermes_dispatch is not None
    assert duplicate.hermes_dispatch is None
    assert duplicate.dispatch_record_id == first.dispatch_record_id
    assert len(dispatcher.admissions) == 1
    assert first.dispatch_record_id is not None
    record = session.get(HermesDispatchRecord, first.dispatch_record_id)
    assert record is not None
    assert record.status is HermesDispatchStatus.SUCCESS


def test_denied_admission_never_invokes_dispatcher(session: Session) -> None:
    dispatcher = _NeverDispatcher()

    outcome = MessageAdmissionService(
        session,
        hermes_dispatcher=dispatcher,
    ).process(normalized_message())

    assert outcome.admission.admitted is False
    assert outcome.admission.reason is AdmissionReason.ACCESS_DENIED
    assert outcome.dispatch_record_id is None
    assert outcome.hermes_dispatch is None
    assert dispatcher.calls == 0
    assert session.get(Message, outcome.message_id) is not None
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0


class _LookupOnlyDispatchRecordStore(HermesDispatchRecordStore):
    def enqueue(
        self,
        admission: AdmissionOutcome,
    ) -> tuple[HermesDispatchRecord, bool]:
        del admission
        raise AssertionError("duplicate messages must not be enqueued again")


def test_duplicate_queued_message_is_not_reenqueued_or_dispatched(session: Session) -> None:
    allow_sender(session)
    message = normalized_message()

    first = MessageAdmissionService(session).process(message)
    dispatcher = _RecordingDispatcher(session)
    duplicate = MessageAdmissionService(
        session,
        hermes_dispatcher=dispatcher,
        dispatch_record_store=_LookupOnlyDispatchRecordStore(session),
    ).process(message)

    assert first.dispatch_record_id is not None
    assert duplicate.dispatch_record_id == first.dispatch_record_id
    assert duplicate.message_created is False
    assert first.hermes_dispatch is None
    assert duplicate.hermes_dispatch is None
    assert dispatcher.admissions == []
    record = session.get(HermesDispatchRecord, first.dispatch_record_id)
    assert record is not None
    assert record.status is HermesDispatchStatus.QUEUED
    assert record.attempt_count == 0
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_dispatch_failure_propagates_after_message_commit(session: Session) -> None:
    allow_sender(session)
    message = normalized_message()
    dispatcher = _FailingDispatcher()

    with pytest.raises(_DispatchFailure, match="Hermes dispatch failed"):
        MessageAdmissionService(
            session,
            hermes_dispatcher=dispatcher,
        ).process(message)

    persisted = session.scalar(select(Message).where(Message.event_id == message.event_id))
    assert persisted is not None
    assert persisted.content == message.content
    dispatch_record = session.scalar(select(HermesDispatchRecord))
    assert dispatch_record is not None
    assert dispatch_record.status is HermesDispatchStatus.UNCERTAIN
    assert dispatch_record.last_error_code == "unexpected_dispatch_error"
    assert len(dispatcher.admissions) == 1
    assert dispatcher.admissions[0].admitted is True
    assert dispatcher.admissions[0].message_id == persisted.id


class _DeliveryFailure(RuntimeError):
    pass


class _FailingResponseProcessor:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, response: HermesDispatchOutcome) -> None:
        assert response.assistant_content == "Hermes response"
        self.calls += 1
        raise _DeliveryFailure("delivery failed")


def test_delivery_failure_replay_does_not_redispatch_without_artifact(session: Session) -> None:
    allow_sender(session)
    message = normalized_message()
    dispatcher = _RecordingDispatcher(session)
    response_processor = _FailingResponseProcessor()
    relay = HermesResponseRelay(dispatcher, response_processor)
    service = MessageAdmissionService(session, hermes_dispatcher=relay)

    with pytest.raises(_DeliveryFailure, match="delivery failed"):
        service.process(message)

    replay = service.process(message)

    assert replay.hermes_dispatch is None
    assert len(dispatcher.admissions) == 1
    assert response_processor.calls == 1
    record = session.scalar(select(HermesDispatchRecord))
    assert record is not None
    assert record.status is HermesDispatchStatus.SUCCESS
