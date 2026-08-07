from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.artifact import ArtifactKind, ArtifactRepository
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.delivery import (
    ChannelDeliveryWorker,
    DeliveryAttempt,
    DeliveryAttemptStatus,
    DeliveryOutboxRecord,
    DeliveryOutboxStore,
    DeliveryReceipt,
    DeliveryStatus,
    RetryableDeliveryError,
)
from cf_agent_gateway.hermes import (
    ArtifactRefPart,
    HermesChatResult,
    HermesDispatchOutboxExecutor,
    HermesDispatchOutcome,
    HermesDispatchService,
    HermesResponseRelay,
    ResponseEnvelope,
    TextPart,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.response import (
    DeliveryTarget,
    ResponsePartKind,
    ResponsePersistenceProcessor,
    ResponseRecord,
    ResponseStatus,
    ResponseStore,
)
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchStatus,
)
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService

SOURCE = "wechat"
ACCOUNT_ID = "wxid-gateway"
CONVERSATION_ID = "wxid-alice"
SENDER_ID = "wxid-alice"
RESPONSE_ID = "response-delivery-001"
PNG = b"\x89PNG\r\n\x1a\nresponse-delivery-test"


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


class RecordingSender:
    def __init__(self, *, fail_media_once: bool = False, uncertain: bool = False) -> None:
        self.account_id = ACCOUNT_ID
        self.calls: list[tuple[object, ...]] = []
        self.fail_media_once = fail_media_once
        self.uncertain = uncertain
        self.close_calls = 0

    def send_text(self, conversation_id: str, content: str) -> dict[str, object]:
        self.calls.append(("text", conversation_id, content))
        if self.uncertain:
            raise RuntimeError("provider result unknown")
        return {"success": True, "localId": len(self.calls)}

    def send_media(
        self,
        conversation_id: str,
        media_type: str,
        data: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("media", conversation_id, media_type, data, mime_type, filename))
        if self.fail_media_once:
            self.fail_media_once = False
            raise RetryableDeliveryError("retry media")
        return {"success": True, "localId": len(self.calls)}

    def close(self) -> None:
        self.close_calls += 1


class StaticHermesClient:
    def chat(
        self,
        content: str,
        *,
        hermes_thread_id: str | None = None,
        profile_reference: str | None = None,
        profile_revision: int | None = None,
        thread_id: str | None = None,
        session_metadata: dict[str, object] | None = None,
    ) -> HermesChatResult:
        del content, profile_reference, profile_revision, thread_id, session_metadata
        assert hermes_thread_id is not None
        return HermesChatResult(
            assistant_content="persisted response",
            hermes_thread_id=hermes_thread_id,
        )


