from __future__ import annotations

from contextlib import suppress
from typing import Protocol

from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesDispatchError,
    HermesResponseError,
    HermesTransportError,
)
from cf_agent_gateway.hermes.models import (
    HermesChatResult,
    HermesDispatchOutcome,
    HermesOperationStatus,
)
from cf_agent_gateway.hermes.store import HermesLedgerStore
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadStatus,
    ThreadType,
    WorkspaceStatus,
)
from cf_agent_gateway.workspace.store import WorkspaceStore

HERMES_THREAD_NAMESPACE = "v1:cf-agent-gateway"


class HermesChatClient(Protocol):
    def chat(self, content: str, *, hermes_thread_id: str | None = None) -> HermesChatResult: ...


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
        ledger_store: HermesLedgerStore | None = None,
    ) -> None:
        self._session = session
        self._client = client
        self._message_store = message_store if message_store is not None else MessageStore(session)
        self._workspace_store = (
            workspace_store if workspace_store is not None else WorkspaceStore(session)
        )
        self._ledger_store = (
            ledger_store if ledger_store is not None else HermesLedgerStore(session)
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
        claim = self._ledger_store.claim_dispatch(
            message_id=message.id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            requested_hermes_thread_id=requested_hermes_thread_id,
        )
        record = claim.record
        if record.workspace_id != workspace.id or record.ai_thread_id != thread.id:
            raise HermesDispatchError(reason="dispatch_record_target_mismatch")
        if not claim.acquired:
            if record.status is HermesOperationStatus.SUCCEEDED:
                if record.assistant_content is None:
                    raise HermesDispatchError(reason="dispatch_record_invalid")
                return HermesDispatchOutcome(
                    message_id=message.id,
                    workspace_id=record.workspace_id,
                    ai_thread_id=record.ai_thread_id,
                    assistant_content=record.assistant_content,
                )
            raise HermesDispatchError(reason="dispatch_in_progress")
        lease_token = record.lease_token
        if lease_token is None:
            raise HermesDispatchError(reason="dispatch_record_invalid")

        try:
            result = self._client.chat(
                message.content,
                hermes_thread_id=requested_hermes_thread_id,
            )
        except Exception as error:
            self._session.rollback()
            with suppress(Exception):
                if _external_result_is_ambiguous(error):
                    self._ledger_store.note_ambiguous_dispatch(
                        record,
                        lease_token=lease_token,
                        error_code=_operation_error_code(error),
                    )
                else:
                    self._ledger_store.fail_dispatch(
                        record,
                        lease_token=lease_token,
                        error_code=_operation_error_code(error),
                    )
            raise

        try:
            hermes_thread_advanced = self._workspace_store.advance_hermes_thread(
                thread,
                expected_hermes_thread_id=requested_hermes_thread_id,
                next_hermes_thread_id=result.hermes_thread_id,
                commit=False,
            )
            if not hermes_thread_advanced:
                raise HermesDispatchError(reason="hermes_thread_advanced_concurrently")
            completed = self._ledger_store.complete_dispatch(
                record,
                lease_token=lease_token,
                result_hermes_thread_id=result.hermes_thread_id,
                assistant_content=result.assistant_content,
            )
            if not completed:
                raise HermesDispatchError(reason="dispatch_lease_lost")
        except Exception:
            self._session.rollback()
            raise

        return HermesDispatchOutcome(
            message_id=message.id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            assistant_content=result.assistant_content,
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


def _hermes_thread_id_for_dispatch(thread: AIThread) -> str:
    if thread.hermes_thread_id is not None:
        return thread.hermes_thread_id
    return _initial_hermes_thread_id(thread)


def _initial_hermes_thread_id(thread: AIThread) -> str:
    return f"{HERMES_THREAD_NAMESPACE}:{thread.id}"


def _operation_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__


def _external_result_is_ambiguous(error: Exception) -> bool:
    if isinstance(error, HermesTransportError | HermesResponseError):
        return True
    return isinstance(error, HermesAPIError) and (
        error.status_code in {408, 429} or error.status_code >= 500
    )
