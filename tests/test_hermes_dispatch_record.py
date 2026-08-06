from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.task.model import (
    HermesDispatchAdmissionError,
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchStateConflictError,
    HermesDispatchStatus,
    HermesDispatchTargetConflictError,
    build_hermes_dispatch_idempotency_key,
)
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace, ThreadType


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


def create_allowed_admission(
    session: Session,
    *,
    suffix: str = "one",
) -> AdmissionOutcome:
    identity = EnterpriseIdentity(
        id=f"identity-{suffix}",
        employee_id=f"employee-{suffix}",
    )
    workspace = EmployeeWorkspace(
        id=f"workspace-{suffix}",
        enterprise_identity_id=identity.id,
    )
    thread = AIThread(
        id=f"thread-{suffix}",
        workspace_id=workspace.id,
        thread_type=ThreadType.PRIVATE,
        thread_key=f"v1:test:thread:{suffix}",
    )
    conversation = Conversation(
        source="test",
        source_account_id="bot-001",
        conversation_id=f"conversation-{suffix}",
        conversation_type="private",
    )
    message = Message(
        event_id=f"event-{suffix}",
        source=conversation.source,
        source_account_id=conversation.source_account_id,
        source_message_id=f"source-message-{suffix}",
        conversation_id=conversation.conversation_id,
        conversation_type=conversation.conversation_type,
        is_mentioned=None,
        is_self=False,
        sender_type="human",
        sender_id=f"sender-{suffix}",
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
    return AdmissionOutcome(
        message_id=message.id,
        admitted=True,
        should_create_task=True,
        reason=AdmissionReason.ALLOWED,
        enterprise_identity_id=identity.id,
        workspace_id=workspace.id,
        ai_thread_id=thread.id,
    )


def test_enqueue_persists_default_queued_record(session: Session) -> None:
    admission = create_allowed_admission(session)

    record, created = HermesDispatchRecordStore(session).enqueue(admission)

    assert created is True
    assert record.idempotency_key == build_hermes_dispatch_idempotency_key(admission.message_id)
    assert record.message_id == admission.message_id
    assert record.enterprise_identity_id == admission.enterprise_identity_id
    assert record.workspace_id == admission.workspace_id
    assert record.ai_thread_id == admission.ai_thread_id
    assert record.status is HermesDispatchStatus.QUEUED
    assert record.attempt_count == 0
    assert record.claim_token is None
    assert record.claimed_at is None
    assert record.completed_at is None
    assert record.last_error_code is None
    assert record.created_at is not None
    assert record.updated_at is not None

    session.expire_all()
    persisted = session.get(HermesDispatchRecord, record.id)
    assert persisted is not None
    assert persisted.idempotency_key == record.idempotency_key
    assert persisted.status is HermesDispatchStatus.QUEUED


def test_enqueue_is_idempotent_and_preserves_existing_state(session: Session) -> None:
    admission = create_allowed_admission(session)
    store = HermesDispatchRecordStore(session)
    first, first_created = store.enqueue(admission)
    running = store.claim(first.id, claim_token="worker-attempt-one")

    duplicate, duplicate_created = store.enqueue(admission)

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert duplicate.status is HermesDispatchStatus.RUNNING
    assert duplicate.claim_token == running.claim_token
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_enqueue_rejects_same_key_for_a_different_target(session: Session) -> None:
    admission = create_allowed_admission(session)
    store = HermesDispatchRecordStore(session)
    existing, _ = store.enqueue(admission)
    conflicting = AdmissionOutcome(
        message_id=admission.message_id,
        admitted=True,
        should_create_task=True,
        reason=AdmissionReason.ALLOWED,
        enterprise_identity_id=admission.enterprise_identity_id,
        workspace_id="workspace-different",
        ai_thread_id="thread-different",
    )

    with pytest.raises(HermesDispatchTargetConflictError) as exc_info:
        store.enqueue(conflicting)

    assert exc_info.value.existing_record_id == existing.id
    assert exc_info.value.idempotency_key == existing.idempotency_key
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_database_rejects_second_dispatch_for_same_message(session: Session) -> None:
    admission = create_allowed_admission(session)
    existing, _ = HermesDispatchRecordStore(session).enqueue(admission)
    duplicate = HermesDispatchRecord(
        idempotency_key="v1:hermes-chat:manual-duplicate",
        message_id=existing.message_id,
        enterprise_identity_id=existing.enterprise_identity_id,
        workspace_id=existing.workspace_id,
        ai_thread_id=existing.ai_thread_id,
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


@pytest.mark.parametrize(
    "admission, reason",
    [
        (
            AdmissionOutcome(
                message_id=1,
                admitted=False,
                should_create_task=False,
                reason=AdmissionReason.ACCESS_DENIED,
            ),
            "admission_not_allowed",
        ),
        (
            AdmissionOutcome(
                message_id=1,
                admitted=True,
                should_create_task=False,
                reason=AdmissionReason.ALLOWED,
                enterprise_identity_id="identity",
                workspace_id="workspace",
                ai_thread_id="thread",
            ),
            "task_not_requested",
        ),
        (
            AdmissionOutcome(
                message_id=1,
                admitted=True,
                should_create_task=True,
                reason=AdmissionReason.ALLOWED,
                enterprise_identity_id=None,
                workspace_id="workspace",
                ai_thread_id="thread",
            ),
            "enterprise_identity_missing",
        ),
    ],
)
def test_enqueue_rejects_invalid_admission(
    session: Session,
    admission: AdmissionOutcome,
    reason: str,
) -> None:
    with pytest.raises(HermesDispatchAdmissionError) as exc_info:
        HermesDispatchRecordStore(session).enqueue(admission)

    assert exc_info.value.reason == reason


def test_concurrent_enqueue_creates_one_record(tmp_path: Path) -> None:
    database_path = tmp_path / "dispatch-enqueue.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    barrier = Barrier(2)

    class BarrierStore(HermesDispatchRecordStore):
        def __init__(self, database_session: Session) -> None:
            super().__init__(database_session)
            self._lookups = 0

        def get_by_idempotency_key(self, idempotency_key: str) -> HermesDispatchRecord | None:
            record = super().get_by_idempotency_key(idempotency_key)
            self._lookups += 1
            if self._lookups == 1:
                assert record is None
                barrier.wait(timeout=5)
            return record

    try:
        with factory() as setup_session:
            admission = create_allowed_admission(setup_session)

        def enqueue() -> tuple[int, bool]:
            with factory() as worker_session:
                record, created = BarrierStore(worker_session).enqueue(admission)
                return record.id, created

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: enqueue(), range(2)))

        assert sorted(created for _, created in results) == [False, True]
        assert len({record_id for record_id, _ in results}) == 1
        with factory() as verification_session:
            assert (
                verification_session.scalar(select(func.count()).select_from(HermesDispatchRecord))
                == 1
            )
    finally:
        engine.dispose()


