from __future__ import annotations


class DeliveryError(RuntimeError):
    """Base class for stable response delivery errors."""

    code = "delivery_error"


class RetryableDeliveryError(DeliveryError):
    code = "delivery_retryable_error"


class PermanentDeliveryError(DeliveryError):
    code = "delivery_permanent_error"


class UncertainDeliveryError(DeliveryError):
    code = "delivery_uncertain_error"


class DeliveryStateConflictError(DeliveryError):
    code = "delivery_state_conflict"

    def __init__(self, *, delivery_id: int, expected_status: str) -> None:
        self.delivery_id = delivery_id
        self.expected_status = expected_status
        super().__init__(
            f"delivery outbox record {delivery_id} is not in {expected_status!r} state"
        )
