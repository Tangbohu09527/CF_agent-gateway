from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat.outbound import WechatMessageSender
from cf_agent_gateway.admission import AdmissionOutcome
from cf_agent_gateway.hermes.errors import HermesDeliveryError
from cf_agent_gateway.hermes.models import (
    HermesDispatchOutcome,
    HermesResponseDeliveryOutcome,
)
from cf_agent_gateway.hermes.service import HermesDispatcher
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.workspace.models import AIThread, ThreadSourceBinding, ThreadStatus
from cf_agent_gateway.workspace.store import WorkspaceStore

WECHAT_PLATFORM = "wechat"


class HermesResponseProcessor(Protocol):
    def handle(self, response: HermesDispatchOutcome) -> None: ...


class HermesResponseRelay:
    """Decorate a Hermes dispatcher with successful-response delivery."""

    def __init__(
        self,
        dispatcher: HermesDispatcher,
        response_processor: HermesResponseProcessor,
    ) -> None:
        self._dispatcher = dispatcher
        self._response_processor = response_processor

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        response = self._dispatcher.dispatch(admission)
        self._response_processor.handle(response)
        return response


class HermesResponseHandler:
    """Route one successful Hermes dispatch result back to its source conversation."""

    def __init__(
        self,
        session: Session,
        sender: WechatMessageSender,
        *,
        message_store: MessageStore | None = None,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        self._session = session
        self._sender = sender
        self._message_store = message_store if message_store is not None else MessageStore(session)
        self._workspace_store = (
            workspace_store if workspace_store is not None else WorkspaceStore(session)
        )

    def handle(self, response: HermesDispatchOutcome) -> None:
        self.process(response)

    def process(self, response: HermesDispatchOutcome) -> HermesResponseDeliveryOutcome:
        message = self._message_store.get(response.message_id)
        if message is None:
            raise HermesDeliveryError(reason="message_not_found")

        thread = self._workspace_store.get_thread(response.ai_thread_id)
        if thread is None:
            raise HermesDeliveryError(reason="ai_thread_not_found")
        self._session.refresh(thread)
        self._validate_thread(thread, response)

        if message.source != WECHAT_PLATFORM:
            raise HermesDeliveryError(reason="unsupported_source")

        source_binding = self._workspace_store.get_source_binding(
            platform=message.source,
            account_id=message.source_account_id,
            physical_conversation_id=message.conversation_id,
            sender_id=message.sender_id,
        )
        if source_binding is None:
            bindings = self._workspace_store.list_source_bindings_for_thread(thread.id)
            reason = "conversation_mismatch" if bindings else "source_binding_not_found"
            raise HermesDeliveryError(reason=reason)
        if not self._binding_matches_response(source_binding, message, response):
            raise HermesDeliveryError(reason="conversation_mismatch")
        if self._sender.account_id != source_binding.account_id:
            raise HermesDeliveryError(reason="sender_account_mismatch")
        if not isinstance(response.assistant_content, str) or response.assistant_content == "":
            raise HermesDeliveryError(reason="empty_assistant_content")

        conversation_id = source_binding.physical_conversation_id
        self._sender.send_text(conversation_id, response.assistant_content)
        return HermesResponseDeliveryOutcome(
            message_id=message.id,
            ai_thread_id=thread.id,
            conversation_id=conversation_id,
        )

    @staticmethod
    def _validate_thread(thread: AIThread, response: HermesDispatchOutcome) -> None:
        if thread.workspace_id != response.workspace_id:
            raise HermesDeliveryError(reason="ai_thread_workspace_mismatch")
        if thread.status is not ThreadStatus.ACTIVE:
            raise HermesDeliveryError(reason="ai_thread_unavailable")

    @staticmethod
    def _binding_matches_response(
        binding: ThreadSourceBinding,
        message: Message,
        response: HermesDispatchOutcome,
    ) -> bool:
        return (
            binding.ai_thread_id == response.ai_thread_id
            and binding.platform == message.source
            and binding.account_id == message.source_account_id
            and binding.physical_conversation_id == message.conversation_id
            and binding.sender_id == message.sender_id
        )