def create_domain(
    session: Session,
) -> tuple[Message, EmployeeWorkspace, AIThread, AdmissionOutcome]:
    identity = IdentityService(session).create_identity(employee_id="employee-response")
    thread = WorkspaceService(session).ensure_thread_for_authorized_request(
        enterprise_identity_id=identity.id,
        platform=SOURCE,
        account_id=ACCOUNT_ID,
        physical_conversation_id=CONVERSATION_ID,
        conversation_type="private",
        sender_id=SENDER_ID,
    )
    workspace = session.get(EmployeeWorkspace, thread.workspace_id)
    assert workspace is not None
    message, created = MessageStore(session).create(
        MessageEvent(
            event_id="wechat:response-delivery-001",
            source=SOURCE,
            source_account_id=ACCOUNT_ID,
            source_message_id="source-response-delivery-001",
            conversation_id=CONVERSATION_ID,
            conversation_type="private",
            is_mentioned=None,
            is_self=False,
            sender_type="human",
            sender_id=SENDER_ID,
            sender_name="Alice",
            message_type="text",
            raw_type=1,
            content="generate a response",
            timestamp=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
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
    return message, workspace, thread, admission


def outcome(
    message: Message,
    workspace: EmployeeWorkspace,
    thread: AIThread,
    envelope: ResponseEnvelope,
) -> HermesDispatchOutcome:
    return HermesDispatchOutcome.from_response(
        message_id=message.id,
        workspace_id=workspace.id,
        ai_thread_id=thread.id,
        response=envelope,
    )


def target() -> DeliveryTarget:
    return DeliveryTarget(
        channel=SOURCE,
        account_id=ACCOUNT_ID,
        conversation_id=CONVERSATION_ID,
    )


def test_response_store_preserves_parts_and_prevents_duplicate_enqueue(
    session: Session,
) -> None:
    message, workspace, thread, _ = create_domain(session)
    envelope = ResponseEnvelope(
        response_id=RESPONSE_ID,
        parts=(
            TextPart(text="Report "),
            ArtifactRefPart(artifact_id="artifact-001"),
            TextPart(text="ready"),
        ),
    )
    store = ResponseStore(session)

    response, delivery, created = store.save_generated(
        outcome(message, workspace, thread, envelope),
        target=target(),
    )
    duplicate_response, duplicate_delivery, duplicate_created = store.save_generated(
        outcome(message, workspace, thread, envelope),
        target=target(),
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate_response.response_id == response.response_id
    assert duplicate_delivery.id == delivery.id
    assert response.status is ResponseStatus.GENERATED
    assert [
        (part.ordinal, part.part_type, part.text, part.artifact_id) for part in response.parts
    ] == [
        (0, ResponsePartKind.TEXT, "Report ", None),
        (1, ResponsePartKind.ARTIFACT_REF, None, "artifact-001"),
        (2, ResponsePartKind.TEXT, "ready", None),
    ]
    assert delivery.status is DeliveryStatus.QUEUED
    assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
    assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1


def test_channel_worker_delivers_text_image_and_file_in_envelope_order(
    session: Session,
    tmp_path: Path,
) -> None:
    message, workspace, thread, _ = create_domain(session)
    repository = ArtifactRepository(session, tmp_path)
    image = repository.create(
        response_id=RESPONSE_ID,
        kind=ArtifactKind.IMAGE,
        filename="chart.png",
        mime_type="image/png",
        content=PNG,
    )
    file = repository.create(
        response_id=RESPONSE_ID,
        kind=ArtifactKind.FILE,
        filename="report.txt",
        mime_type="text/plain",
        content=b"report bytes",
    )
    envelope = ResponseEnvelope(
        response_id=RESPONSE_ID,
        parts=(
            TextPart(text="before"),
            ArtifactRefPart(artifact_id=image.artifact_id),
            TextPart(text="between"),
            ArtifactRefPart(artifact_id=file.artifact_id),
        ),
    )
    response, delivery, _ = ResponseStore(session).save_generated(
        outcome(message, workspace, thread, envelope),
        target=target(),
    )
    sender = RecordingSender()

    result = ChannelDeliveryWorker(
        session,
        lambda *, account_id: sender,
        artifact_repository=repository,
    ).run_once()

    assert result is not None and result.status is DeliveryStatus.DELIVERED
    assert sender.calls == [
        ("text", CONVERSATION_ID, "before"),
        ("media", CONVERSATION_ID, "image", PNG, "image/png", None),
        ("text", CONVERSATION_ID, "between"),
        (
            "media",
            CONVERSATION_ID,
            "file",
            b"report bytes",
            "text/plain",
            "report.txt",
        ),
    ]
    session.refresh(response)
    session.refresh(delivery)
    assert response.status is ResponseStatus.DELIVERED
    assert delivery.status is DeliveryStatus.DELIVERED
    receipts = DeliveryOutboxStore(session).list_receipts(delivery.id)
    assert [receipt.part_ordinal for receipt in receipts] == [0, 1, 2, 3]
    assert [receipt.provider_message_id for receipt in receipts] == ["1", "2", "3", "4"]
    assert session.scalar(select(func.count()).select_from(DeliveryReceipt)) == 4


def test_retry_resumes_at_first_part_without_receipt(
    session: Session,
    tmp_path: Path,
) -> None:
    message, workspace, thread, _ = create_domain(session)
    repository = ArtifactRepository(session, tmp_path)
    image = repository.create(
        response_id=RESPONSE_ID,
        kind=ArtifactKind.IMAGE,
        filename="chart.png",
        mime_type="image/png",
        content=PNG,
    )
    envelope = ResponseEnvelope(
        response_id=RESPONSE_ID,
        parts=(
            TextPart(text="first"),
            ArtifactRefPart(artifact_id=image.artifact_id),
            TextPart(text="last"),
        ),
    )
    _, delivery, _ = ResponseStore(session).save_generated(
        outcome(message, workspace, thread, envelope),
        target=target(),
    )
    sender = RecordingSender(fail_media_once=True)
    worker = ChannelDeliveryWorker(
        session,
        lambda *, account_id: sender,
        artifact_repository=repository,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first is not None and first.status is DeliveryStatus.QUEUED
    assert first.next_part_ordinal == 1
    assert second is not None and second.status is DeliveryStatus.DELIVERED
    assert [call[0] for call in sender.calls] == ["text", "media", "media", "text"]
    assert sender.calls.count(("text", CONVERSATION_ID, "first")) == 1
    attempts = DeliveryOutboxStore(session).list_attempts(delivery.id)
    assert [attempt.status for attempt in attempts] == [
        DeliveryAttemptStatus.DELIVERED,
        DeliveryAttemptStatus.FAILED,
        DeliveryAttemptStatus.DELIVERED,
        DeliveryAttemptStatus.DELIVERED,
    ]
    assert session.scalar(select(func.count()).select_from(DeliveryAttempt)) == 4


def test_delivery_failure_never_changes_successful_hermes_dispatch(session: Session) -> None:
    _, _, _, admission = create_domain(session)
    record_store = HermesDispatchRecordStore(session)
    record, created = record_store.enqueue(admission)
    assert created is True
    dispatcher = HermesDispatchOutboxExecutor(
        session,
        HermesResponseRelay(
            HermesDispatchService(session, StaticHermesClient()),
            ResponsePersistenceProcessor(session),
        ),
        record_store=record_store,
    )
    dispatcher.dispatch(admission)
    sender = RecordingSender(uncertain=True)

    result = ChannelDeliveryWorker(session, lambda *, account_id: sender).run_once()

    session.refresh(record)
    response = session.scalar(select(ResponseRecord))
    delivery = session.scalar(select(DeliveryOutboxRecord))
    assert result is not None and result.status is DeliveryStatus.UNCERTAIN
    assert record.status is HermesDispatchStatus.SUCCESS
    assert response is not None and response.status is ResponseStatus.UNCERTAIN
    assert delivery is not None and delivery.status is DeliveryStatus.UNCERTAIN
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1
    assert ChannelDeliveryWorker(session, lambda *, account_id: sender).run_once() is None


def test_wechat_worker_does_not_claim_another_channel(session: Session) -> None:
    message, workspace, thread, _ = create_domain(session)
    envelope = ResponseEnvelope(
        response_id=RESPONSE_ID,
        parts=(TextPart(text="channel scoped"),),
    )
    _, delivery, _ = ResponseStore(session).save_generated(
        outcome(message, workspace, thread, envelope),
        target=DeliveryTarget(
            channel="email",
            account_id=ACCOUNT_ID,
            conversation_id=CONVERSATION_ID,
        ),
    )
    sender = RecordingSender()

    result = ChannelDeliveryWorker(session, lambda *, account_id: sender).run_once()

    session.refresh(delivery)
    assert result is None
    assert delivery.status is DeliveryStatus.QUEUED
    assert sender.calls == []


def test_stale_claim_without_attempt_is_safely_requeued(session: Session) -> None:
    message, workspace, thread, _ = create_domain(session)
    envelope = ResponseEnvelope(
        response_id=RESPONSE_ID,
        parts=(TextPart(text="recover me"),),
    )
    _, delivery, _ = ResponseStore(session).save_generated(
        outcome(message, workspace, thread, envelope),
        target=target(),
    )
    store = DeliveryOutboxStore(session)
    claimed_at = datetime.now(UTC) + timedelta(seconds=1)
    claimed = store.claim_next(
        channel=SOURCE,
        claim_token="abandoned-before-send",
        now=claimed_at,
    )
    assert claimed is not None
    sender = RecordingSender()
    worker = ChannelDeliveryWorker(
        session,
        lambda *, account_id: sender,
        claim_timeout_seconds=60,
        clock=lambda: claimed_at + timedelta(seconds=61),
    )

    result = worker.run_once()

    session.refresh(delivery)
    assert result is not None and result.status is DeliveryStatus.DELIVERED
    assert delivery.attempt_count == 2
    assert sender.calls == [("text", CONVERSATION_ID, "recover me")]


def test_stale_in_flight_attempt_becomes_uncertain(session: Session) -> None:
    message, workspace, thread, _ = create_domain(session)
    envelope = ResponseEnvelope(
        response_id=RESPONSE_ID,
        parts=(TextPart(text="possibly sent"),),
    )
    response, delivery, _ = ResponseStore(session).save_generated(
        outcome(message, workspace, thread, envelope),
        target=target(),
    )
    store = DeliveryOutboxStore(session)
    claimed_at = datetime.now(UTC) + timedelta(seconds=1)
    claimed = store.claim_next(
        channel=SOURCE,
        claim_token="abandoned-in-flight",
        now=claimed_at,
    )
    assert claimed is not None
    attempt = store.start_attempt(
        delivery.id,
        claim_token="abandoned-in-flight",
        part_ordinal=0,
    )

    recovered = store.recover_stale_claims(
        channel=SOURCE,
        stale_before=claimed_at + timedelta(seconds=1),
        now=claimed_at + timedelta(seconds=2),
    )

    session.refresh(response)
    session.refresh(delivery)
    session.refresh(attempt)
    assert [item.id for item in recovered] == [delivery.id]
    assert response.status is ResponseStatus.UNCERTAIN
    assert delivery.status is DeliveryStatus.UNCERTAIN
    assert attempt.status is DeliveryAttemptStatus.UNCERTAIN
    sender = RecordingSender()
    assert ChannelDeliveryWorker(session, lambda *, account_id: sender).run_once() is None
    assert sender.calls == []


def test_retry_budget_is_independent_for_each_part(session: Session) -> None:
    message, workspace, thread, _ = create_domain(session)
    envelope = ResponseEnvelope(
        response_id=RESPONSE_ID,
        parts=(TextPart(text="first"), TextPart(text="second")),
    )
    _, delivery, _ = ResponseStore(session).save_generated(
        outcome(message, workspace, thread, envelope),
        target=target(),
    )

    class FailEachTextOnce(RecordingSender):
        def __init__(self) -> None:
            super().__init__()
            self.failed: set[str] = set()

        def send_text(self, conversation_id: str, content: str) -> dict[str, object]:
            self.calls.append(("text", conversation_id, content))
            if content not in self.failed:
                self.failed.add(content)
                raise RetryableDeliveryError("retry text")
            return {"success": True, "localId": len(self.calls)}

    sender = FailEachTextOnce()
    worker = ChannelDeliveryWorker(
        session,
        lambda *, account_id: sender,
        max_attempts=2,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    first = worker.run_once()
    second = worker.run_once()
    third = worker.run_once()

    assert first is not None and first.status is DeliveryStatus.QUEUED
    assert second is not None and second.status is DeliveryStatus.QUEUED
    assert third is not None and third.status is DeliveryStatus.DELIVERED
    assert sender.calls == [
        ("text", CONVERSATION_ID, "first"),
        ("text", CONVERSATION_ID, "first"),
        ("text", CONVERSATION_ID, "second"),
        ("text", CONVERSATION_ID, "second"),
    ]
    attempts = DeliveryOutboxStore(session).list_attempts(delivery.id)
    assert [attempt.attempt_number for attempt in attempts] == [1, 2, 1, 2]
    assert [attempt.status for attempt in attempts] == [
        DeliveryAttemptStatus.FAILED,
        DeliveryAttemptStatus.DELIVERED,
        DeliveryAttemptStatus.FAILED,
        DeliveryAttemptStatus.DELIVERED,
    ]
