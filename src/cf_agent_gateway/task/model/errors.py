from __future__ import annotations

from cf_agent_gateway.task.model.models import HermesDispatchStatus


class HermesDispatchRecordError(RuntimeError):
    """Base class for stable Hermes dispatch record errors."""


class HermesDispatchAdmissionError(HermesDispatchRecordError):
    code = "hermes_dispatch_admission_error"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"cannot enqueue Hermes dispatch: {reason}")


class HermesDispatchTargetConflictError(HermesDispatchRecordError):
    code = "hermes_dispatch_target_conflict"

    def __init__(self, *, idempotency_key: str, existing_record_id: int) -> None:
        self.idempotency_key = idempotency_key
        self.existing_record_id = existing_record_id
        super().__init__(
            "Hermes dispatch idempotency key is already assigned to a different target"
        )


class HermesDispatchStateConflictError(HermesDispatchRecordError):
    code = "hermes_dispatch_state_conflict"

    def __init__(
        self,
        *,
        record_id: int,
        expected_status: HermesDispatchStatus,
    ) -> None:
        self.record_id = record_id
        self.expected_status = expected_status
        super().__init__(
            f"Hermes dispatch record {record_id} is not in {expected_status.value!r} state"
        )
