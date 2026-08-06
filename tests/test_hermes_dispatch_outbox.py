from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    HermesAPIError,
    HermesDispatchError,
    HermesDispatchOutboxExecutor,
    HermesDispatchOutcome,
    HermesResponseError,
    HermesResponseRelay,
    HermesTimeoutError,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.task.model import (
    HermesDispatchRecordStore,
    HermesDispatchStatus,
    build_hermes_dispatch_idempotency_key,
)
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace, ThreadType


def seed_dispatch_target(session: Session) -> None:
    identity = EnterpriseIdentity(id="identity-1", employee_id="employee-1")
    workspace = EmployeeWorkspace(
        id="workspace-1",
        enterprise_identity_id=identity.id,
    )
    thread = AIThread(
        id="thread-1",
        workspace_id=workspace.id,
        thread_type=ThreadType.PRIVATE,
        thread_key="v1:test:thread:one",
    )
    conversation = Conversation(
        source="test",
        source_account_id="bot-001",
        conversation_id="conversation-1",
        conversation_type="private",
    )
    message = Message(
        id=1,
        event_id="event-1",
        source=conversation.source,
        source_account_id=conversation.source_account_id,
        source_message_id="source-message-1",
        conversation_id=conversation.conversation_id,
        conversation_type=conversation.conversation_type,
        is_mentioned=None,
        is_self=False,
        sender_type="human",
        sender_id="sender-1",
        message_type="text",
        content="Dispatch this persisted text",
        timestamp=datetime(2026, 8, 6, 9, 30, tzinfo=UTC),
    )
    session.add(identity)
    session.flush()
    session.add(workspace)
    session.flush()
    session.add_all([thread, conversation])
    session.flush()
    session.add(message)
    session.commit()


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as database_session:
            seed_dispatch_target(database_session)
            yield database_session
    finally:
        engine.dispose()


def allowed_admission() -> AdmissionOutcome:
    return AdmissionOutcome(
        message_id=1,
        admitted=True,
        should_create_task=True,
        reason=AdmissionReason.ALLOWED,
        enterprise_identity_id="identity-1",
        workspace_id="workspace-1",
        ai_thread_id="thread-1",
    )


class _ControlledDispatcher:
    def __init__(self, store: HermesDispatchRecordStore, *, error: Exception | None = None) -> None:
        self._store = store
        self._error = error
        self.calls = 0

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        self.calls += 1
        record = self._store.get_by_idempotency_key(
            build_hermes_dispatch_idempotency_key(admission.message_id)
        )
        assert record is not None
        assert record.status is HermesDispatchStatus.RUNNING
        assert record.claim_token is not None
        assert record.attempt_count == 1
        if self._error is not None:
            raise self._error
        assert admission.workspace_id is not None
        assert admission.ai_thread_id is not None
        return HermesDispatchOutcome(
            message_id=admission.message_id,
            workspace_id=admission.workspace_id,
            ai_thread_id=admission.ai_thread_id,
            assistant_content="Hermes response",
        )


