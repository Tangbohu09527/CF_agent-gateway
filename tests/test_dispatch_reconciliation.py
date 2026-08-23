from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
from cf_agent_gateway.hermes.worker import HermesDispatchWorker
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.response.runtime import ResponsePersistenceProcessor
from cf_agent_gateway.response.store import DeliveryTarget, ResponseStore
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService


@dataclass(frozen=True, slots=True)
class PersistedSuccess:
    record_id: int
    message_id: int
    outcome: HermesDispatchOutcome


class NoHermesDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch_record(self, record: HermesDispatchRecord) -> HermesDispatchOutcome:
        del record
        self.calls += 1
        raise AssertionError("reconciliation must not call Hermes")


def _database_factory(tmp_path: Path, name: str) -> tuple[sessionmaker[Session], object]:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / name}")
    initialize_database(engine)
    return create_database_session_factory(engine), engine


def _persist_raw_success(
    factory: sessionmaker[Session],
    suffix: str,
) -> PersistedSuccess:
    with factory() as session:
        identity = IdentityService(session).create_identity(employee_id=f"employee-{suffix}")
        thread = WorkspaceService(session).ensure_thread_for_authorized_request(
            enterprise_identity_id=identity.id,
            platform="wechat",
            account_id=f"account-{suffix}",
            physical_conversation_id=f"conversation-{suffix}",
            conversation_type="private",
            sender_id=f"sender-{suffix}",
        )
        workspace = session.get(EmployeeWorkspace, thread.workspace_id)
        assert workspace is not None
        message, created = MessageStore(session).create(
            MessageEvent(
                event_id=f"event-{suffix}",
                source="wechat",
                source_account_id=f"account-{suffix}",
                source_message_id=f"source-{suffix}",
                conversation_id=f"conversation-{suffix}",
                conversation_type="private",
                is_mentioned=None,
                is_self=False,
                sender_type="human",
                sender_id=f"sender-{suffix}",
                sender_name="Reconciliation test",
                message_type="text",
                content="persist the response without redispatch",
                timestamp=datetime(2026, 8, 23, 2, 0, tzinfo=UTC),
            )
        )
        assert created is True
        record, enqueued = HermesDispatchRecordStore(session).enqueue(
            AdmissionOutcome(
                message_id=message.id,
                admitted=True,
                should_create_task=True,
                reason=AdmissionReason.ALLOWED,
                enterprise_identity_id=identity.id,
                workspace_id=workspace.id,
                ai_thread_id=thread.id,
            )
        )
        assert enqueued is True
        claimed = HermesDispatchRecordStore(session).claim(
            record.id,
            claim_token=f"claim-{suffix}",
        )
        outcome = HermesDispatchOutcome(
            message_id=message.id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            assistant_content=f"persisted response {suffix}",
        )
        HermesDispatchResponseStore(session).complete_success(
            claimed.id,
            claim_token=f"claim-{suffix}",
            outcome=outcome,
        )
        return PersistedSuccess(
            record_id=record.id,
            message_id=message.id,
            outcome=outcome,
        )


def _worker(
    factory: sessionmaker[Session],
    dispatcher: NoHermesDispatcher,
) -> HermesDispatchWorker:
    return HermesDispatchWorker(
        factory,
        lambda session: dispatcher,
        lease_seconds=10,
        retry_limit=1,
        response_processor_factory=lambda session: ResponsePersistenceProcessor(session),
        reconcile_persisted_responses=True,
    )


def test_run_once_reconciles_raw_success_without_calling_hermes(tmp_path: Path) -> None:
    factory, engine = _database_factory(tmp_path, "raw-success.db")
    try:
        persisted = _persist_raw_success(factory, "raw")
        dispatcher = NoHermesDispatcher()

        result = _worker(factory, dispatcher).run_once()

        assert result is None
        assert dispatcher.calls == 0
        with factory() as session:
            response = session.scalar(
                select(ResponseRecord).where(ResponseRecord.message_id == persisted.message_id)
            )
            delivery = session.scalar(select(DeliveryOutboxRecord))
            assert response is not None
            assert delivery is not None
            assert delivery.response_id == response.response_id
            assert session.scalar(select(func.count()).select_from(HermesDispatchResponse)) == 1
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
    finally:
        engine.dispose()


