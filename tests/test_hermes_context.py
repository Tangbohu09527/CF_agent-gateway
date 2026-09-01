from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.artifact.models import Artifact, ArtifactKind
from cf_agent_gateway.context import (
    AuthorizedContextProvider,
    ContextAccessDeniedError,
    ContextEntry,
    ContextEntryKind,
    ContextProvider,
    ContextSnapshot,
    ContextSnapshotStore,
    ContextValidationError,
    ThreadContextAccessPolicy,
    create_context_provider,
    create_context_tool,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    ArtifactRefPart,
    HermesDispatchOutcome,
    HermesDispatchResponseStore,
    ResponseEnvelope,
    TextPart,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace import ThreadPolicy, ThreadResolver
from cf_agent_gateway.workspace.models import EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService

SOURCE = "wechat"
SOURCE_ACCOUNT_ID = "wxid-gateway"


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


@dataclass(frozen=True, slots=True)
class ThreadResources:
    identity_id: str
    workspace_id: str
    thread_id: str
    conversation_id: str
    sender_id: str
    conversation_type: str = "private"


def create_thread_resources(session: Session, suffix: str) -> ThreadResources:
    identity = IdentityService(session).create_identity(employee_id=f"employee-{suffix}")
    conversation_id = f"conversation-{suffix}"
    sender_id = f"sender-{suffix}"
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
    return ThreadResources(
        identity_id=identity.id,
        workspace_id=workspace.id,
        thread_id=thread.id,
        conversation_id=conversation_id,
        sender_id=sender_id,
    )


def create_group_sender_resources(
    session: Session,
    suffix: str,
    *,
    conversation_id: str,
) -> ThreadResources:
    identity = IdentityService(session).create_identity(employee_id=f"employee-{suffix}")
    sender_id = f"sender-{suffix}"
    thread = ThreadResolver(session).resolve(
        conversation={
            "conversation_id": conversation_id,
            "conversation_type": "group",
        },
        source_account={
            "platform": SOURCE,
            "account_id": SOURCE_ACCOUNT_ID,
        },
        sender_identity={"identity_id": identity.id},
        agent_profile={"profile_id": "context-profile", "revision": 1},
        thread_policy=ThreadPolicy.GROUP_SENDER,
    )
    workspace = session.get(EmployeeWorkspace, thread.workspace_id)
    assert workspace is not None
    return ThreadResources(
        identity_id=identity.id,
        workspace_id=workspace.id,
        thread_id=thread.id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        conversation_type="group",
    )


def persist_message(
    session: Session,
    resources: ThreadResources,
    *,
    sequence: int,
    content: str,
    occurred_at: datetime | None = None,
) -> Message:
    message, created = MessageStore(session).create(
        MessageEvent(
            event_id=f"wechat:context-{resources.sender_id}-{sequence}",
            source=SOURCE,
            source_account_id=SOURCE_ACCOUNT_ID,
            source_message_id=f"server-{resources.sender_id}-{sequence}",
            conversation_id=resources.conversation_id,
            conversation_type=resources.conversation_type,
            is_mentioned=True if resources.conversation_type == "group" else None,
            is_self=False,
            sender_type="human",
            sender_id=resources.sender_id,
            sender_name=resources.sender_id,
            message_type="text",
            raw_type=1,
            content=content,
            timestamp=occurred_at or datetime(2026, 8, 10, 9, sequence, tzinfo=UTC),
        )
    )
    assert created is True
    return message


def enqueue_persisted_message(
    session: Session,
    resources: ThreadResources,
    message: Message,
) -> HermesDispatchRecord:
    record, record_created = HermesDispatchRecordStore(session).enqueue(
        AdmissionOutcome(
            message_id=message.id,
            admitted=True,
            should_create_task=True,
            reason=AdmissionReason.ALLOWED,
            enterprise_identity_id=resources.identity_id,
            workspace_id=resources.workspace_id,
            ai_thread_id=resources.thread_id,
        )
    )
    assert record_created is True
    return record


def enqueue_message(
    session: Session,
    resources: ThreadResources,
    *,
    sequence: int,
    content: str,
    occurred_at: datetime | None = None,
) -> tuple[Message, HermesDispatchRecord]:
    message = persist_message(
        session,
        resources,
        sequence=sequence,
        content=content,
        occurred_at=occurred_at,
    )
    return message, enqueue_persisted_message(session, resources, message)


def complete_message(
    session: Session,
    resources: ThreadResources,
    message: Message,
    record: HermesDispatchRecord,
    *,
    response_id: str,
    parts: tuple[TextPart | ArtifactRefPart, ...],
) -> None:
    claim_token = f"context-claim-{record.id}"
    HermesDispatchRecordStore(session).claim(record.id, claim_token=claim_token)
    outcome = HermesDispatchOutcome.from_response(
        message_id=message.id,
        workspace_id=resources.workspace_id,
        ai_thread_id=resources.thread_id,
        response=ResponseEnvelope(response_id=response_id, parts=parts),
    )
    HermesDispatchResponseStore(session).complete_success(
        record.id,
        claim_token=claim_token,
        outcome=outcome,
    )


def persist_artifact(session: Session, *, artifact_id: str, response_id: str) -> None:
    session.add(
        Artifact(
            artifact_id=artifact_id,
            response_id=response_id,
            kind=ArtifactKind.FILE,
            filename=f"{artifact_id}.txt",
            mime_type="text/plain",
            storage_key=f"context/{artifact_id}",
        )
    )
    session.commit()


def test_thread_timeline_projects_messages_responses_artifacts_and_timestamps(
    session: Session,
) -> None:
    resources = create_thread_resources(session, "timeline")
    occurred_at = datetime(2026, 8, 10, 8, 30, tzinfo=UTC)
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="Prepare the release report",
        occurred_at=occurred_at,
    )
    persist_artifact(
        session,
        artifact_id="artifact-context-1",
        response_id="response-context-1",
    )
    persist_artifact(
        session,
        artifact_id="artifact-other-response",
        response_id="response-other",
    )
    complete_message(
        session,
        resources,
        message,
        record,
        response_id="response-context-1",
        parts=(
            TextPart(text="The release is ready."),
            ArtifactRefPart(artifact_id="artifact-context-1"),
            ArtifactRefPart(artifact_id="artifact-other-response"),
        ),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    entries = provider.read(resources.thread_id)

    assert [entry.kind for entry in entries] == [
        ContextEntryKind.MESSAGE,
        ContextEntryKind.ASSISTANT_RESPONSE,
        ContextEntryKind.ARTIFACT_REFERENCE,
    ]
    assert all(entry.thread_id == resources.thread_id for entry in entries)
    assert all(entry.message_id == message.id for entry in entries)
    assert all(entry.dispatch_id == record.id for entry in entries)
    assert entries[0].content == "Prepare the release report"
    assert entries[0].occurred_at == occurred_at
    assert entries[0].received_at.tzinfo is not None
    assert entries[0].created_at.tzinfo is not None
    assert entries[1].content == "The release is ready."
    assert entries[1].response_id == "response-context-1"
    assert entries[1].part_ordinal == 0
    assert entries[2].artifact_id == "artifact-context-1"
    assert entries[2].response_id == "response-context-1"
    assert entries[2].part_ordinal == 1
    assert all(entry.artifact_id != "artifact-other-response" for entry in entries)
    assert entries[1].occurred_at == entries[1].received_at == entries[1].created_at
    assert entries[2].occurred_at == entries[2].received_at == entries[2].created_at


def test_context_snapshot_create_and_read_latest(session: Session) -> None:
    resources = create_thread_resources(session, "snapshot-latest")
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="authorize snapshot reads",
    )
    complete_message(
        session,
        resources,
        message,
        record,
        response_id="response-snapshot-latest",
        parts=(TextPart(text="snapshot reads authorized"),),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )
    store = ContextSnapshotStore(session)
    covered_until = record.id + 1

    assert provider.read_snapshot(resources.thread_id) is None

    first = store.create(
        resources.thread_id,
        summary="first explicit summary",
        covered_until=covered_until,
    )
    second_message, second_record = enqueue_message(
        session,
        resources,
        sequence=2,
        content="advance snapshot coverage",
    )
    complete_message(
        session,
        resources,
        second_message,
        second_record,
        response_id="response-snapshot-latest-v2",
        parts=(TextPart(text="snapshot coverage advanced"),),
    )
    second_covered_until = second_record.id + 1

    second = store.create(
        resources.thread_id,
        summary="second explicit summary",
        covered_until=second_covered_until,
    )

    assert first == ContextSnapshot(
        thread_id=resources.thread_id,
        snapshot_version=1,
        summary="first explicit summary",
        covered_until=covered_until,
        created_at=first.created_at,
    )
    assert first.created_at.tzinfo is not None
    assert second.snapshot_version == 2

    with pytest.raises(ContextValidationError, match="must not precede"):
        store.create(
            resources.thread_id,
            summary="regressed snapshot",
            covered_until=covered_until,
        )
    assert provider.read_snapshot(resources.thread_id) == second
    assert second.covered_until == second_covered_until
    assert provider.read_snapshot(resources.thread_id) == second