def test_inline_executor_claims_and_completes_dispatch(session: Session) -> None:
    store = HermesDispatchRecordStore(session)
    dispatcher = _ControlledDispatcher(store)

    outcome = HermesDispatchOutboxExecutor(
        session,
        dispatcher,
        record_store=store,
    ).dispatch(allowed_admission())

    record = store.get_by_idempotency_key(build_hermes_dispatch_idempotency_key(1))
    assert record is not None
    session.refresh(record)
    assert outcome.assistant_content == "Hermes response"
    assert dispatcher.calls == 1
    assert record.status is HermesDispatchStatus.SUCCESS
    assert record.attempt_count == 1
    assert record.claim_token is None
    assert record.claimed_at is not None
    assert record.completed_at is not None
    assert record.last_error_code is None


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            HermesDispatchError(reason="message_not_found"),
            HermesDispatchStatus.FAILED,
            "hermes_dispatch_error:message_not_found",
        ),
        (
            HermesDispatchError(reason="hermes_thread_advanced_concurrently"),
            HermesDispatchStatus.UNCERTAIN,
            "hermes_dispatch_error:hermes_thread_advanced_concurrently",
        ),
        (
            HermesAPIError(operation="chat_completion", status_code=400),
            HermesDispatchStatus.FAILED,
            "hermes_api_error:http_400",
        ),
        (
            HermesAPIError(operation="chat_completion", status_code=503),
            HermesDispatchStatus.UNCERTAIN,
            "hermes_api_error:http_503",
        ),
        (
            HermesTimeoutError(operation="chat_completion"),
            HermesDispatchStatus.UNCERTAIN,
            "hermes_timeout_error",
        ),
        (
            HermesResponseError(operation="chat_completion"),
            HermesDispatchStatus.UNCERTAIN,
            "hermes_response_error",
        ),
        (
            RuntimeError("unexpected dispatch failure"),
            HermesDispatchStatus.UNCERTAIN,
            "unexpected_dispatch_error",
        ),
    ],
)
def test_inline_executor_records_failure_certainty(
    session: Session,
    error: Exception,
    expected_status: HermesDispatchStatus,
    expected_code: str,
) -> None:
    store = HermesDispatchRecordStore(session)
    dispatcher = _ControlledDispatcher(store, error=error)

    with pytest.raises(type(error)) as exc_info:
        HermesDispatchOutboxExecutor(
            session,
            dispatcher,
            record_store=store,
        ).dispatch(allowed_admission())

    assert exc_info.value is error
    record = store.get_by_idempotency_key(build_hermes_dispatch_idempotency_key(1))
    assert record is not None
    session.refresh(record)
    assert record.status is expected_status
    assert record.last_error_code == expected_code
    assert record.completed_at is not None


class _FailedTransactionDispatcher:
    def __init__(self, session: Session, store: HermesDispatchRecordStore) -> None:
        self._session = session
        self._store = store

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        record = self._store.get_by_idempotency_key(
            build_hermes_dispatch_idempotency_key(admission.message_id)
        )
        assert record is not None
        record.attempt_count = -1
        self._session.commit()
        raise AssertionError("the database constraint must reject the invalid attempt count")


def test_inline_executor_recovers_session_before_recording_failure(session: Session) -> None:
    store = HermesDispatchRecordStore(session)
    dispatcher = _FailedTransactionDispatcher(session, store)

    with pytest.raises(IntegrityError):
        HermesDispatchOutboxExecutor(
            session,
            dispatcher,
            record_store=store,
        ).dispatch(allowed_admission())

    record = store.get_by_idempotency_key(build_hermes_dispatch_idempotency_key(1))
    assert record is not None
    session.refresh(record)
    assert record.status is HermesDispatchStatus.UNCERTAIN
    assert record.attempt_count == 1
    assert record.last_error_code == "unexpected_dispatch_error"


class _DeliveryFailure(RuntimeError):
    pass


class _FailingResponseProcessor:
    def handle(self, response: HermesDispatchOutcome) -> None:
        assert response.assistant_content == "Hermes response"
        raise _DeliveryFailure("outbound delivery failed")


def test_delivery_failure_does_not_change_successful_dispatch(session: Session) -> None:
    store = HermesDispatchRecordStore(session)
    dispatcher = _ControlledDispatcher(store)
    relay = HermesResponseRelay(
        HermesDispatchOutboxExecutor(session, dispatcher, record_store=store),
        _FailingResponseProcessor(),
    )

    with pytest.raises(_DeliveryFailure, match="outbound delivery failed"):
        relay.dispatch(allowed_admission())

    record = store.get_by_idempotency_key(build_hermes_dispatch_idempotency_key(1))
    assert record is not None
    session.refresh(record)
    assert record.status is HermesDispatchStatus.SUCCESS
    assert record.last_error_code is None
