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