def test_claim_is_compare_and_swap_and_increments_attempt(session: Session) -> None:
    admission = create_allowed_admission(session)
    store = HermesDispatchRecordStore(session)
    queued, _ = store.enqueue(admission)

    claimed = store.claim(queued.id, claim_token="worker-attempt-one")

    assert claimed.status is HermesDispatchStatus.RUNNING
    assert claimed.attempt_count == 1
    assert claimed.claim_token == "worker-attempt-one"
    assert claimed.claimed_at is not None
    assert claimed.completed_at is None

    with pytest.raises(HermesDispatchStateConflictError) as exc_info:
        store.claim(queued.id, claim_token="worker-attempt-two")

    assert exc_info.value.record_id == queued.id
    assert exc_info.value.expected_status is HermesDispatchStatus.QUEUED
    session.expire_all()
    persisted = session.get(HermesDispatchRecord, queued.id)
    assert persisted is not None
    assert persisted.claim_token == "worker-attempt-one"
    assert persisted.attempt_count == 1


def test_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    database_path = tmp_path / "dispatch-claim.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    barrier = Barrier(2)
    try:
        with factory() as setup_session:
            admission = create_allowed_admission(setup_session)
            queued, _ = HermesDispatchRecordStore(setup_session).enqueue(admission)
            record_id = queued.id

        def claim(claim_token: str) -> str | None:
            with factory() as worker_session:
                barrier.wait(timeout=5)
                try:
                    HermesDispatchRecordStore(worker_session).claim(
                        record_id,
                        claim_token=claim_token,
                    )
                except HermesDispatchStateConflictError:
                    return None
                return claim_token

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("worker-one", "worker-two")))

        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        with factory() as verification_session:
            record = verification_session.get(HermesDispatchRecord, record_id)
            assert record is not None
            assert record.status is HermesDispatchStatus.RUNNING
            assert record.attempt_count == 1
            assert record.claim_token == winners[0]
    finally:
        engine.dispose()


