class AdmissionError(Exception):
    """Base class for admission orchestration errors."""


class AdmissionInvariantError(AdmissionError):
    """Raised when an allowed access decision lacks required identity facts."""
