from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.hermes.errors import HermesDispatchError
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadStatus,
    WorkspaceStatus,
)
from cf_agent_gateway.workspace.store import WorkspaceStore


class HermesChatClient(Protocol):
    def chat(self, content: str) -> str: ...


class HermesDispatcher(Protocol):
    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome: ...


class HermesDispatchService:
    """Dispatch an allowed, persisted message through its active AI thread."""

    def __init__(
        self,
        session: Session,
        client: HermesChatClient,
        *,
        message_store: MessageStore | None = None,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        self._session = session
        self._client = client
        self._message_store = message_store if message_store is not None else MessageStore(session)
        self._workspace_store = (
            workspace_store if workspace_store is not None else WorkspaceStore(session)
        )

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        workspace_id, ai_thread_id = self._allowed_target(admission)

        message = self._message_store.get(admission.message_id)
        if message is None:
            raise HermesDispatchError(reason="message_not_found")

        thread = self._session.get(AIThread, ai_thread_id)
        if thread is None:
            raise HermesDispatchError(reason="ai_thread_not_found")
        self._session.refresh(thread)
        if thread.workspace_id != workspace_id:
            raise HermesDispatchError(reason="ai_thread_workspace_mismatch")
        if thread.status is not ThreadStatus.ACTIVE:
            raise HermesDispatchError(reason="ai_thread_unavailable")

        workspace = self._session.get(EmployeeWorkspace, workspace_id)
        if workspace is None:
            raise HermesDispatchError(reason="workspace_not_found")
        self._session.refresh(workspace)
        if workspace.status is not WorkspaceStatus.ACTIVE:
            raise HermesDispatchError(reason="workspace_unavailable")
        if workspace.enterprise_identity_id != admission.enterprise_identity_id:
            raise HermesDispatchError(reason="workspace_identity_mismatch")

        source_binding = self._workspace_store.get_source_binding(
            platform=message.source,
            account_id=message.source_account_id,
            physical_conversation_id=message.conversation_id,
            sender_id=message.sender_id,
        )
        if source_binding is None:
            raise HermesDispatchError(reason="source_binding_not_found")
        if source_binding.ai_thread_id != ai_thread_id:
            raise HermesDispatchError(reason="message_thread_mismatch")
        if not message.content:
            raise HermesDispatchError(reason="empty_message_content")

        assistant_content = self._client.chat(message.content)
        return HermesDispatchOutcome(
            message_id=message.id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            assistant_content=assistant_content,
        )

    @staticmethod
    def _allowed_target(admission: AdmissionOutcome) -> tuple[str, str]:
        if not admission.admitted or admission.reason is not AdmissionReason.ALLOWED:
            raise HermesDispatchError(reason="admission_not_allowed")
        if admission.enterprise_identity_id is None:
            raise HermesDispatchError(reason="enterprise_identity_missing")
        if admission.workspace_id is None or admission.ai_thread_id is None:
            raise HermesDispatchError(reason="dispatch_target_missing")
        return admission.workspace_id, admission.ai_thread_id