def test_stale_claim_token_cannot_complete_running_record(session: Session) -> None:
    admission = create_allowed_admission(session)
    store = HermesDispatchRecordStore(session)
    queued, _ = store.enqueue(admission)
    store.claim(queued.id, claim_token="current-worker")

    with pytest.raises(HermesDispatchStateConflictError) as exc_info:
        store.mark_success(queued.id, claim_token="stale-worker")

    assert exc_info.value.expected_status is HermesDispatchStatus.RUNNING
    persisted = store.get(queued.id)
    assert persisted is not None
    session.refresh(persisted)
    assert persisted.status is HermesDispatchStatus.RUNNING
    assert persisted.claim_token == "current-worker"


@pytest.mark.parametrize(
    ("terminal_status", "error_code"),
    [
        (HermesDispatchStatus.SUCCESS, None),
        (HermesDispatchStatus.FAILED, "hermes_api_error"),
        (HermesDispatchStatus.UNCERTAIN, "hermes_timeout"),
    ],
)
def test_running_record_reaches_terminal_state_once(
    session: Session,
    terminal_status: HermesDispatchStatus,
    error_code: str | None,
) -> None:
    admission = create_allowed_admission(session, suffix=terminal_status.value)
    store = HermesDispatchRecordStore(session)
    queued, _ = store.enqueue(admission)
    store.claim(queued.id, claim_token="worker-token")

    if terminal_status is HermesDispatchStatus.SUCCESS:
        completed = store.mark_success(queued.id, claim_token="worker-token")
    elif terminal_status is HermesDispatchStatus.FAILED:
        assert error_code is not None
        completed = store.mark_failed(
            queued.id,
            claim_token="worker-token",
            error_code=error_code,
        )
    else:
        assert error_code is not None
        completed = store.mark_uncertain(
            queued.id,
            claim_token="worker-token",
            error_code=error_code,
        )

    assert completed.status is terminal_status
    assert completed.attempt_count == 1
    assert completed.claim_token is None
    assert completed.claimed_at is not None
    assert completed.completed_at is not None
    assert completed.last_error_code == error_code

    with pytest.raises(HermesDispatchStateConflictError):
        store.mark_success(queued.id, claim_token="worker-token")


def test_database_enforces_status_attempt_and_state_constraints(session: Session) -> None:
    admission = create_allowed_admission(session)
    record, _ = HermesDispatchRecordStore(session).enqueue(admission)

    for assignment in (
        "status = 'not-a-status'",
        "attempt_count = -1",
        "attempt_count = 1",
        "status = 'running'",
        "status = 'running', attempt_count = 1, claim_token = '   ', "
        "claimed_at = CURRENT_TIMESTAMP",
        "status = 'success', completed_at = CURRENT_TIMESTAMP",
    ):
        with pytest.raises(IntegrityError):
            session.execute(
                text(f"UPDATE hermes_dispatch_records SET {assignment} WHERE id = :record_id"),
                {"record_id": record.id},
            )
            session.commit()
        session.rollback()


def test_database_initialization_creates_dispatch_indexes(session: Session) -> None:
    indexes = {
        index["name"]
        for index in inspect(session.get_bind()).get_indexes("hermes_dispatch_records")
    }

    assert "ix_hermes_dispatch_queue" in indexes
    assert "ix_hermes_dispatch_thread_queue" in indexes
    assert "ix_hermes_dispatch_records_message_id" in indexes


@pytest.mark.parametrize("message_id", [0, -1, True, "1"])
def test_idempotency_key_requires_positive_integer_message_id(message_id: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        build_hermes_dispatch_idempotency_key(message_id)  # type: ignore[arg-type]
