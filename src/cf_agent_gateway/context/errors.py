from __future__ import annotations


class ContextRuntimeError(RuntimeError):
    """Base error for the read-only context runtime boundary."""


class ContextAccessDeniedError(ContextRuntimeError):
    code = "context_access_denied"

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"context access denied for thread {thread_id!r}")


class ContextValidationError(ContextRuntimeError, ValueError):
    code = "context_request_invalid"
