from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    HermesChatResult,
    HermesDeliveryError,
    HermesDispatchOutcome,
    HermesDispatchService,
    HermesResponseDeliveryOutcome,
    HermesResponseError,
    HermesResponseHandler,
    HermesResponseRelay,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace, ThreadSourceBinding
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

    def chat(self, content: str, *, hermes_thread_id: str | None = None) -> HermesChatResult:
        del hermes_thread_id
        self.contents.append(content)
        if self.error is not None:
            raise self.error
        return HermesChatResult(
            assistant_content=self.assistant_content,
            hermes_thread_id="hermes-response-thread",
        )


class RecordingWechatSender:
    def __init__(
        self,
        *,
        account_id: str = SOURCE_ACCOUNT_ID,
        error: Exception | None = None,
    ) -> None:
        self.account_id = account_id
        self.error = error
        self.messages: list[tuple[str, str]] = []

    def send_text(self, conversation_id: str, content: str) -> None:
        self.messages.append((conversation_id, content))
        if self.error is not None:
            raise self.error


class ControlledSenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResponseResources:
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


def create_response_resources(session: Session) -> ResponseResources:
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
            content=MESSAGE_CONTENT,
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
    return ResponseResources(
        identity_id=identity.id,
        workspace=workspace,
        thread=thread,
        message=message,
        admission=admission,
    )


def dispatch_response(
    session: Session,
    resources: ResponseResources,
    client: RecordingHermesClient | None = None,
) -> HermesDispatchOutcome:
    return HermesDispatchService(session, client or RecordingHermesClient()).dispatch(
        resources.admission
    )


def assert_delivery_error(
    handler: HermesResponseHandler,
    response: HermesDispatchOutcome,
    reason: str,
) -> None:
    with pytest.raises(HermesDeliveryError) as caught:
        handler.process(response)

    assert caught.value.code == "hermes_delivery_error"
    assert caught.value.reason == reason


def test_process_routes_assistant_response_to_bound_wechat_conversation(
    session: Session,
) -> None:
    resources = create_response_resources(session)
    response = dispatch_response(session, resources)
    sender = RecordingWechatSender()

    outcome = HermesResponseHandler(session, sender).process(response)

    assert outcome == HermesResponseDeliveryOutcome(
        message_id=resources.message.id,
        ai_thread_id=resources.thread.id,
        conversation_id=CONVERSATION_ID,
    )
    assert sender.messages == [(CONVERSATION_ID, ASSISTANT_CONTENT)]


def test_handle_delegates_delivery_and_returns_none(session: Session) -> None:
    resources = create_response_resources(session)
    response = dispatch_response(session, resources)
    sender = RecordingWechatSender()

    result = HermesResponseHandler(session, sender).handle(response)

    assert result is None
    assert sender.messages == [(CONVERSATION_ID, ASSISTANT_CONTENT)]


def test_response_relay_connects_dispatch_to_delivery(session: Session) -> None:
    resources = create_response_resources(session)
    client = RecordingHermesClient()
    sender = RecordingWechatSender()
    relay = HermesResponseRelay(
        HermesDispatchService(session, client),
        HermesResponseHandler(session, sender),
    )

    response = relay.dispatch(resources.admission)

    assert response.assistant_content == ASSISTANT_CONTENT
    assert client.contents == [MESSAGE_CONTENT]
    assert sender.messages == [(CONVERSATION_ID, ASSISTANT_CONTENT)]


def test_response_relay_delivers_cross_workspace_group_response(session: Session) -> None:
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
    sender = RecordingWechatSender()
    relay = HermesResponseRelay(
        HermesDispatchService(session, client),
        HermesResponseHandler(session, sender),
    )

    response = relay.dispatch(admission)

    assert response.workspace_id == participant_workspace.id
    assert response.ai_thread_id == thread.id
    assert sender.messages == [(conversation_id, ASSISTANT_CONTENT)]


def test_missing_source_binding_rejects_response_without_calling_sender(
    session: Session,
) -> None:
    resources = create_response_resources(session)
    response = dispatch_response(session, resources)
    binding = session.scalar(
        select(ThreadSourceBinding).where(ThreadSourceBinding.ai_thread_id == resources.thread.id)
    )
    assert binding is not None
    session.delete(binding)
    session.commit()
    sender = RecordingWechatSender()

    assert_delivery_error(
        HermesResponseHandler(session, sender),
        response,
        "source_binding_not_found",
    )

    assert sender.messages == []


def test_conversation_mismatch_rejects_response_without_calling_sender(
    session: Session,
) -> None:
    resources = create_response_resources(session)
    response = dispatch_response(session, resources)
    binding = session.scalar(
        select(ThreadSourceBinding).where(ThreadSourceBinding.ai_thread_id == resources.thread.id)
    )
    assert binding is not None
    binding.physical_conversation_id = "wxid-other-chat"
    session.commit()
    sender = RecordingWechatSender()

    assert_delivery_error(
        HermesResponseHandler(session, sender),
        response,
        "conversation_mismatch",
    )

    assert sender.messages == []


def test_sender_account_mismatch_rejects_response_without_sending(session: Session) -> None:
    resources = create_response_resources(session)
    response = dispatch_response(session, resources)
    sender = RecordingWechatSender(account_id="wxid-other-gateway")

    assert_delivery_error(
        HermesResponseHandler(session, sender),
        response,
        "sender_account_mismatch",
    )

    assert sender.messages == []


def test_sender_error_propagates_exactly(session: Session) -> None:
    resources = create_response_resources(session)
    response = dispatch_response(session, resources)
    sender_error = ControlledSenderError("controlled outbound failure")
    sender = RecordingWechatSender(error=sender_error)

    with pytest.raises(ControlledSenderError, match="controlled outbound failure") as caught:
        HermesResponseHandler(session, sender).process(response)

    assert caught.value is sender_error
    assert sender.messages == [(CONVERSATION_ID, ASSISTANT_CONTENT)]


def test_hermes_error_propagates_without_calling_sender(session: Session) -> None:
    resources = create_response_resources(session)
    hermes_error = HermesResponseError(operation="chat")
    client = RecordingHermesClient(error=hermes_error)
    sender = RecordingWechatSender()
    handler = HermesResponseHandler(session, sender)
    relay = HermesResponseRelay(HermesDispatchService(session, client), handler)

    with pytest.raises(HermesResponseError) as caught:
        relay.dispatch(resources.admission)

    assert caught.value is hermes_error
    assert client.contents == [MESSAGE_CONTENT]
    assert sender.messages == []
