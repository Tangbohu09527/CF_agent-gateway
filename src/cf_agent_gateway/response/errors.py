from __future__ import annotations


class ResponsePersistenceError(RuntimeError):
    """Base class for stable response persistence errors."""

    code = "response_persistence_error"


class ResponseValidationError(ResponsePersistenceError):
    code = "response_validation_error"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"cannot persist Hermes response: {reason}")


class ResponseConflictError(ResponsePersistenceError):
    code = "response_conflict"

    def __init__(self, *, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__("Hermes response idempotency key has conflicting content or target")


class ResponseStateConflictError(ResponsePersistenceError):
    code = "response_state_conflict"

    def __init__(self, *, response_id: str, expected_status: str) -> None:
        self.response_id = response_id
        self.expected_status = expected_status
        super().__init__(f"Hermes response {response_id!r} is not in {expected_status!r} state")
