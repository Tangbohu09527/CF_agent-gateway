from __future__ import annotations


class WorkspaceError(Exception):
    """Base class for stable workspace domain errors."""

    code = "workspace_error"


class AuthorizedIdentityNotFoundError(WorkspaceError):
    code = "authorized_identity_not_found"

    def __init__(self, enterprise_identity_id: str) -> None:
        self.enterprise_identity_id = enterprise_identity_id
        super().__init__(f"enterprise identity not found: {enterprise_identity_id}")


class AuthorizedIdentityUnavailableError(WorkspaceError):
    code = "authorized_identity_unavailable"

    def __init__(self, enterprise_identity_id: str, status: str) -> None:
        self.enterprise_identity_id = enterprise_identity_id
        self.status = status
        super().__init__(f"enterprise identity is not active: {status}")


class WorkspaceUnavailableError(WorkspaceError):
    code = "workspace_unavailable"

    def __init__(self, workspace_id: str, status: str) -> None:
        self.workspace_id = workspace_id
        self.status = status
        super().__init__(f"employee workspace is not active: {status}")


class ThreadUnavailableError(WorkspaceError):
    code = "thread_unavailable"

    def __init__(self, ai_thread_id: str, status: str) -> None:
        self.ai_thread_id = ai_thread_id
        self.status = status
        super().__init__(f"AI thread is not active: {status}")


class ThreadNotFoundError(WorkspaceError):
    code = "thread_not_found"

    def __init__(self, ai_thread_id: str) -> None:
        self.ai_thread_id = ai_thread_id
        super().__init__(f"AI thread not found: {ai_thread_id}")


class ThreadConflictError(WorkspaceError):
    code = "thread_conflict"

    def __init__(self, workspace_id: str, thread_key: str) -> None:
        self.workspace_id = workspace_id
        self.thread_key = thread_key
        super().__init__("thread key is already assigned to an incompatible thread")


class ThreadRoutingConflictError(WorkspaceError):
    code = "thread_routing_conflict"

    def __init__(
        self,
        *,
        ai_thread_id: str,
        agent_profile_id: str,
        thread_policy: str,
    ) -> None:
        self.ai_thread_id = ai_thread_id
        self.agent_profile_id = agent_profile_id
        self.thread_policy = thread_policy
        super().__init__("AI thread is already assigned to a different V2 route")


class HermesThreadConflictError(WorkspaceError):
    code = "hermes_thread_conflict"

    def __init__(
        self,
        *,
        hermes_thread_id: str,
        existing_ai_thread_id: str,
        requested_ai_thread_id: str,
    ) -> None:
        self.hermes_thread_id = hermes_thread_id
        self.existing_ai_thread_id = existing_ai_thread_id
        self.requested_ai_thread_id = requested_ai_thread_id
        super().__init__("Hermes runtime thread is already bound to another AI thread")


class ThreadSourceBindingConflictError(WorkspaceError):
    code = "thread_source_binding_conflict"

    def __init__(
        self,
        *,
        platform: str,
        account_id: str,
        physical_conversation_id: str,
        sender_id: str | None,
        existing_ai_thread_id: str,
        requested_ai_thread_id: str,
    ) -> None:
        self.platform = platform
        self.account_id = account_id
        self.physical_conversation_id = physical_conversation_id
        self.sender_id = sender_id
        self.existing_ai_thread_id = existing_ai_thread_id
        self.requested_ai_thread_id = requested_ai_thread_id
        super().__init__("source conversation is already bound to another AI thread")
