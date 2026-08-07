from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    DispatchClaim,
    HermesDeliveryError,
    HermesDispatchError,
    HermesDispatchOutcome,
    HermesDispatchResponse,
    HermesDispatchWorker,
    HermesTimeoutError,
    ResponseEnvelope,
    TextPart,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchStateConflictError,
    HermesDispatchStatus,
)
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace, ThreadType


@dataclass(frozen=True, slots=True)
class ThreadSeed:
    identity_id: str
    workspace_id: str
    thread_id: str
    source_account_id: str
    conversation_id: str


class ControlledDispatcher:
    def __init__(
        self,
        *,
        failures: dict[int, Exception] | None = None,
        barrier: Barrier | None = None,
    ) -> None:
        self.failures = failures if failures is not None else {}
        self.barrier = barrier
        self.calls: list[tuple[int, str]] = []
        self._lock = Lock()

    def dispatch_record(self, record: HermesDispatchRecord) -> HermesDispatchOutcome:
        with self._lock:
            self.calls.append((record.id, record.idempotency_key))
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        error = self.failures.get(record.message_id)
        if error is not None:
            raise error
        envelope = ResponseEnvelope(
            response_id=f"response-{record.id}",
            parts=(TextPart(text=f"answer-{record.message_id}"),),
        )
        return HermesDispatchOutcome.from_response(
            message_id=record.message_id,
            workspace_id=record.workspace_id,
            ai_thread_id=record.ai_thread_id,
            response=envelope,
        )


class PersistedResponseObserver:
    def __init__(self, session: Session, observations: list[tuple[int, int]]) -> None:
        self._session = session
        self._observations = observations

    def handle(self, response: HermesDispatchOutcome) -> None:
        record = self._session.scalar(
            select(HermesDispatchRecord).where(
                HermesDispatchRecord.message_id == response.message_id
            )
        )
        assert record is not None
        persisted = self._session.scalar(
            select(HermesDispatchResponse).where(
                HermesDispatchResponse.dispatch_record_id == record.id
            )
        )
        assert persisted is not None
        assert record.status is HermesDispatchStatus.SUCCESS
        self._observations.append((record.id, persisted.id))


class FailingResponseProcessor(PersistedResponseObserver):
    def handle(self, response: HermesDispatchOutcome) -> None:
        super().handle(response)
        raise HermesDeliveryError(reason="controlled_delivery_failure")


def database_factory(tmp_path: Path) -> tuple[sessionmaker[Session], object]:
    database_path = tmp_path / "dispatch-worker.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    return create_database_session_factory(engine), engine


def create_thread(session: Session, suffix: str) -> ThreadSeed:
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
        thread_key=f"v1:test:dispatch-worker:{suffix}",
    )
    conversation = Conversation(
        source="test",
        source_account_id=f"account-{suffix}",
        conversation_id=f"conversation-{suffix}",
        conversation_type="private",
    )
    session.add(identity)
    session.flush()
    session.add(workspace)
    session.flush()
    session.add(thread)
    session.flush()
    session.add(conversation)
    session.commit()
    return ThreadSeed(
        identity_id=identity.id,
        workspace_id=workspace.id,
        thread_id=thread.id,
        source_account_id=conversation.source_account_id,
        conversation_id=conversation.conversation_id,
    )


def enqueue_message(
    session: Session,
    seed: ThreadSeed,
    suffix: str,
) -> HermesDispatchRecord:
    message = Message(
        event_id=f"event-{suffix}",
        source="test",
        source_account_id=seed.source_account_id,
        source_message_id=f"source-message-{suffix}",
        conversation_id=seed.conversation_id,
        conversation_type="private",
        is_mentioned=None,
        is_self=False,
        sender_type="human",
        sender_id=f"sender-{suffix}",
        message_type="text",
        content=f"question-{suffix}",
        timestamp=datetime.now(UTC),
    )
    session.add(message)
    session.commit()
    record, created = HermesDispatchRecordStore(session).enqueue(
        AdmissionOutcome(
            message_id=message.id,
            admitted=True,
            should_create_task=True,
            reason=AdmissionReason.ALLOWED,
            enterprise_identity_id=seed.identity_id,
            workspace_id=seed.workspace_id,
            ai_thread_id=seed.thread_id,
        )
    )
    assert created is True
    return record


