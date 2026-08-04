from __future__ import annotations

from sqlalchemy.orm import Session

from cf_agent_gateway.identity.models import IdentityStatus
from cf_agent_gateway.identity.store import IdentityStore
from cf_agent_gateway.workspace.errors import (
    AuthorizedIdentityNotFoundError,
    AuthorizedIdentityUnavailableError,
    ThreadConflictError,
    ThreadNotFoundError,
    ThreadSourceBindingConflictError,
    ThreadUnavailableError,
    WorkspaceUnavailableError,
)
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadStatus,
    ThreadType,
    WorkspaceStatus,
)
from cf_agent_gateway.workspace.schemas import AuthorizedThreadRequest, HermesThreadBinding
from cf_agent_gateway.workspace.store import WorkspaceStore
from cf_agent_gateway.workspace.thread_keys import build_thread_key


class WorkspaceService:
    """Resolves workspaces and threads only after the caller has authorized the request.

    This service intentionally contains no access-control policy. Callers must complete
    access control before invoking either ``ensure_*_for_authorized_*`` method.
    """

    def __init__(self, session: Session) -> None:
        self._identity_store = IdentityStore(session)
        self._store = WorkspaceStore(session)

    def ensure_workspace_for_authorized_identity(
        self, enterprise_identity_id: str
    ) -> EmployeeWorkspace:
        """Return the stable workspace for an identity already authorized by the caller."""
        identity = self._identity_store.get_identity(enterprise_identity_id)
        if identity is None:
            raise AuthorizedIdentityNotFoundError(enterprise_identity_id)
        if identity.status is not IdentityStatus.ACTIVE:
            raise AuthorizedIdentityUnavailableError(identity.id, identity.status.value)

        workspace, _ = self._store.ensure_workspace(identity.id)
        if workspace.status is not WorkspaceStatus.ACTIVE:
            raise WorkspaceUnavailableError(workspace.id, workspace.status.value)
        return self._store.touch_workspace(workspace)

    def ensure_thread_for_authorized_request(
        self,
        *,
        enterprise_identity_id: str,
        platform: str,
        account_id: str,
        physical_conversation_id: str,
        conversation_type: str,
        sender_id: str,
    ) -> AIThread:
        """Return a stable AI thread for a request already authorized by the caller."""
        request = AuthorizedThreadRequest(
            enterprise_identity_id=enterprise_identity_id,
            platform=platform,
            account_id=account_id,
            physical_conversation_id=physical_conversation_id,
            conversation_type=conversation_type,
            sender_id=sender_id,
        )
        workspace = self.ensure_workspace_for_authorized_identity(request.enterprise_identity_id)
        thread_key = build_thread_key(
            platform=request.platform,
            account_id=request.account_id,
            physical_conversation_id=request.physical_conversation_id,
            conversation_type=request.conversation_type,
            sender_id=request.sender_id,
        )
        source_binding = self._store.get_source_binding(
            platform=request.platform,
            account_id=request.account_id,
            physical_conversation_id=request.physical_conversation_id,
            sender_id=request.sender_id,
        )
        if source_binding is not None:
            thread = self._bound_thread(source_binding.ai_thread_id)
            self._validate_thread_type(thread, request.conversation_type)
        else:
            thread, thread_created = self._store.ensure_thread(
                workspace_id=workspace.id,
                thread_type=request.conversation_type,
                thread_key=thread_key,
            )
            try:
                self._store.ensure_source_binding(
                    ai_thread_id=thread.id,
                    platform=request.platform,
                    account_id=request.account_id,
                    physical_conversation_id=request.physical_conversation_id,
                    sender_id=request.sender_id,
                )
            except ThreadSourceBindingConflictError as conflict:
                if thread_created:
                    self._store.discard_thread_if_unbound(thread.id)
                thread = self._bound_thread(conflict.existing_ai_thread_id)
                self._validate_thread_type(thread, request.conversation_type)

        if thread.status is not ThreadStatus.ACTIVE:
            raise ThreadUnavailableError(thread.id, thread.status.value)
        return self._store.touch_thread(thread)

    def bind_hermes_thread(self, ai_thread_id: str, hermes_thread_id: str | None) -> AIThread:
        binding = HermesThreadBinding(
            ai_thread_id=ai_thread_id,
            hermes_thread_id=hermes_thread_id,
        )
        thread = self._store.get_thread(binding.ai_thread_id)
        if thread is None:
            raise ThreadNotFoundError(binding.ai_thread_id)
        return self._store.bind_hermes_thread(thread, binding.hermes_thread_id)

    def _bound_thread(self, ai_thread_id: str) -> AIThread:
        thread = self._store.get_thread(ai_thread_id)
        if thread is None:
            raise ThreadNotFoundError(ai_thread_id)
        return thread

    @staticmethod
    def _validate_thread_type(thread: AIThread, expected_type: ThreadType) -> None:
        if thread.thread_type is not expected_type:
            raise ThreadConflictError(thread.workspace_id, thread.thread_key)
