from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    HermesChatResult,
    HermesDispatchError,
    HermesDispatchOutcome,
    HermesDispatchService,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadStatus,
    WorkspaceStatus,
)
from cf_agent_gateway.workspace.service import WorkspaceService
from cf_agent_gateway.workspace.store import WorkspaceStore

SOURCE = "wechat"
SOURCE_ACCOUNT_ID = "wxid-gateway"
CONVERSATION_ID = "wxid-alice"
SENDER_ID = "wxid-alice"
MESSAGE_CONTENT = "please summarize the release notes"
ASSISTANT_CONTENT = "The release is ready."
HERMES_THREAD_ID = "hermes-thread-001"


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


class RecordingHermesClient:
    def __init__(
        self,
        assistant_content: str = ASSISTANT_CONTENT,
        *,
        hermes_thread_id: str = HERMES_THREAD_ID,
        error: Exception | None = None,
    ) -> None:
        self.assistant_content = assistant_content
        self.hermes_thread_id = hermes_thread_id
        self.error = error
        self.contents: list[str] = []
        self.thread_ids: list[str | None] = []

    def chat(self, content: str, *, hermes_thread_id: str | None = None) -> HermesChatResult:
        self.contents.append(content)
        self.thread_ids.append(hermes_thread_id)
        if self.error is not None:
            raise self.error
        return HermesChatResult(
            assistant_content=self.assistant_content,
            hermes_thread_id=self.hermes_thread_id,
        )


class ControlledClientError(RuntimeError):
    pass


class ConcurrentFirstHermesClient:
    def __init__(self, calls: int) -> None:
        self._barrier = Barrier(calls)
        self.thread_ids: list[str] = []

    def chat(self, content: str, *, hermes_thread_id: str | None = None) -> HermesChatResult:
        assert content
        assert hermes_thread_id is not None
        self.thread_ids.append(hermes_thread_id)
        self._barrier.wait(timeout=10)
        return HermesChatResult(
            assistant_content=ASSISTANT_CONTENT,
            hermes_thread_id=hermes_thread_id,
        )


class ConcurrentRotatingHermesClient:
    def __init__(self, calls: int) -> None:
        self._barrier = Barrier(calls)
        self.thread_ids: list[str] = []

    def chat(self, content: str, *, hermes_thread_id: str | None = None) -> HermesChatResult:
        assert hermes_thread_id is not None
        self.thread_ids.append(hermes_thread_id)
        self._barrier.wait(timeout=10)
        suffix = "first" if content == MESSAGE_CONTENT else "second"
        return HermesChatResult(
            assistant_content=ASSISTANT_CONTENT,
            hermes_thread_id=f"hermes-rotated-{suffix}",
        )


class FailingHermesBindingStore(WorkspaceStore):
    def advance_hermes_thread(
        self,
        thread: AIThread,
        *,
        expected_hermes_thread_id: str,
        next_hermes_thread_id: str,
    ) -> bool:
        del expected_hermes_thread_id
        thread.hermes_thread_id = next_hermes_thread_id
        raise ControlledClientError("controlled binding failure")


@dataclass(frozen=True, slots=True)
class DispatchResources:
    identity_id: str
    workspace: EmployeeWorkspace
    thread: AIThread
    message: Message
    admission: AdmissionOutcome


def create_thread(
    session: Session,
    identity_id: str,
    *,
    conversation_id: str,
    sender_id: str,
    conversation_type: str = "private",
) -> AIThread:
    return WorkspaceService(session).ensure_thread_for_authorized_request(
        enterprise_identity_id=identity_id,
        platform=SOURCE,
        account_id=SOURCE_ACCOUNT_ID,
        physical_conversation_id=conversation_id,
        conversation_type=conversation_type,
        sender_id=sender_id,
    )


def initial_hermes_thread_id(thread: AIThread) -> str:
    return f"v1:cf-agent-gateway:{thread.id}"