def make_worker(
    factory: sessionmaker[Session],
    dispatcher: ControlledDispatcher,
    *,
    lease_seconds: float = 5,
    retry_limit: int = 2,
    observations: list[tuple[int, int]] | None = None,
) -> HermesDispatchWorker:
    return HermesDispatchWorker(
        factory,
        lambda session: dispatcher,
        lease_seconds=lease_seconds,
        retry_limit=retry_limit,
        response_processor_factory=(
            (lambda session: PersistedResponseObserver(session, observations))
            if observations is not None
            else None
        ),
    )


def test_multiple_workers_compete_with_one_cas_winner(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            enqueue_message(session, create_thread(session, "one"), "one")
        workers = (
            make_worker(factory, ControlledDispatcher()),
            make_worker(factory, ControlledDispatcher()),
        )
        barrier = Barrier(2)

        def claim(worker: HermesDispatchWorker) -> DispatchClaim | None:
            barrier.wait(timeout=5)
            return worker.claim_once()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, workers))

        assert sum(claim is not None for claim in claims) == 1
        with factory() as session:
            record = session.scalar(select(HermesDispatchRecord))
            assert record is not None
            assert record.status is HermesDispatchStatus.RUNNING
            assert record.attempt_count == 1
    finally:
        engine.dispose()


def test_fifo_blocks_same_thread_but_allows_parallel_threads(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            first_seed = create_thread(session, "fifo")
            first = enqueue_message(session, first_seed, "fifo-one")
            second = enqueue_message(session, first_seed, "fifo-two")
            other = enqueue_message(session, create_thread(session, "parallel"), "parallel")

        barrier = Barrier(2)
        dispatcher = ControlledDispatcher(barrier=barrier)
        worker = make_worker(factory, dispatcher)
        first_claim = worker.claim_once()
        other_claim = worker.claim_once()
        assert first_claim is not None
        assert other_claim is not None
        assert {first_claim.record_id, other_claim.record_id} == {first.id, other.id}
        assert worker.claim_once() is None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker.process_claim, (first_claim, other_claim)))
        assert {result.status for result in results} == {HermesDispatchStatus.SUCCESS}

        second_claim = worker.claim_once()
        assert second_claim is not None
        assert second_claim.record_id == second.id
    finally:
        engine.dispose()