def test_reconciliation_requires_explicit_capability(tmp_path: Path) -> None:
    factory, engine = _database_factory(tmp_path, "explicit-reconciliation.db")
    try:
        _persist_raw_success(factory, "explicit")
        worker = HermesDispatchWorker(
            factory,
            lambda session: NoHermesDispatcher(),
            lease_seconds=10,
            retry_limit=1,
            response_processor_factory=ResponsePersistenceProcessor,
        )

        assert worker.reconcile_once() is False
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 0
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 0
    finally:
        engine.dispose()


def test_existing_response_missing_outbox_is_repaired_once(tmp_path: Path) -> None:
    factory, engine = _database_factory(tmp_path, "missing-outbox.db")
    try:
        persisted = _persist_raw_success(factory, "missing")
        with factory() as session:
            message = session.get(Message, persisted.message_id)
            assert message is not None
            _, delivery, _ = ResponseStore(session).save_generated(
                persisted.outcome,
                target=DeliveryTarget(
                    channel=message.source,
                    account_id=message.source_account_id,
                    conversation_id=message.conversation_id,
                ),
            )
            session.delete(delivery)
            session.commit()

        dispatcher = NoHermesDispatcher()
        worker = _worker(factory, dispatcher)
        assert worker.run_once() is None
        assert worker.run_once() is None

        assert dispatcher.calls == 0
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
    finally:
        engine.dispose()


def test_poison_response_does_not_starve_later_reconciliation_candidate(
    tmp_path: Path,
    caplog,
    monkeypatch,
) -> None:
    factory, engine = _database_factory(tmp_path, "poison-fairness.db")
    try:
        poison = _persist_raw_success(factory, "poison")
        valid = _persist_raw_success(factory, "valid")
        with factory() as session:
            raw_response = session.scalar(
                select(HermesDispatchResponse).where(
                    HermesDispatchResponse.dispatch_record_id == poison.record_id
                )
            )
            assert raw_response is not None
            raw_response.response_payload = {"invalid": "envelope"}
            session.commit()

        monkeypatch.setattr(
            "cf_agent_gateway.hermes.worker.RECONCILIATION_BATCH_SIZE",
            1,
        )
        dispatcher = NoHermesDispatcher()
        worker = _worker(factory, dispatcher)
        with caplog.at_level(logging.WARNING):
            assert worker.reconcile_once() is False
            assert worker.reconcile_once() is True

        assert dispatcher.calls == 0
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ResponseRecord)
                    .where(ResponseRecord.message_id == poison.message_id)
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ResponseRecord)
                    .where(ResponseRecord.message_id == valid.message_id)
                )
                == 1
            )
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
        poison_logs = [
            record
            for record in caplog.records
            if getattr(record, "fields", {}).get("error_code")
            == "dispatch_reconciliation_candidate_invalid"
        ]
        assert len(poison_logs) == 1
        assert poison_logs[0].fields["dispatch_record_id"] == poison.record_id
        assert poison_logs[0].fields["failure_count"] == 1
        assert poison_logs[0].fields["recovery_action"] == "candidate_backoff"
        assert "persist the response" not in caplog.text
    finally:
        engine.dispose()


