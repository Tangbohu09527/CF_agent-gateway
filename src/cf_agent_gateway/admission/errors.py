class AdmissionError(Exception):
    """Base class for admission orchestration errors."""


class AdmissionInvariantError(AdmissionError):
    """Raised when an allowed access decision lacks required identity facts."""


class AdmissionPendingError(AdmissionError):
    """Raised while another evaluator owns a live admission claim."""

    def __init__(self, message_id: int) -> None:
        super().__init__("message admission is pending")
        self.message_id = message_id


class AdmissionStateConflictError(AdmissionError):
    """Raised when a claimed outcome cannot be completed with CAS."""

    def __init__(self, message_id: int) -> None:
        super().__init__("message admission state changed concurrently")
        self.message_id = message_id