def create_dispatch_resources(
    session: Session,
    *,
    content: str = MESSAGE_CONTENT,
) -> DispatchResources:
    identity = IdentityService(session).create_identity(
        employee_id="employee-alice",
        display_name="Alice",
    )
    thread = create_thread(
        session,
        identity.id,
        conversation_id=CONVERSATION_ID,
        sender_id=SENDER_ID,
    )
    workspace = session.get(EmployeeWorkspace, thread.workspace_id)
    assert workspace is not None

    message, created = MessageStore(session).create(
        MessageEvent(
            event_id="wechat:event-001",
            source=SOURCE,
            source_account_id=SOURCE_ACCOUNT_ID,
            source_message_id="server-001",
            conversation_id=CONVERSATION_ID,
            conversation_type="private",
            is_mentioned=None,
            is_self=False,
            sender_type="human",
            sender_id=SENDER_ID,
            sender_name="Alice",
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
    return DispatchResources(
        identity_id=identity.id,
        workspace=workspace,
        thread=thread,
        message=message,
        admission=admission,
    )


def create_follow_up_admission(
    session: Session,
    resources: DispatchResources,
) -> AdmissionOutcome:
    message, created = MessageStore(session).create(
        MessageEvent(
            event_id="wechat:event-002",
            source=SOURCE,
            source_account_id=SOURCE_ACCOUNT_ID,
            source_message_id="server-002",
            conversation_id=CONVERSATION_ID,
            conversation_type="private",
            is_mentioned=None,
            is_self=False,
            sender_type="human",
            sender_id=SENDER_ID,
            sender_name="Alice",
            message_type="text",
            raw_type=1,
            content="and list the breaking changes",
            timestamp=datetime(2026, 8, 1, 10, 16, tzinfo=UTC),
        )
    )
    assert created is True
    return replace(resources.admission, message_id=message.id)


def assert_dispatch_error(
    service: HermesDispatchService,
    admission: AdmissionOutcome,
    reason: str,
) -> None:
    with pytest.raises(HermesDispatchError) as caught:
        service.dispatch(admission)

    assert caught.value.code == "hermes_dispatch_error"
    assert caught.value.reason == reason


def test_dispatch_calls_client_with_persisted_message_and_returns_target(
    session: Session,
) -> None:
    resources = create_dispatch_resources(session)
    client = RecordingHermesClient()

    outcome = HermesDispatchService(session, client).dispatch(resources.admission)

    assert outcome == HermesDispatchOutcome(
        message_id=resources.message.id,
        workspace_id=resources.workspace.id,
        ai_thread_id=resources.thread.id,
        assistant_content=ASSISTANT_CONTENT,
    )
    assert client.contents == [MESSAGE_CONTENT]
    assert client.thread_ids == [initial_hermes_thread_id(resources.thread)]
    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id == HERMES_THREAD_ID


def test_repeated_dispatch_reuses_bound_hermes_thread(session: Session) -> None:
    resources = create_dispatch_resources(session)
    second_admission = create_follow_up_admission(session, resources)
    client = RecordingHermesClient()
    service = HermesDispatchService(session, client)

    first = service.dispatch(resources.admission)
    second = service.dispatch(second_admission)

    assert first.ai_thread_id == second.ai_thread_id == resources.thread.id
    assert client.contents == [MESSAGE_CONTENT, "and list the breaking changes"]
    assert client.thread_ids == [initial_hermes_thread_id(resources.thread), HERMES_THREAD_ID]
    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id == HERMES_THREAD_ID


def test_hermes_thread_advancement_reports_atomic_cas_result(session: Session) -> None:
    resources = create_dispatch_resources(session)
    store = WorkspaceStore(session)
    claimed_thread_id = initial_hermes_thread_id(resources.thread)
    store.claim_hermes_thread(resources.thread, claimed_thread_id)

    stale_advance = store.advance_hermes_thread(
        resources.thread,
        expected_hermes_thread_id="stale-hermes-thread",
        next_hermes_thread_id="hermes-rotated",
    )
    successful_advance = store.advance_hermes_thread(
        resources.thread,
        expected_hermes_thread_id=claimed_thread_id,
        next_hermes_thread_id="hermes-rotated",
    )

    assert stale_advance is False
    assert successful_advance is True
    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id == "hermes-rotated"


def test_concurrent_first_dispatches_share_one_hermes_thread(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrent-hermes.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as setup_session:
            resources = create_dispatch_resources(setup_session)
            second_admission = create_follow_up_admission(setup_session, resources)
            admissions = (
                resources.admission,
                second_admission,
            )
            expected_thread_id = initial_hermes_thread_id(resources.thread)

        client = ConcurrentFirstHermesClient(calls=len(admissions))

        def dispatch(admission: AdmissionOutcome) -> HermesDispatchOutcome:
            with factory() as dispatch_session:
                return HermesDispatchService(dispatch_session, client).dispatch(admission)

        with ThreadPoolExecutor(max_workers=len(admissions)) as executor:
            outcomes = list(executor.map(dispatch, admissions))

        assert {outcome.ai_thread_id for outcome in outcomes} == {resources.thread.id}
        assert client.thread_ids == [expected_thread_id, expected_thread_id]
        with factory() as verification_session:
            thread = verification_session.get(AIThread, resources.thread.id)
            assert thread is not None
            assert thread.hermes_thread_id == expected_thread_id
    finally:
        engine.dispose()


def test_concurrent_hermes_rotation_does_not_overwrite_winning_binding(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "concurrent-hermes-rotation.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as setup_session:
            resources = create_dispatch_resources(setup_session)
            second_admission = create_follow_up_admission(setup_session, resources)
            admissions = (resources.admission, second_admission)
            initial_thread_id = initial_hermes_thread_id(resources.thread)

        client = ConcurrentRotatingHermesClient(calls=len(admissions))

        def dispatch(admission: AdmissionOutcome) -> str:
            with factory() as dispatch_session:
                try:
                    HermesDispatchService(dispatch_session, client).dispatch(admission)
                except HermesDispatchError as error:
                    return error.reason
                return "success"

        with ThreadPoolExecutor(max_workers=len(admissions)) as executor:
            results = list(executor.map(dispatch, admissions))

        assert sorted(results) == ["hermes_thread_advanced_concurrently", "success"]
        assert client.thread_ids == [initial_thread_id, initial_thread_id]
        with factory() as verification_session:
            thread = verification_session.get(AIThread, resources.thread.id)
            assert thread is not None
            assert thread.hermes_thread_id in {
                "hermes-rotated-first",
                "hermes-rotated-second",
            }
    finally:
        engine.dispose()


def test_dispatch_saves_rotated_hermes_thread_id(session: Session) -> None:
    resources = create_dispatch_resources(session)
    WorkspaceService(session).bind_hermes_thread(resources.thread.id, "hermes-before-compression")
    client = RecordingHermesClient(hermes_thread_id="hermes-after-compression")

    HermesDispatchService(session, client).dispatch(resources.admission)

    assert client.thread_ids == ["hermes-before-compression"]
    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id == "hermes-after-compression"


def test_denied_admission_does_not_call_client(session: Session) -> None:
    resources = create_dispatch_resources(session)
    client = RecordingHermesClient()
    denied = AdmissionOutcome(
        message_id=resources.message.id,
        admitted=False,
        should_create_task=False,
        reason=AdmissionReason.ACCESS_DENIED,
    )

    assert_dispatch_error(
        HermesDispatchService(session, client),
        denied,
        "admission_not_allowed",
    )

    assert client.contents == []


@pytest.mark.parametrize(
    ("inactive_resource", "reason"),
    [
        ("thread", "ai_thread_unavailable"),
        ("workspace", "workspace_unavailable"),
    ],
)
def test_inactive_thread_or_workspace_does_not_call_client(
    session: Session,
    inactive_resource: str,
    reason: str,
) -> None:
    resources = create_dispatch_resources(session)
    client = RecordingHermesClient()
    if inactive_resource == "thread":
        resources.thread.status = ThreadStatus.DISABLED
    else:
        resources.workspace.status = WorkspaceStatus.DISABLED
    session.commit()

    assert_dispatch_error(
        HermesDispatchService(session, client),
        resources.admission,
        reason,
    )

    assert client.contents == []


def test_admission_target_workspace_mismatch_does_not_call_client(session: Session) -> None:
    resources = create_dispatch_resources(session)
    other_identity = IdentityService(session).create_identity(employee_id="employee-bob")
    other_thread = create_thread(
        session,
        other_identity.id,
        conversation_id="wxid-bob",
        sender_id="wxid-bob",
    )
    mismatched_admission = replace(
        resources.admission,
        workspace_id=other_thread.workspace_id,
    )
    client = RecordingHermesClient()

    assert_dispatch_error(
        HermesDispatchService(session, client),
        mismatched_admission,
        "ai_thread_workspace_mismatch",
    )

    assert client.contents == []


def test_group_thread_dispatch_allows_a_different_authorized_member(session: Session) -> None:
    identity_service = IdentityService(session)
    owner = identity_service.create_identity(employee_id="employee-group-owner")
    participant = identity_service.create_identity(employee_id="employee-group-participant")
    conversation_id = "operations@chatroom"
    thread = create_thread(
        session,
        owner.id,
        conversation_id=conversation_id,
        sender_id="wxid-owner",
        conversation_type="group",
    )
    participant_workspace = WorkspaceService(session).ensure_workspace_for_authorized_identity(
        participant.id
    )
    assert participant_workspace.id != thread.workspace_id
    owner_workspace = session.get(EmployeeWorkspace, thread.workspace_id)
    assert owner_workspace is not None
    owner_workspace.status = WorkspaceStatus.DISABLED
    session.commit()
    message, created = MessageStore(session).create(
        MessageEvent(
            event_id="wechat:event-group-participant",
            source=SOURCE,
            source_account_id=SOURCE_ACCOUNT_ID,
            source_message_id="server-group-participant",
            conversation_id=conversation_id,
            conversation_type="group",
            is_mentioned=True,
            is_self=False,
            sender_type="human",
            sender_id="wxid-participant",
            sender_name="Participant",
            message_type="text",
            raw_type=1,
            content=MESSAGE_CONTENT,
            timestamp=datetime(2026, 8, 1, 10, 16, tzinfo=UTC),
        )
    )
    assert created is True
    admission = AdmissionOutcome(
        message_id=message.id,
        admitted=True,
        should_create_task=True,
        reason=AdmissionReason.ALLOWED,
        enterprise_identity_id=participant.id,
        workspace_id=participant_workspace.id,
        ai_thread_id=thread.id,
    )
    client = RecordingHermesClient()

    outcome = HermesDispatchService(session, client).dispatch(admission)

    assert outcome.ai_thread_id == thread.id
    assert client.thread_ids == [initial_hermes_thread_id(thread)]


def test_message_source_binding_mismatch_does_not_call_client(session: Session) -> None:
    resources = create_dispatch_resources(session)
    other_thread = create_thread(
        session,
        resources.identity_id,
        conversation_id="wxid-other-chat",
        sender_id=SENDER_ID,
    )
    mismatched_admission = replace(resources.admission, ai_thread_id=other_thread.id)
    client = RecordingHermesClient()

    assert_dispatch_error(
        HermesDispatchService(session, client),
        mismatched_admission,
        "message_thread_mismatch",
    )

    assert client.contents == []


def test_empty_persisted_content_does_not_call_client(session: Session) -> None:
    resources = create_dispatch_resources(session, content="")
    client = RecordingHermesClient()

    assert_dispatch_error(
        HermesDispatchService(session, client),
        resources.admission,
        "empty_message_content",
    )

    assert client.contents == []


def test_client_error_propagates_without_mutating_hermes_thread_id(session: Session) -> None:
    resources = create_dispatch_resources(session)
    WorkspaceService(session).bind_hermes_thread(resources.thread.id, "hermes-existing")
    client_error = ControlledClientError("controlled Hermes failure")
    client = RecordingHermesClient(error=client_error)

    with pytest.raises(ControlledClientError, match="controlled Hermes failure") as caught:
        HermesDispatchService(session, client).dispatch(resources.admission)

    assert caught.value is client_error
    assert client.contents == [MESSAGE_CONTENT]
    assert client.thread_ids == ["hermes-existing"]
    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id == "hermes-existing"


def test_binding_failure_rolls_back_to_claimed_hermes_thread_id(session: Session) -> None:
    resources = create_dispatch_resources(session)
    claimed_thread_id = initial_hermes_thread_id(resources.thread)
    client = RecordingHermesClient(hermes_thread_id="hermes-returned")
    store = FailingHermesBindingStore(session)

    with pytest.raises(ControlledClientError, match="controlled binding failure"):
        HermesDispatchService(session, client, workspace_store=store).dispatch(resources.admission)

    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id == claimed_thread_id