@pytest.mark.parametrize("covered_until", [True, 0, -1, 1.5, "2"])
def test_context_snapshot_rejects_invalid_cursor_values(
    session: Session,
    covered_until: object,
) -> None:
    resources = create_thread_resources(session, "snapshot-invalid-cursor")

    with pytest.raises(ContextValidationError, match="positive integer"):
        ContextSnapshotStore(session).create(
            resources.thread_id,
            summary="invalid cursor",
            covered_until=covered_until,  # type: ignore[arg-type]
        )


def test_context_snapshot_rejects_unstable_boundaries(session: Session) -> None:
    resources = create_thread_resources(session, "snapshot-boundary")
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="pending snapshot turn",
    )
    store = ContextSnapshotStore(session)

    with pytest.raises(ContextValidationError, match="unfinished timeline turn"):
        store.create(
            resources.thread_id,
            summary="must not cross pending work",
            covered_until=record.id + 1,
        )

    complete_message(
        session,
        resources,
        message,
        record,
        response_id="response-snapshot-boundary",
        parts=(TextPart(text="now stable"),),
    )
    snapshot = store.create(
        resources.thread_id,
        summary="the completed turn is covered",
        covered_until=record.id + 1,
    )

    with pytest.raises(ContextValidationError, match="current thread history"):
        store.create(
            resources.thread_id,
            summary="must not skip future messages",
            covered_until=record.id + 2,
        )
    assert store.read_snapshot(resources.thread_id) == snapshot


