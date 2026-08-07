from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.agent_profile import AgentProfile, AgentProfileStatus
from cf_agent_gateway.hermes.errors import HermesDispatchError
from cf_agent_gateway.hermes.models import HermesChatResult, HermesDispatchOutcome
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadStatus,
    ThreadType,
    WorkspaceStatus,
)
from cf_agent_gateway.workspace.store import WorkspaceStore
from cf_agent_gateway.workspace.thread_keys import build_v2_thread_key

HERMES_THREAD_NAMESPACE = "v1:cf-agent-gateway"


class HermesChatClient(Protocol):
    def chat(
        self,
        content: str,
        *,
        hermes_thread_id: str | None = None,
        profile_reference: str | None = None,
        profile_revision: int | None = None,
        thread_id: str | None = None,
        session_metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> HermesChatResult: ...


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
        return self._dispatch(admission, idempotency_key=None)

    def dispatch_record(self, record: HermesDispatchRecord) -> HermesDispatchOutcome:
        """Execute a claimed durable record without mutating the Message Archive."""

        if record.status is not HermesDispatchStatus.RUNNING or record.claim_token is None:
            raise HermesDispatchError(reason="dispatch_record_not_claimed")
        admission = AdmissionOutcome(
            message_id=record.message_id,
            admitted=True,
            should_create_task=True,
            reason=AdmissionReason.ALLOWED,
            enterprise_identity_id=record.enterprise_identity_id,
            workspace_id=record.workspace_id,
            ai_thread_id=record.ai_thread_id,
        )
        return self._dispatch(admission, idempotency_key=record.idempotency_key)

    def _dispatch(
        self,
        admission: AdmissionOutcome,
        *,
        idempotency_key: str | None,
    ) -> HermesDispatchOutcome:
        workspace_id, ai_thread_id = self._allowed_target(admission)

        message = self._message_store.get(admission.message_id)
        if message is None:
            raise HermesDispatchError(reason="message_not_found")

        thread = self._session.get(AIThread, ai_thread_id)
        if thread is None:
            raise HermesDispatchError(reason="ai_thread_not_found")
        self._session.refresh(thread)
        if thread.thread_type is ThreadType.PRIVATE and thread.workspace_id != workspace_id:
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

        profile = self._resolve_dispatch_profile(thread, message, admission)
        if not message.content:
            raise HermesDispatchError(reason="empty_message_content")

        thread = self._workspace_store.get_thread_for_update(ai_thread_id)
        if thread is None:
            raise HermesDispatchError(reason="ai_thread_not_found")
        if thread.thread_type is ThreadType.PRIVATE and thread.workspace_id != workspace_id:
            raise HermesDispatchError(reason="ai_thread_workspace_mismatch")
        if thread.status is not ThreadStatus.ACTIVE:
            raise HermesDispatchError(reason="ai_thread_unavailable")
        if thread.hermes_thread_id is None:
            thread = self._workspace_store.claim_hermes_thread(
                thread,
                _initial_hermes_thread_id(thread),
            )
        requested_hermes_thread_id = _hermes_thread_id_for_dispatch(thread)

        try:
            if profile is None:
                if idempotency_key is None:
                    result = self._client.chat(
                        message.content,
                        hermes_thread_id=requested_hermes_thread_id,
                    )
                else:
                    result = self._client.chat(
                        message.content,
                        hermes_thread_id=requested_hermes_thread_id,
                        idempotency_key=idempotency_key,
                    )
            elif idempotency_key is None:
                result = self._client.chat(
                    message.content,
                    hermes_thread_id=requested_hermes_thread_id,
                    profile_reference=profile.external_profile_ref,
                    profile_revision=profile.revision,
                    thread_id=thread.id,
                    session_metadata=self._session_metadata(message, thread, admission),
                )
            else:
                result = self._client.chat(
                    message.content,
                    hermes_thread_id=requested_hermes_thread_id,
                    profile_reference=profile.external_profile_ref,
                    profile_revision=profile.revision,
                    thread_id=thread.id,
                    session_metadata=self._session_metadata(message, thread, admission),
                    idempotency_key=idempotency_key,
                )
            hermes_thread_advanced = self._workspace_store.advance_hermes_thread(
                thread,
                expected_hermes_thread_id=requested_hermes_thread_id,
                next_hermes_thread_id=result.hermes_thread_id,
            )
            if not hermes_thread_advanced:
                raise HermesDispatchError(reason="hermes_thread_advanced_concurrently")
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        return HermesDispatchOutcome(
            message_id=message.id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            assistant_content=result.assistant_content,
            response=result.response,
        )

    def _resolve_dispatch_profile(
        self,
        thread: AIThread,
        message: Message,
        admission: AdmissionOutcome,
    ) -> AgentProfile | None:
        if thread.agent_profile_id is None and thread.thread_policy is None:
            source_binding = self._workspace_store.get_source_binding(
                platform=message.source,
                account_id=message.source_account_id,
                physical_conversation_id=message.conversation_id,
                sender_id=message.sender_id,
            )
            if source_binding is None:
                raise HermesDispatchError(reason="source_binding_not_found")
            if source_binding.ai_thread_id != thread.id:
                raise HermesDispatchError(reason="message_thread_mismatch")
            return None

        if thread.agent_profile_id is None or thread.thread_policy is None:
            raise HermesDispatchError(reason="v2_route_snapshot_invalid")
        profile = self._session.get(AgentProfile, thread.agent_profile_id)
        if profile is None:
            raise HermesDispatchError(reason="agent_profile_not_found")
        self._session.refresh(profile)
        if profile.status is not AgentProfileStatus.ACTIVE:
            raise HermesDispatchError(reason="agent_profile_unavailable")
        if admission.enterprise_identity_id is None:
            raise HermesDispatchError(reason="enterprise_identity_missing")

        expected_thread_key = build_v2_thread_key(
            platform=message.source,
            account_id=message.source_account_id,
            physical_conversation_id=message.conversation_id,
            conversation_type=message.conversation_type,
            sender_identity_id=admission.enterprise_identity_id,
            agent_profile_id=profile.id,
            agent_profile_revision=profile.revision,
            thread_policy=thread.thread_policy,
        )
        if expected_thread_key != thread.thread_key:
            raise HermesDispatchError(reason="message_thread_mismatch")
        return profile

    @staticmethod
    def _session_metadata(
        message: Message,
        thread: AIThread,
        admission: AdmissionOutcome,
    ) -> dict[str, object]:
        if admission.enterprise_identity_id is None or thread.thread_policy is None:
            raise HermesDispatchError(reason="v2_route_snapshot_invalid")
        return {
            "message_id": message.id,
            "source": message.source,
            "source_account_id": message.source_account_id,
            "conversation_id": message.conversation_id,
            "conversation_type": message.conversation_type,
            "enterprise_identity_id": admission.enterprise_identity_id,
            "sender_identity_id": admission.enterprise_identity_id,
            "sender_id": message.sender_id,
            "thread_policy": thread.thread_policy.value,
        }

    @staticmethod
    def _allowed_target(admission: AdmissionOutcome) -> tuple[str, str]:
        if not admission.admitted or admission.reason is not AdmissionReason.ALLOWED:
            raise HermesDispatchError(reason="admission_not_allowed")
        if admission.enterprise_identity_id is None:
            raise HermesDispatchError(reason="enterprise_identity_missing")
        if admission.workspace_id is None or admission.ai_thread_id is None:
            raise HermesDispatchError(reason="dispatch_target_missing")
        return admission.workspace_id, admission.ai_thread_id


def _hermes_thread_id_for_dispatch(thread: AIThread) -> str:
    if thread.hermes_thread_id is not None:
        return thread.hermes_thread_id
    return _initial_hermes_thread_id(thread)


def _initial_hermes_thread_id(thread: AIThread) -> str:
    return f"{HERMES_THREAD_NAMESPACE}:{thread.id}"
