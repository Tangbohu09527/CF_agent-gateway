from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    HermesChatResult,
    HermesDispatchService,
    HermesLedgerStore,
    HermesOperationStatus,
    HermesRecoveryService,
    HermesResponseHandler,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.workspace.models import EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService

SOURCE = "wechat"
SOURCE_ACCOUNT_ID = "wxid-gateway"
ASSISTANT_CONTENT = "The release is ready."


@pytest.fixture
def database_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "hermes-recovery.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


class RecordingHermesClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.contents: list[str] = []

    def chat(self, content: str, *, hermes_thread_id: str | None = None) -> HermesChatResult:
        self.contents.append(content)
        if self.error is not None:
            raise self.error
        assert hermes_thread_id is not None
        return HermesChatResult(
            assistant_content=ASSISTANT_CONTENT,
            hermes_thread_id=hermes_thread_id,
        )


class RecordingWechatSender:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.account_id = SOURCE_ACCOUNT_ID
        self.error = error
        self.messages: list[tuple[str, str]] = []

    def send_text(self, conversation_id: str, content: str) -> None:
        self.messages.append((conversation_id, content))
        if self.error is not None:
            raise self.error


class CoordinatedRecoveryService(HermesRecoveryService):
    def __init__(self, *args: object, recovery_barrier: Barrier, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._recovery_barrier = recovery_barrier

    def _recover_dispatch(self, dispatch_id: int) -> bool:
        self._recovery_barrier.wait(timeout=10)
        return super()._recover_dispatch(dispatch_id)


@dataclass(frozen=True, slots=True)
class RecoveryResources:
    admission: AdmissionOutcome
    message_id: int
    workspace_id: str
    ai_thread_id: str
    conversation_id: str
    content: str


def create_resources(session: Session, suffix: str) -> RecoveryResources:
    conversation_id = f"wxid-{suffix}"
    sender_id = f"wxid-sender-{suffix}"
    identity = IdentityService(session).create_identity(
        employee_id=f"employee-{suffix}",
        display_name=suffix,
    )
    thread = WorkspaceService(session).ensure_thread_for_authorized_request(
        enterprise_identity_id=identity.id,
        platform=SOURCE,
        account_id=SOURCE_ACCOUNT_ID,
        physical_conversation_id=conversation_id,
        conversation_type="private",
        sender_id=sender_id,
    )
    workspace = session.get(EmployeeWorkspace, thread.workspace_id)
    assert workspace is not None
    content = f"message-{suffix}"
    message, created = MessageStore(session).create(
        MessageEvent(
            event_id=f"wechat:event-{suffix}",
            source=SOURCE,
            source_account_id=SOURCE_ACCOUNT_ID,
            source_message_id=f"server-{suffix}",
            conversation_id=conversation_id,
            conversation_type="private",
            is_mentioned=None,
            is_self=False,
            sender_type="human",
            sender_id=sender_id,
            sender_name=suffix,
            message_type="text",
            raw_type=1,
            content=content,
            timestamp=datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
        )
    )
    assert created is True
    admission = AdmissionOutcome(
        message_id=message.id,
        admitted=True,
        should_create_task=True,
        reason=AdmissionReason.ALLOWED,
        enterprise_identity_id=identity.id,
        workspace_id=workspace.id,
        ai_thread_id=thread.id,
    )
    return RecoveryResources(
        admission=admission,
        message_id=message.id,
        workspace_id=workspace.id,
        ai_thread_id=thread.id,
        conversation_id=conversation_id,
        content=content,
    )


def create_recovery_service(
    factory: sessionmaker[Session],
    client: RecordingHermesClient,
    sender: RecordingWechatSender,
    *,
    clock: datetime | None = None,
) -> HermesRecoveryService:
    return HermesRecoveryService(
        factory,
        lambda session: HermesDispatchService(session, client),
        lambda session: HermesResponseHandler(session, sender),
        clock=(lambda: clock) if clock is not None else None,
    )


def test_failed_dispatch_recovers_without_source_message_replay(
    database_factory: sessionmaker[Session],
) -> None:
    client = RecordingHermesClient(error=RuntimeError("Hermes unavailable"))
    sender = RecordingWechatSender()
    with database_factory() as session:
        resources = create_resources(session, "source-window-expired")
        with pytest.raises(RuntimeError, match="Hermes unavailable"):
            HermesDispatchService(session, client).dispatch(resources.admission)

    client.error = None
    result = create_recovery_service(database_factory, client, sender).drain()

    assert result.dispatch_candidates == 1
    assert result.dispatch_recovered == 1
    assert result.delivery_candidates == 1
    assert result.delivery_recovered == 1
    assert client.contents == [resources.content, resources.content]
    assert sender.messages == [(resources.conversation_id, ASSISTANT_CONTENT)]
    with database_factory() as session:
        dispatch = HermesLedgerStore(session).get_dispatch(resources.message_id)
        assert dispatch is not None
        assert dispatch.status is HermesOperationStatus.SUCCEEDED
        assert dispatch.attempt_count == 2


def test_failed_missing_delivery_does_not_starve_later_candidate(
    database_factory: sessionmaker[Session],
) -> None:
    due_at = datetime.now(UTC) + timedelta(minutes=1)
    client = RecordingHermesClient()
    sender = RecordingWechatSender()
    with database_factory() as session:
        poison = create_resources(session, "poison-delivery")
        healthy = create_resources(session, "healthy-delivery")
        HermesDispatchService(session, client).dispatch(poison.admission)
        HermesDispatchService(session, client).dispatch(healthy.admission)
        session.execute(
            text("DELETE FROM thread_source_bindings WHERE ai_thread_id = :ai_thread_id"),
            {"ai_thread_id": poison.ai_thread_id},
        )
        session.commit()

    service = HermesRecoveryService(
        database_factory,
        lambda session: HermesDispatchService(session, client),
        lambda session: HermesResponseHandler(session, sender),
        batch_size=1,
        clock=lambda: due_at,
    )
    first = service.drain()
    second = service.drain()

    assert first.delivery_candidates == 1
    assert first.delivery_failed == 1
    assert second.delivery_candidates == 1
    assert second.delivery_recovered == 1
    assert sender.messages == [(healthy.conversation_id, ASSISTANT_CONTENT)]


def test_successful_dispatch_without_delivery_is_recovered(
    database_factory: sessionmaker[Session],
) -> None:
    client = RecordingHermesClient()
    sender = RecordingWechatSender()
    with database_factory() as session:
        resources = create_resources(session, "missing-delivery")
        HermesDispatchService(session, client).dispatch(resources.admission)

    result = create_recovery_service(database_factory, client, sender).drain()

    assert result.dispatch_candidates == 0
    assert result.delivery_candidates == 1
    assert result.delivery_recovered == 1
    assert sender.messages == [(resources.conversation_id, ASSISTANT_CONTENT)]
    with database_factory() as session:
        delivery = HermesLedgerStore(session).get_delivery(resources.message_id)
        assert delivery is not None
        assert delivery.status is HermesOperationStatus.SUCCEEDED


def test_recovery_failure_is_isolated_and_does_not_log_exception_details(
    database_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = RecordingHermesClient(error=RuntimeError("credential=do-not-log"))
    sender = RecordingWechatSender()
    with database_factory() as session:
        resources = create_resources(session, "isolated-failure")
        with pytest.raises(RuntimeError):
            HermesDispatchService(session, client).dispatch(resources.admission)

    with caplog.at_level(logging.WARNING, logger="cf_agent_gateway.hermes.recovery"):
        result = create_recovery_service(database_factory, client, sender).drain()

    assert result.dispatch_candidates == 1
    assert result.dispatch_recovered == 0
    assert result.dispatch_failed == 1
    assert "do-not-log" not in caplog.text
    recovery_record = caplog.records[-1]
    assert recovery_record.fields == {
        "operation": "dispatch",
        "dispatch_id": recovery_record.fields["dispatch_id"],
        "message_id": resources.message_id,
        "error_code": "RuntimeError",
    }


def test_dispatch_recovery_reclaims_failed_and_stale_but_not_active_lease(
    database_factory: sessionmaker[Session],
) -> None:
    due_at = datetime.now(UTC)
    stale_at = due_at - timedelta(hours=1)
    client = RecordingHermesClient()
    sender = RecordingWechatSender()
    with database_factory() as session:
        failed = create_resources(session, "dispatch-failed")
        stale = create_resources(session, "dispatch-stale")
        active = create_resources(session, "dispatch-active")
        ledger = HermesLedgerStore(session)
        failed_claim = ledger.claim_dispatch(
            message_id=failed.message_id,
            workspace_id=failed.workspace_id,
            ai_thread_id=failed.ai_thread_id,
            requested_hermes_thread_id="failed-thread",
            now=due_at,
        )
        assert failed_claim.record.lease_token is not None
        ledger.fail_dispatch(
            failed_claim.record,
            lease_token=failed_claim.record.lease_token,
            error_code="controlled_failure",
        )
        ledger.claim_dispatch(
            message_id=stale.message_id,
            workspace_id=stale.workspace_id,
            ai_thread_id=stale.ai_thread_id,
            requested_hermes_thread_id="stale-thread",
            now=stale_at,
        )
        ledger.claim_dispatch(
            message_id=active.message_id,
            workspace_id=active.workspace_id,
            ai_thread_id=active.ai_thread_id,
            requested_hermes_thread_id="active-thread",
            now=due_at,
        )

    service = create_recovery_service(
        database_factory,
        client,
        sender,
        clock=due_at,
    )
    first = service.drain()
    second = service.drain()

    assert first.dispatch_candidates == 2
    assert first.dispatch_recovered == 2
    assert second.dispatch_candidates == 0
    assert set(client.contents) == {failed.content, stale.content}
    with database_factory() as session:
        ledger = HermesLedgerStore(session)
        failed_record = ledger.get_dispatch(failed.message_id)
        stale_record = ledger.get_dispatch(stale.message_id)
        active_record = ledger.get_dispatch(active.message_id)
        assert failed_record is not None
        assert stale_record is not None
        assert active_record is not None
        assert failed_record.status is HermesOperationStatus.SUCCEEDED
        assert stale_record.status is HermesOperationStatus.SUCCEEDED
        assert active_record.status is HermesOperationStatus.IN_PROGRESS
        assert active_record.attempt_count == 1


def test_delivery_recovery_reclaims_failed_and_stale_but_not_active_lease(
    database_factory: sessionmaker[Session],
) -> None:
    due_at = datetime.now(UTC)
    stale_at = due_at - timedelta(hours=1)
    client = RecordingHermesClient()
    sender = RecordingWechatSender()
    with database_factory() as session:
        failed = create_resources(session, "delivery-failed")
        stale = create_resources(session, "delivery-stale")
        active = create_resources(session, "delivery-active")
        dispatches = [
            HermesDispatchService(session, client).dispatch(resource.admission)
            for resource in (failed, stale, active)
        ]
        ledger = HermesLedgerStore(session)
        claims = [
            ledger.claim_delivery(
                message_id=resource.message_id,
                ai_thread_id=resource.ai_thread_id,
                conversation_id=resource.conversation_id,
                content_sha256=sha256(response.assistant_content.encode("utf-8")).hexdigest(),
                now=claimed_at,
            )
            for resource, response, claimed_at in zip(
                (failed, stale, active),
                dispatches,
                (due_at, stale_at, due_at),
                strict=True,
            )
        ]
        assert claims[0].record.lease_token is not None
        ledger.fail_delivery(
            claims[0].record,
            lease_token=claims[0].record.lease_token,
            error_code="controlled_failure",
        )

    service = create_recovery_service(
        database_factory,
        client,
        sender,
        clock=due_at,
    )
    first = service.drain()
    second = service.drain()

    assert first.delivery_candidates == 2
    assert first.delivery_recovered == 2
    assert second.delivery_candidates == 0
    assert set(sender.messages) == {
        (failed.conversation_id, ASSISTANT_CONTENT),
        (stale.conversation_id, ASSISTANT_CONTENT),
    }
    with database_factory() as session:
        ledger = HermesLedgerStore(session)
        failed_record = ledger.get_delivery(failed.message_id)
        stale_record = ledger.get_delivery(stale.message_id)
        active_record = ledger.get_delivery(active.message_id)
        assert failed_record is not None
        assert stale_record is not None
        assert active_record is not None
        assert failed_record.status is HermesOperationStatus.SUCCEEDED
        assert stale_record.status is HermesOperationStatus.SUCCEEDED
        assert active_record.status is HermesOperationStatus.IN_PROGRESS
        assert active_record.attempt_count == 1


def test_concurrent_recovery_snapshots_only_dispatch_once(
    database_factory: sessionmaker[Session],
) -> None:
    client = RecordingHermesClient()
    sender = RecordingWechatSender()
    with database_factory() as session:
        resources = create_resources(session, "concurrent-recovery")
        ledger = HermesLedgerStore(session)
        claim = ledger.claim_dispatch(
            message_id=resources.message_id,
            workspace_id=resources.workspace_id,
            ai_thread_id=resources.ai_thread_id,
            requested_hermes_thread_id="failed-thread",
        )
        assert claim.record.lease_token is not None
        ledger.fail_dispatch(
            claim.record,
            lease_token=claim.record.lease_token,
            error_code="controlled_failure",
        )

    recovery_barrier = Barrier(2)
    services = [
        CoordinatedRecoveryService(
            database_factory,
            lambda session: HermesDispatchService(session, client),
            lambda session: HermesResponseHandler(session, sender),
            recovery_barrier=recovery_barrier,
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda service: service.drain(), services))

    assert [result.dispatch_candidates for result in results] == [1, 1]
    assert client.contents == [resources.content]
    with database_factory() as session:
        record = HermesLedgerStore(session).get_dispatch(resources.message_id)
        assert record is not None
        assert record.status is HermesOperationStatus.SUCCEEDED
        assert record.attempt_count == 2