def test_context_snapshot_waits_for_referenced_artifacts(session: Session) -> None:
    resources = create_thread_resources(session, "snapshot-artifact")
    artifact_id = "artifact-snapshot-late"
    response_id = "response-snapshot-artifact"
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="artifact arrives separately",
    )
    complete_message(
        session,
        resources,
        message,
        record,
        response_id=response_id,
        parts=(ArtifactRefPart(artifact_id=artifact_id),),
    )
    store = ContextSnapshotStore(session)

    with pytest.raises(ContextValidationError, match="unresolved artifact reference"):
        store.create(
            resources.thread_id,
            summary="must wait for the referenced artifact",
            covered_until=record.id + 1,
        )
    assert store.read_snapshot(resources.thread_id) is None

    persist_artifact(
        session,
        artifact_id=artifact_id,
        response_id=response_id,
    )
    snapshot = store.create(
        resources.thread_id,
        summary="the artifact reference is now stable",
        covered_until=record.id + 1,
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    assert snapshot.snapshot_version == 1
    assert [
        entry.artifact_id for entry in provider.read(resources.thread_id) if entry.artifact_id
    ] == [artifact_id]


def test_context_snapshot_rejects_artifact_owned_by_another_response(
    session: Session,
) -> None:
    resources = create_thread_resources(session, "snapshot-artifact-owner")
    artifact_id = "artifact-snapshot-wrong-owner"
    response_id = "response-snapshot-owner"
    persist_artifact(
        session,
        artifact_id=artifact_id,
        response_id="response-snapshot-other",
    )
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="artifact ownership must match",
    )
    complete_message(
        session,
        resources,
        message,
        record,
        response_id=response_id,
        parts=(ArtifactRefPart(artifact_id=artifact_id),),
    )
    store = ContextSnapshotStore(session)

    with pytest.raises(ContextValidationError, match="unresolved artifact reference"):
        store.create(
            resources.thread_id,
            summary="must reject mismatched artifact ownership",
            covered_until=record.id + 1,
        )
    assert store.read_snapshot(resources.thread_id) is None


