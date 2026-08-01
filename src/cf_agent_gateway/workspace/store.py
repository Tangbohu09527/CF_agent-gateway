from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.workspace.errors import (
    HermesThreadConflictError,
    ThreadConflictError,
    ThreadSourceBindingConflictError,
)
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadSourceBinding,
    ThreadType,
    WorkspaceStatus,
)


class WorkspaceStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure_workspace(self, enterprise_identity_id: str) -> tuple[EmployeeWorkspace, bool]:
        existing = self.get_workspace_for_identity(enterprise_identity_id)
        if existing is not None:
            return existing, False

        workspace = EmployeeWorkspace(
            id=str(uuid4()),
            enterprise_identity_id=enterprise_identity_id,
            status=WorkspaceStatus.ACTIVE,
        )
        self._session.add(workspace)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_workspace_for_identity(enterprise_identity_id)
            if existing is not None:
                return existing, False
            raise
        return workspace, True

    def get_workspace_for_identity(self, enterprise_identity_id: str) -> EmployeeWorkspace | None:
        statement = select(EmployeeWorkspace).where(
            EmployeeWorkspace.enterprise_identity_id == enterprise_identity_id
        )
        return self._session.scalar(statement)

    def ensure_thread(
        self, *, workspace_id: str, thread_type: ThreadType, thread_key: str
    ) -> tuple[AIThread, bool]:
        existing = self.get_thread_by_key(workspace_id=workspace_id, thread_key=thread_key)
        if existing is not None:
            return self._compatible_thread_or_raise(existing, thread_type), False

        thread = AIThread(
            id=str(uuid4()),
            workspace_id=workspace_id,
            thread_type=thread_type,
            thread_key=thread_key,
        )
        self._session.add(thread)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_thread_by_key(workspace_id=workspace_id, thread_key=thread_key)
            if existing is not None:
                return self._compatible_thread_or_raise(existing, thread_type), False
            raise
        return thread, True

    def get_thread(self, ai_thread_id: str) -> AIThread | None:
        return self._session.get(AIThread, ai_thread_id)

    def get_thread_by_key(self, *, workspace_id: str, thread_key: str) -> AIThread | None:
        statement = select(AIThread).where(
            AIThread.workspace_id == workspace_id,
            AIThread.thread_key == thread_key,
        )
        return self._session.scalar(statement)

    def get_thread_by_hermes_thread_id(self, hermes_thread_id: str) -> AIThread | None:
        statement = select(AIThread).where(AIThread.hermes_thread_id == hermes_thread_id)
        return self._session.scalar(statement)

    def ensure_source_binding(
        self,
        *,
        ai_thread_id: str,
        platform: str,
        account_id: str,
        physical_conversation_id: str,
        sender_id: str | None,
    ) -> tuple[ThreadSourceBinding, bool]:
        existing = self.get_source_binding(
            platform=platform,
            account_id=account_id,
            physical_conversation_id=physical_conversation_id,
            sender_id=sender_id,
        )
        if existing is not None:
            return self._compatible_source_binding_or_raise(existing, ai_thread_id), False

        binding = ThreadSourceBinding(
            id=str(uuid4()),
            ai_thread_id=ai_thread_id,
            platform=platform,
            account_id=account_id,
            physical_conversation_id=physical_conversation_id,
            sender_id=sender_id,
        )
        self._session.add(binding)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_source_binding(
                platform=platform,
                account_id=account_id,
                physical_conversation_id=physical_conversation_id,
                sender_id=sender_id,
            )
            if existing is not None:
                return self._compatible_source_binding_or_raise(existing, ai_thread_id), False
            raise
        return binding, True

    def get_source_binding(
        self,
        *,
        platform: str,
        account_id: str,
        physical_conversation_id: str,
        sender_id: str | None,
    ) -> ThreadSourceBinding | None:
        statement = select(ThreadSourceBinding).where(
            ThreadSourceBinding.platform == platform,
            ThreadSourceBinding.account_id == account_id,
            ThreadSourceBinding.physical_conversation_id == physical_conversation_id,
            ThreadSourceBinding.sender_id == sender_id,
        )
        return self._session.scalar(statement)

    def bind_hermes_thread(self, thread: AIThread, hermes_thread_id: str | None) -> AIThread:
        ai_thread_id = thread.id
        if hermes_thread_id is not None:
            existing = self.get_thread_by_hermes_thread_id(hermes_thread_id)
            if existing is not None:
                return self._compatible_hermes_thread_or_raise(
                    existing,
                    requested_ai_thread_id=ai_thread_id,
                    hermes_thread_id=hermes_thread_id,
                )

        thread.hermes_thread_id = hermes_thread_id
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            if hermes_thread_id is not None:
                existing = self.get_thread_by_hermes_thread_id(hermes_thread_id)
                if existing is not None:
                    return self._compatible_hermes_thread_or_raise(
                        existing,
                        requested_ai_thread_id=ai_thread_id,
                        hermes_thread_id=hermes_thread_id,
                    )
            raise
        return thread

    def touch_workspace(self, workspace: EmployeeWorkspace) -> EmployeeWorkspace:
        workspace.last_active_at = datetime.now(UTC)
        self._session.commit()
        return workspace

    def touch_thread(self, thread: AIThread) -> AIThread:
        thread.last_active_at = datetime.now(UTC)
        self._session.commit()
        return thread

    @staticmethod
    def _compatible_thread_or_raise(thread: AIThread, thread_type: ThreadType) -> AIThread:
        if thread.thread_type is not thread_type:
            raise ThreadConflictError(thread.workspace_id, thread.thread_key)
        return thread

    @staticmethod
    def _compatible_hermes_thread_or_raise(
        thread: AIThread, *, requested_ai_thread_id: str, hermes_thread_id: str
    ) -> AIThread:
        if thread.id != requested_ai_thread_id:
            raise HermesThreadConflictError(
                hermes_thread_id=hermes_thread_id,
                existing_ai_thread_id=thread.id,
                requested_ai_thread_id=requested_ai_thread_id,
            )
        return thread

    @staticmethod
    def _compatible_source_binding_or_raise(
        binding: ThreadSourceBinding, requested_ai_thread_id: str
    ) -> ThreadSourceBinding:
        if binding.ai_thread_id != requested_ai_thread_id:
            raise ThreadSourceBindingConflictError(
                platform=binding.platform,
                account_id=binding.account_id,
                physical_conversation_id=binding.physical_conversation_id,
                sender_id=binding.sender_id,
                existing_ai_thread_id=binding.ai_thread_id,
                requested_ai_thread_id=requested_ai_thread_id,
            )
        return binding