def test_reconciliation_backoff_survives_worker_restart_and_recovers_when_due(
    tmp_path: Path,
    caplog,
) -> None:
    factory, engine = _database_factory(tmp_path, "persistent-backoff.db")
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    try:
        persisted = _persist_raw_success(factory, "persistent-backoff")
        with factory() as session:
            raw_response = session.scalar(
                select(HermesDispatchResponse).where(
                    HermesDispatchResponse.dispatch_record_id == persisted.record_id
                )
            )
            assert raw_response is not None
            original_payload = raw_response.response_payload
            raw_response.response_payload = {"invalid": "envelope"}
            session.commit()

        first_worker = _worker(factory, NoHermesDispatcher())
        with caplog.at_level(logging.WARNING):
            assert first_worker.reconcile_once(now=now) is False
        with factory() as session:
            record = session.get(HermesDispatchRecord, persisted.record_id)
            assert record is not None
            assert record.reconciliation_failure_count == 1
            assert record.reconciliation_next_attempt_at is not None
            due_at = record.reconciliation_next_attempt_at

        restarted_worker = _worker(factory, NoHermesDispatcher())
        assert restarted_worker.reconcile_once(now=now + timedelta(seconds=1)) is False
        with factory() as session:
            record = session.get(HermesDispatchRecord, persisted.record_id)
            assert record is not None
            assert record.reconciliation_failure_count == 1
            raw_response = session.scalar(
                select(HermesDispatchResponse).where(
                    HermesDispatchResponse.dispatch_record_id == persisted.record_id
                )
            )
            assert raw_response is not None
            raw_response.response_payload = original_payload
            session.commit()

        assert restarted_worker.reconcile_once(
            now=due_at.replace(tzinfo=UTC) if due_at.tzinfo is None else due_at
        )
        with factory() as session:
            record = session.get(HermesDispatchRecord, persisted.record_id)
            assert record is not None
            assert record.reconciliation_failure_count == 0
            assert record.reconciliation_next_attempt_at is None
            assert record.reconciliation_quarantined_at is None
            assert record.reconciliation_last_error_code is None
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
    finally:
        engine.dispose()


def test_reconciliation_quarantines_candidate_after_bounded_failures(
    tmp_path: Path,
    caplog,
) -> None:
    factory, engine = _database_factory(tmp_path, "quarantine.db")
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    try:
        persisted = _persist_raw_success(factory, "quarantine")
        with factory() as session:
            raw_response = session.scalar(
                select(HermesDispatchResponse).where(
                    HermesDispatchResponse.dispatch_record_id == persisted.record_id
                )
            )
            assert raw_response is not None
            raw_response.response_payload = {"invalid": "envelope"}
            session.commit()

        worker = _worker(factory, NoHermesDispatcher())
        with caplog.at_level(logging.WARNING):
            for expected_failure_count in range(1, 6):
                assert worker.reconcile_once(now=now) is False
                with factory() as session:
                    record = session.get(HermesDispatchRecord, persisted.record_id)
                    assert record is not None
                    assert record.reconciliation_failure_count == expected_failure_count
                    if expected_failure_count < 5:
                        assert record.reconciliation_next_attempt_at is not None
                        assert record.reconciliation_quarantined_at is None
                        next_attempt = record.reconciliation_next_attempt_at
                        now = (
                            next_attempt.replace(tzinfo=UTC)
                            if next_attempt.tzinfo is None
                            else next_attempt
                        )
                    else:
                        assert record.reconciliation_next_attempt_at is None
                        assert record.reconciliation_quarantined_at is not None

        assert worker.reconcile_once(now=now + timedelta(days=1)) is False
        candidate_logs = [
            record
            for record in caplog.records
            if getattr(record, "fields", {}).get("error_code")
            == "dispatch_reconciliation_candidate_invalid"
        ]
        assert len(candidate_logs) == 5
        assert candidate_logs[-1].fields["recovery_action"] == "candidate_quarantined"
    finally:
        engine.dispose()