def test_context_snapshot_validates_only_new_artifact_coverage(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = create_thread_resources(session, "snapshot-artifact-incremental")
    store = ContextSnapshotStore(session)

    def complete_artifact_turn(
        sequence: int,
        *,
        artifact_count: int,
    ) -> HermesDispatchRecord:
        response_id = f"response-snapshot-incremental-{sequence}"
        artifact_ids = tuple(
            f"artifact-snapshot-incremental-{sequence}-{ordinal}"
            for ordinal in range(artifact_count)
        )
        message, record = enqueue_message(
            session,
            resources,
            sequence=sequence,
            content=f"artifact turn {sequence}",
        )
        complete_message(
            session,
            resources,
            message,
            record,
            response_id=response_id,
            parts=tuple(ArtifactRefPart(artifact_id=artifact_id) for artifact_id in artifact_ids),
        )
        for artifact_id in artifact_ids:
            persist_artifact(
                session,
                artifact_id=artifact_id,
                response_id=response_id,
            )
        return record

    first_record = complete_artifact_turn(1, artifact_count=1)
    store.create(
        resources.thread_id,
        summary="first artifact turn",
        covered_until=first_record.id + 1,
    )
    second_record = complete_artifact_turn(2, artifact_count=5)
    second_covered_until = second_record.id + 1

    validate_envelope = Mock(wraps=ResponseEnvelope.model_validate)
    monkeypatch.setattr(
        ResponseEnvelope,
        "model_validate",
        staticmethod(validate_envelope),
    )
    monkeypatch.setattr(
        "cf_agent_gateway.context.storage._ARTIFACT_LOOKUP_BATCH_SIZE",
        2,
    )

    artifact_selects: list[str] = []

    def capture_artifact_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "from artifacts" in " ".join(statement.lower().split()):
            artifact_selects.append(statement)

    engine = session.get_bind()
    # Ensure a point-lookup implementation would issue SQL and be counted here.
    session.expunge_all()
    event.listen(engine, "before_cursor_execute", capture_artifact_selects)
    try:
        snapshot = store.create(
            resources.thread_id,
            summary="both artifact turns",
            covered_until=second_covered_until,
        )
        same_coverage_snapshot = store.create(
            resources.thread_id,
            summary="same coverage, revised summary",
            covered_until=second_covered_until,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_artifact_selects)

    assert snapshot.snapshot_version == 2
    assert same_coverage_snapshot.snapshot_version == 3
    validate_envelope.assert_called_once()
    assert validate_envelope.call_args.args[0]["response_id"] == ("response-snapshot-incremental-2")
    assert [statement.count("?") for statement in artifact_selects] == [2, 2, 1]


def test_context_snapshot_cursor_keeps_pre_dispatch_message_in_tail(
    session: Session,
) -> None:
    resources = create_thread_resources(session, "snapshot-pre-dispatch")
    message = persist_message(
        session,
        resources,
        sequence=1,
        content="persisted before dispatch enqueue",
    )
    store = ContextSnapshotStore(session)
    snapshot = store.create(
        resources.thread_id,
        summary="No completed turns yet.",
        covered_until=1,
    )

    record = enqueue_persisted_message(session, resources, message)
    complete_message(
        session,
        resources,
        message,
        record,
        response_id="response-snapshot-pre-dispatch",
        parts=(TextPart(text="arrived after the snapshot"),),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    complete_timeline = provider.read(resources.thread_id)
    tail_timeline = provider.read_timeline(
        resources.thread_id,
        snapshot.covered_until,
        None,
    )

    assert record.id >= snapshot.covered_until
    assert tail_timeline == complete_timeline
    assert {entry.dispatch_id for entry in tail_timeline} == {record.id}


def test_context_timeline_rejects_invalid_dispatch_cursor_bounds(session: Session) -> None:
    resources = create_thread_resources(session, "timeline-invalid-cursor")
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="authorize cursor validation",
    )
    complete_message(
        session,
        resources,
        message,
        record,
        response_id="response-timeline-invalid-cursor",
        parts=(TextPart(text="authorized"),),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    for from_, to in ((0, None), (True, None), (1.5, None), (1, 1), (2, 1)):
        with pytest.raises(ContextValidationError):
            provider.read_timeline(resources.thread_id, from_, to)  # type: ignore[arg-type]


def test_context_snapshot_versions_serialize_concurrent_sqlite_writers(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'snapshots.db').as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as setup_session:
            resources = create_thread_resources(setup_session, "snapshot-concurrent")
            message, record = enqueue_message(
                setup_session,
                resources,
                sequence=1,
                content="concurrent snapshot source",
            )
            complete_message(
                setup_session,
                resources,
                message,
                record,
                response_id="response-snapshot-concurrent",
                parts=(TextPart(text="stable source"),),
            )
            covered_until = record.id + 1

        barrier = Barrier(2)

        def create_snapshot(summary: str) -> ContextSnapshot:
            with factory() as worker_session:
                barrier.wait(timeout=5)
                return ContextSnapshotStore(worker_session).create(
                    resources.thread_id,
                    summary=summary,
                    covered_until=covered_until,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(create_snapshot, "concurrent summary one"),
                executor.submit(create_snapshot, "concurrent summary two"),
            ]
            snapshots = [future.result(timeout=10) for future in futures]

        assert {snapshot.snapshot_version for snapshot in snapshots} == {1, 2}
        with factory() as read_session:
            latest = ContextSnapshotStore(read_session).read_snapshot(resources.thread_id)
            assert latest is not None
            assert latest.snapshot_version == 2
    finally:
        engine.dispose()


def test_context_snapshot_preserves_and_partitions_the_timeline(session: Session) -> None:
    resources = create_thread_resources(session, "snapshot-retention")
    first_message, first_record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="fact covered by snapshot",
        occurred_at=datetime(2026, 8, 10, 8, 30, tzinfo=UTC),
    )
    complete_message(
        session,
        resources,
        first_message,
        first_record,
        response_id="response-snapshot-covered",
        parts=(TextPart(text="covered answer"),),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )
    original_prefix = provider.read(resources.thread_id)
    cutoff = first_record.id + 1
    snapshot = ContextSnapshotStore(session).create(
        resources.thread_id,
        summary="The first complete turn is covered.",
        covered_until=cutoff,
    )

    assert provider.read(resources.thread_id) == original_prefix
    assert session.get(Message, first_message.id) is not None

    second_message, second_record = enqueue_message(
        session,
        resources,
        sequence=2,
        content="late event-time fact after snapshot",
        occurred_at=datetime(2026, 8, 10, 8, tzinfo=UTC),
    )
    complete_message(
        session,
        resources,
        second_message,
        second_record,
        response_id="response-snapshot-tail",
        parts=(TextPart(text="tail answer"),),
    )

    complete_timeline = provider.read(resources.thread_id)
    covered_timeline = provider.read_timeline(resources.thread_id, None, cutoff)
    tail_timeline = provider.read_timeline(resources.thread_id, cutoff, None)

    assert covered_timeline == original_prefix
    assert second_record.id >= cutoff
    assert second_message.id in {entry.message_id for entry in tail_timeline}
    assert covered_timeline + tail_timeline == complete_timeline
    assert {entry.message_id for entry in complete_timeline} == {
        first_message.id,
        second_message.id,
    }
    assert provider.read_snapshot(resources.thread_id) == snapshot


def test_context_snapshot_and_timeline_isolate_threads(session: Session) -> None:
    conversation_id = "snapshot-isolation@chatroom"
    first = create_group_sender_resources(
        session,
        "snapshot-first",
        conversation_id=conversation_id,
    )
    second = create_group_sender_resources(
        session,
        "snapshot-second",
        conversation_id=conversation_id,
    )
    first_message, first_record = enqueue_message(
        session,
        first,
        sequence=1,
        content="first snapshot thread",
    )
    complete_message(
        session,
        first,
        first_message,
        first_record,
        response_id="response-snapshot-first",
        parts=(TextPart(text="first isolated answer"),),
    )
    second_message, second_record = enqueue_message(
        session,
        second,
        sequence=2,
        content="second snapshot thread",
    )
    complete_message(
        session,
        second,
        second_message,
        second_record,
        response_id="response-snapshot-second",
        parts=(TextPart(text="second isolated answer"),),
    )
    store = ContextSnapshotStore(session)
    first_cutoff = first_record.id + 1
    second_cutoff = second_record.id + 1
    first_snapshot = store.create(
        first.thread_id,
        summary="first thread summary",
        covered_until=first_cutoff,
    )
    second_snapshot = store.create(
        second.thread_id,
        summary="second thread summary",
        covered_until=second_cutoff,
    )
    first_provider = create_context_provider(
        session,
        enterprise_identity_id=first.identity_id,
        thread_id=first.thread_id,
    )
    second_provider = create_context_provider(
        session,
        enterprise_identity_id=second.identity_id,
        thread_id=second.thread_id,
    )

    assert first_snapshot.snapshot_version == second_snapshot.snapshot_version == 1
    assert first_provider.read_snapshot(first.thread_id) == first_snapshot
    assert second_provider.read_snapshot(second.thread_id) == second_snapshot
    assert {entry.content for entry in first_provider.read_timeline(first.thread_id)} == {
        "first snapshot thread",
        "first isolated answer",
    }
    assert {entry.content for entry in second_provider.read_timeline(second.thread_id)} == {
        "second snapshot thread",
        "second isolated answer",
    }
    with pytest.raises(ContextAccessDeniedError):
        first_provider.read_snapshot(second.thread_id)


def test_thread_timeline_isolates_threads_and_excludes_unfinished_dispatches(
    session: Session,
) -> None:
    conversation_id = "shared-context@chatroom"
    first = create_group_sender_resources(
        session,
        "first",
        conversation_id=conversation_id,
    )
    second = create_group_sender_resources(
        session,
        "second",
        conversation_id=conversation_id,
    )
    first_message, first_record = enqueue_message(
        session, first, sequence=1, content="first thread question"
    )
    complete_message(
        session,
        first,
        first_message,
        first_record,
        response_id="response-first",
        parts=(TextPart(text="first thread answer"),),
    )
    second_message, second_record = enqueue_message(
        session, second, sequence=2, content="second thread question"
    )
    complete_message(
        session,
        second,
        second_message,
        second_record,
        response_id="response-second",
        parts=(TextPart(text="second thread answer"),),
    )
    _, unfinished = enqueue_message(session, first, sequence=3, content="unfinished current prompt")
    HermesDispatchRecordStore(session).claim(
        unfinished.id,
        claim_token="unfinished-context-claim",
    )

    first_tool = create_context_tool(
        session,
        enterprise_identity_id=first.identity_id,
        thread_id=first.thread_id,
    )
    second_tool = create_context_tool(
        session,
        enterprise_identity_id=second.identity_id,
        thread_id=second.thread_id,
    )
    first_context = {
        message.content
        for message in first_tool.read_current_thread().messages
        if message.content is not None
    }
    second_context = {
        message.content
        for message in second_tool.read_current_thread().messages
        if message.content is not None
    }

    assert first_context == {"first thread question", "first thread answer"}
    assert second_context == {"second thread question", "second thread answer"}
    forged_tool = create_context_tool(
        session,
        enterprise_identity_id=first.identity_id,
        thread_id=second.thread_id,
    )
    with pytest.raises(ContextAccessDeniedError):
        forged_tool.read_current_thread()


def test_thread_timeline_read_range_uses_occurred_at_half_open_bounds(
    session: Session,
) -> None:
    resources = create_thread_resources(session, "range")
    range_start = datetime(2026, 8, 10, 8, 30, tzinfo=UTC)
    range_end = range_start + timedelta(hours=1)
    first_message, first_record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="inside lower bound",
        occurred_at=range_start,
    )
    complete_message(
        session,
        resources,
        first_message,
        first_record,
        response_id="response-range-first",
        parts=(TextPart(text="first bounded answer"),),
    )
    second_message, second_record = enqueue_message(
        session,
        resources,
        sequence=2,
        content="outside upper bound",
        occurred_at=range_end,
    )
    complete_message(
        session,
        resources,
        second_message,
        second_record,
        response_id="response-range-second",
        parts=(TextPart(text="second bounded answer"),),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    entries = provider.read_range(
        resources.thread_id,
        occurred_at_gte=range_start,
        occurred_at_lt=range_end,
    )

    contents = {entry.content for entry in entries}
    assert {"inside lower bound", "first bounded answer"} <= contents
    assert "outside upper bound" not in contents
    assert "second bounded answer" not in contents


@pytest.mark.parametrize(
    ("range_start", "range_end"),
    [
        (
            datetime(2026, 8, 10, 8, 30),
            datetime(2026, 8, 10, 9, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 10, 8, 30, tzinfo=UTC),
            datetime(2026, 8, 10, 8, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 10, 9, 30, tzinfo=UTC),
            datetime(2026, 8, 10, 8, 30, tzinfo=UTC),
        ),
    ],
)
def test_thread_timeline_read_range_rejects_invalid_bounds(
    session: Session,
    range_start: datetime,
    range_end: datetime,
) -> None:
    resources = create_thread_resources(session, "invalid-range")
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="authorize invalid range test",
    )
    complete_message(
        session,
        resources,
        message,
        record,
        response_id="response-invalid-range",
        parts=(TextPart(text="authorized"),),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    with pytest.raises(ContextValidationError):
        provider.read_range(
            resources.thread_id,
            occurred_at_gte=range_start,
            occurred_at_lt=range_end,
        )


def test_context_tool_reads_recent_complete_turns_with_database_limit(
    session: Session,
) -> None:
    resources = create_thread_resources(session, "recent")
    for sequence in range(1, 4):
        message, record = enqueue_message(
            session,
            resources,
            sequence=sequence,
            content=f"question {sequence}",
        )
        complete_message(
            session,
            resources,
            message,
            record,
            response_id=f"response-recent-{sequence}",
            parts=(TextPart(text=f"answer {sequence}"),),
        )
    tool = create_context_tool(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    recent = tool.read_recent_messages(limit=2)

    assert [message.content for message in recent.messages] == [
        "question 2",
        "answer 2",
        "question 3",
        "answer 3",
    ]


def test_context_search_is_case_insensitive_and_literal(session: Session) -> None:
    resources = create_thread_resources(session, "search")
    message, record = enqueue_message(
        session,
        resources,
        sequence=1,
        content="Find token 100%_READY literally",
    )
    persist_artifact(
        session,
        artifact_id="artifact-search-result",
        response_id="response-search",
    )
    complete_message(
        session,
        resources,
        message,
        record,
        response_id="response-search",
        parts=(
            TextPart(text="No wildcard expansion was used."),
            ArtifactRefPart(artifact_id="artifact-search-result"),
        ),
    )
    provider = create_context_provider(
        session,
        enterprise_identity_id=resources.identity_id,
        thread_id=resources.thread_id,
    )

    literal_matches = provider.search(resources.thread_id, "%_ready")
    artifact_matches = provider.search(resources.thread_id, "ARTIFACT-SEARCH")

    assert [entry.content for entry in literal_matches] == ["Find token 100%_READY literally"]
    assert [entry.artifact_id for entry in artifact_matches] == ["artifact-search-result"]


def test_authorized_context_provider_delegates_mock_reads() -> None:
    thread_id = "thread-authorized"
    identity_id = "identity-authorized"
    persisted_at = datetime(2026, 8, 10, 10, tzinfo=UTC)
    covered_until = 2
    timeline = (
        ContextEntry(
            thread_id=thread_id,
            kind=ContextEntryKind.MESSAGE,
            message_id=1,
            content="persisted fact",
            occurred_at=persisted_at,
            received_at=persisted_at,
            dispatch_id=1,
            created_at=persisted_at,
        ),
    )
    snapshot = ContextSnapshot(
        thread_id=thread_id,
        snapshot_version=1,
        summary="persisted summary",
        covered_until=covered_until,
        created_at=persisted_at,
    )
    storage = Mock()
    storage.read.return_value = timeline
    storage.read_snapshot.return_value = snapshot
    storage.read_timeline.return_value = timeline
    provider: ContextProvider = AuthorizedContextProvider(
        storage,
        access_policy=ThreadContextAccessPolicy(
            enterprise_identity_id=identity_id,
            thread_id=thread_id,
        ),
        enterprise_identity_id=identity_id,
        thread_id=thread_id,
    )

    assert provider.read(thread_id) == timeline
    assert provider.read_snapshot(thread_id) == snapshot
    assert provider.read_timeline(thread_id, 1, covered_until) == timeline
    storage.read.assert_called_once_with(thread_id)
    storage.read_snapshot.assert_called_once_with(thread_id)
    storage.read_timeline.assert_called_once_with(thread_id, 1, covered_until)
    storage.search.assert_not_called()


def test_context_policy_requires_literal_boolean_decisions() -> None:
    with pytest.raises(ValueError, match="allowed must be a boolean"):
        ThreadContextAccessPolicy(
            enterprise_identity_id="identity-a",
            thread_id="thread-a",
            allowed="false",
        )

    storage = Mock()
    malformed_policy = Mock()
    malformed_policy.allows.return_value = "true"
    provider = AuthorizedContextProvider(
        storage,
        access_policy=malformed_policy,
        enterprise_identity_id="identity-a",
        thread_id="thread-a",
    )

    with pytest.raises(ContextAccessDeniedError):
        provider.read("thread-a")

    storage.read.assert_not_called()
    storage.read_snapshot.assert_not_called()
    storage.read_timeline.assert_not_called()
    storage.read_recent.assert_not_called()
    storage.search.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    ["read", "read_snapshot", "read_timeline", "read_range", "search"],
)
def test_context_provider_rejects_cross_thread_read_before_storage(operation: str) -> None:
    storage = Mock()
    provider = AuthorizedContextProvider(
        storage,
        access_policy=ThreadContextAccessPolicy(
            enterprise_identity_id="identity-a",
            thread_id="thread-a",
        ),
        enterprise_identity_id="identity-a",
        thread_id="thread-a",
    )

    with pytest.raises(ContextAccessDeniedError) as caught:
        if operation == "read":
            provider.read("thread-b")
        elif operation == "read_snapshot":
            provider.read_snapshot("thread-b")
        elif operation == "read_timeline":
            provider.read_timeline("thread-b")
        elif operation == "read_range":
            provider.read_range(
                "thread-b",
                occurred_at_gte=datetime(2026, 8, 10, tzinfo=UTC),
                occurred_at_lt=datetime(2026, 8, 11, tzinfo=UTC),
            )
        else:
            provider.search("thread-b", "secret")

    assert caught.value.code == "context_access_denied"
    storage.read.assert_not_called()
    storage.read_snapshot.assert_not_called()
    storage.read_timeline.assert_not_called()
    storage.read_recent.assert_not_called()
    storage.read_range.assert_not_called()
    storage.search.assert_not_called()