def test_expired_lease_is_reclaimed_with_new_token(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            enqueue_message(session, create_thread(session, "crash"), "crash")
        dispatcher = ControlledDispatcher()
        worker = make_worker(
            factory,
            dispatcher,
            lease_seconds=1,
            retry_limit=2,
        )
        now = datetime.now(UTC)
        first = worker.claim_once(now=now)
        assert first is not None
        assert worker.claim_once(now=now + timedelta(milliseconds=500)) is None

        recovered = worker.claim_once(now=now + timedelta(seconds=2))
        assert recovered is not None
        assert recovered.record_id == first.record_id
        assert recovered.claim_token != first.claim_token
        assert recovered.attempt_count == 2

        with pytest.raises(HermesDispatchStateConflictError):
            worker.process_claim(first)
        assert dispatcher.calls == []

        result = worker.process_claim(recovered)
        assert result.status is HermesDispatchStatus.SUCCESS
        assert dispatcher.calls == [(recovered.record_id, recovered.idempotency_key)]
    finally:
        engine.dispose()


def test_retry_limit_moves_definite_failures_to_dead(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            record = enqueue_message(session, create_thread(session, "retry"), "retry")
        failure = HermesDispatchError(reason="temporary_failure")
        dispatcher = ControlledDispatcher(failures={record.message_id: failure})
        worker = make_worker(factory, dispatcher, retry_limit=1)

        first = worker.run_once()
        second = worker.run_once()

        assert first is not None
        assert first.status is HermesDispatchStatus.FAILED
        assert second is not None
        assert second.status is HermesDispatchStatus.DEAD
        assert worker.run_once() is None
        with factory() as session:
            persisted = session.get(HermesDispatchRecord, record.id)
            assert persisted is not None
            assert persisted.attempt_count == 2
    finally:
        engine.dispose()


def test_uncertain_blocks_its_thread_without_blocking_other_threads(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            blocked_seed = create_thread(session, "uncertain")
            uncertain = enqueue_message(session, blocked_seed, "uncertain-one")
            enqueue_message(session, blocked_seed, "uncertain-two")
            other = enqueue_message(session, create_thread(session, "available"), "available")
        dispatcher = ControlledDispatcher(
            failures={
                uncertain.message_id: HermesTimeoutError(operation="chat_completion"),
            }
        )
        worker = make_worker(factory, dispatcher)

        first = worker.run_once()
        second = worker.run_once()

        assert first is not None
        assert first.status is HermesDispatchStatus.UNCERTAIN
        assert second is not None
        assert second.record_id == other.id
        assert second.status is HermesDispatchStatus.SUCCESS
        assert worker.claim_once() is None
    finally:
        engine.dispose()


def test_response_is_committed_before_delivery_pipeline(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            record = enqueue_message(session, create_thread(session, "response"), "response")
        observations: list[tuple[int, int]] = []
        dispatcher = ControlledDispatcher()
        worker = make_worker(factory, dispatcher, observations=observations)

        result = worker.run_once()

        assert result is not None
        assert result.status is HermesDispatchStatus.SUCCESS
        assert result.response_id is not None
        assert observations == [(record.id, result.response_id)]
        assert dispatcher.calls == [(record.id, record.idempotency_key)]
    finally:
        engine.dispose()


def test_delivery_failure_does_not_redispatch_persisted_response(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            record = enqueue_message(session, create_thread(session, "delivery"), "delivery")
        observations: list[tuple[int, int]] = []
        dispatcher = ControlledDispatcher()
        worker = HermesDispatchWorker(
            factory,
            lambda session: dispatcher,
            lease_seconds=5,
            retry_limit=2,
            response_processor_factory=lambda session: FailingResponseProcessor(
                session,
                observations,
            ),
        )

        result = worker.run_once()

        assert result is not None
        assert result.status is HermesDispatchStatus.SUCCESS
        assert result.response_id is not None
        assert result.delivery_error_code == "hermes_delivery_error"
        assert observations == [(record.id, result.response_id)]
        assert worker.run_once() is None
        assert dispatcher.calls == [(record.id, record.idempotency_key)]
        with factory() as session:
            persisted = session.get(HermesDispatchRecord, record.id)
            assert persisted is not None
            assert persisted.status is HermesDispatchStatus.SUCCESS
            assert (
                session.scalar(
                    select(HermesDispatchResponse).where(
                        HermesDispatchResponse.dispatch_record_id == record.id
                    )
                )
                is not None
            )
    finally:
        engine.dispose()


def test_worker_does_not_mutate_message_archive(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            record = enqueue_message(session, create_thread(session, "archive"), "archive")
            before = dict(
                session.execute(select(Message.__table__).where(Message.id == record.message_id))
                .mappings()
                .one()
            )

        worker = make_worker(factory, ControlledDispatcher())
        result = worker.run_once()

        assert result is not None
        assert result.status is HermesDispatchStatus.SUCCESS
        with factory() as session:
            after = dict(
                session.execute(select(Message.__table__).where(Message.id == record.message_id))
                .mappings()
                .one()
            )
        assert after == before
    finally:
        engine.dispose()


def test_expired_claim_is_rejected_before_hermes_execution(tmp_path: Path) -> None:
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            enqueue_message(session, create_thread(session, "expired"), "expired")
        dispatcher = ControlledDispatcher()
        worker = make_worker(factory, dispatcher, lease_seconds=1)
        claim = worker.claim_once(now=datetime.now(UTC) - timedelta(seconds=2))
        assert claim is not None

        with pytest.raises(HermesDispatchStateConflictError):
            worker.process_claim(claim)

        assert dispatcher.calls == []
    finally:
        engine.dispose()