def test_resident_reconciliation_scan_and_backoff_prevent_log_storm(
    tmp_path: Path,
    caplog,
) -> None:
    factory, engine = _database_factory(tmp_path, "bounded-resident.db")
    stop_event = Event()
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    ticks = 0

    def monotonic_clock() -> float:
        nonlocal ticks
        ticks += 1
        if ticks >= 100:
            stop_event.set()
        return ticks / 10

    try:
        persisted = _persist_raw_success(factory, "bounded-resident")
        with factory() as session:
            raw_response = session.scalar(
                select(HermesDispatchResponse).where(
                    HermesDispatchResponse.dispatch_record_id == persisted.record_id
                )
            )
            assert raw_response is not None
            raw_response.response_payload = {"invalid": "envelope"}
            session.commit()
        worker = HermesDispatchWorker(
            factory,
            lambda session: NoHermesDispatcher(),
            lease_seconds=10,
            retry_limit=1,
            response_processor_factory=lambda session: ResponsePersistenceProcessor(session),
            reconcile_persisted_responses=True,
            clock=lambda: now,
            monotonic_clock=monotonic_clock,
        )

        with caplog.at_level(logging.WARNING):
            worker.run(
                stop_event=stop_event,
                concurrency=1,
                idle_poll_seconds=0.001,
            )

        candidate_logs = [
            record
            for record in caplog.records
            if getattr(record, "fields", {}).get("error_code")
            == "dispatch_reconciliation_candidate_invalid"
        ]
        assert ticks >= 100
        assert len(candidate_logs) == 1
        assert not [
            record
            for record in candidate_logs
            if record.levelno >= logging.ERROR
            and record.fields["recovery_action"] != "candidate_quarantined"
        ]
    finally:
        engine.dispose()


def test_concurrent_reconciliation_creates_one_response_and_delivery(tmp_path: Path) -> None:
    factory, engine = _database_factory(tmp_path, "concurrent-reconcile.db")
    try:
        _persist_raw_success(factory, "concurrent")
        workers = [_worker(factory, NoHermesDispatcher()) for _ in range(2)]

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda worker: worker.reconcile_once(), workers))

        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
    finally:
        engine.dispose()


def test_concurrent_reconciliation_of_existing_response_creates_one_delivery(
    tmp_path: Path,
) -> None:
    factory, engine = _database_factory(tmp_path, "concurrent-missing-outbox.db")
    try:
        persisted = _persist_raw_success(factory, "concurrent-missing")
        with factory() as session:
            message = session.get(Message, persisted.message_id)
            assert message is not None
            _, delivery, _ = ResponseStore(session).save_generated(
                persisted.outcome,
                target=DeliveryTarget(
                    channel=message.source,
                    account_id=message.source_account_id,
                    conversation_id=message.conversation_id,
                ),
            )
            session.delete(delivery)
            session.commit()
        workers = [_worker(factory, NoHermesDispatcher()) for _ in range(2)]

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda worker: worker.reconcile_once(), workers))

        with factory() as session:
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
    finally:
        engine.dispose()


def test_resident_worker_retries_transient_database_claim_error(
    tmp_path: Path,
    caplog,
    monkeypatch,
) -> None:
    factory, engine = _database_factory(tmp_path, "database-reconnect.db")
    stop_event = Event()
    worker = HermesDispatchWorker(
        factory,
        lambda session: NoHermesDispatcher(),
        lease_seconds=10,
        retry_limit=1,
    )
    calls = 0

    def flaky_claim(*, now=None):
        nonlocal calls
        del now
        calls += 1
        if calls == 1:
            raise OperationalError("SELECT", {}, RuntimeError("connection lost"))
        stop_event.set()
        return None

    monkeypatch.setattr(worker, "claim_once", flaky_claim)
    try:
        with caplog.at_level(logging.WARNING):
            worker.run(
                stop_event=stop_event,
                concurrency=1,
                idle_poll_seconds=0.001,
            )

        assert calls == 2
        assert any(
            getattr(record, "fields", {}).get("error_code")
            == "dispatch_database_temporarily_unavailable"
            for record in caplog.records
        )
        assert "connection lost" not in caplog.text
    finally:
        engine.dispose()
