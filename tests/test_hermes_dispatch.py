from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
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

SOURCE = "wechat"
SOURCE_ACCOUNT_ID = "wxid-gateway"
CONVERSATION_ID = "wxid-alice"
SENDER_ID = "wxid-alice"
MESSAGE_CONTENT = "please summarize the release notes"
ASSISTANT_CONTENT = "The release is ready."


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
        error: Exception | None = None,
    ) -> None:
        self.assistant_content = assistant_content
        self.error = error
        self.contents: list[str] = []

    def chat(self, content: str) -> str:
        self.contents.append(content)
        if self.error is not None:
            raise self.error
        return self.assistant_content


class ControlledClientError(RuntimeError):
    pass


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
) -> AIThread:
    return WorkspaceService(session).ensure_thread_for_authorized_request(
        enterprise_identity_id=identity_id,
        platform=SOURCE,
        account_id=SOURCE_ACCOUNT_ID,
        physical_conversation_id=conversation_id,
        conversation_type="private",
        sender_id=sender_id,
    )


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
    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id is None


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
    session.refresh(resources.thread)
    assert resources.thread.hermes_thread_id == "hermes-existing"
