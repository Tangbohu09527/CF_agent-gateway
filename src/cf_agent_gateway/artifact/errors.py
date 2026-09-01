from __future__ import annotations


class ArtifactError(RuntimeError):
    """Base class for stable Gateway artifact errors."""

    code = "artifact_error"


class ArtifactValidationError(ArtifactError, ValueError):
    code = "artifact_validation_error"


class ArtifactNotFoundError(ArtifactError, LookupError):
    code = "artifact_not_found"

    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        super().__init__(f"artifact not found: {artifact_id}")


class ArtifactStateError(ArtifactError):
    code = "artifact_state_error"

    def __init__(self, *, status: str, operation: str) -> None:
        self.status = status
        self.operation = operation
        super().__init__(f"artifact in status {status!r} cannot perform {operation!r}")


class ArtifactSizeLimitError(ArtifactValidationError):
    code = "artifact_size_limit_exceeded"

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        super().__init__("artifact exceeds the configured size limit")


class ArtifactSizeMismatchError(ArtifactValidationError):
    code = "artifact_size_mismatch"

    def __init__(self) -> None:
        super().__init__("artifact content does not match the expected size")


class ArtifactHashMismatchError(ArtifactValidationError):
    code = "artifact_hash_mismatch"

    def __init__(self) -> None:
        super().__init__("artifact content does not match the expected SHA-256")


class ArtifactStorageError(ArtifactError):
    code = "artifact_storage_error"

    def __init__(self, message: str = "artifact content could not be accessed") -> None:
        super().__init__(message)


class ArtifactStorageKeyError(ArtifactStorageError, ValueError):
    code = "artifact_storage_key_error"

    def __init__(self) -> None:
        super().__init__("artifact storage key is invalid")


class ArtifactIntegrityError(ArtifactStorageError):
    code = "artifact_integrity_error"

    def __init__(self) -> None:
        super().__init__("stored artifact content failed integrity verification")
