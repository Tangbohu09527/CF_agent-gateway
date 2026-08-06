from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import suppress
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cf_agent_gateway.artifact.errors import (
    ArtifactError,
    ArtifactHashMismatchError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactSizeLimitError,
    ArtifactSizeMismatchError,
    ArtifactStateError,
    ArtifactStorageError,
    ArtifactStorageKeyError,
    ArtifactValidationError,
)
from cf_agent_gateway.artifact.models import Artifact, ArtifactKind, ArtifactStatus

DEFAULT_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
STREAM_CHUNK_SIZE = 64 * 1024
type ArtifactContent = bytes | bytearray | memoryview | BinaryIO


class ArtifactRepository:
    """Persist artifacts without exposing paths.

    The storage root must be private to the Gateway process. Storage keys are still
    validated and resolved beneath that root as a defense against corrupted metadata.
    """

    def __init__(
        self,
        session: Session,
        storage_root: str | os.PathLike[str],
        *,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        self._session = session
        self._max_artifact_bytes = _positive_integer(
            max_artifact_bytes,
            "max_artifact_bytes",
        )
        self._storage_root = _initialize_storage_root(storage_root)

    def create(
        self,
        *,
        response_id: str,
        kind: ArtifactKind | str,
        filename: str,
        mime_type: str,
        content: ArtifactContent | None = None,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> Artifact:
        response_id = _required_string(response_id, "response_id", max_length=255)
        artifact_kind = _artifact_kind(kind)
        filename = _safe_filename(filename)
        mime_type = _required_string(mime_type, "mime_type", max_length=255)
        if expected_size is not None:
            expected_size = _expected_size(expected_size, self._max_artifact_bytes)
        if expected_sha256 is not None:
            expected_sha256 = _sha256_digest(expected_sha256)
        if content is None and (expected_size is not None or expected_sha256 is not None):
            raise ArtifactValidationError("expected size and SHA-256 require artifact content")

        artifact_id = str(uuid4())
        artifact = Artifact(
            artifact_id=artifact_id,
            response_id=response_id,
            kind=artifact_kind,
            filename=filename,
            mime_type=mime_type,
            size=None,
            sha256=None,
            storage_key=_storage_key(artifact_id),
            status=ArtifactStatus.CREATED,
        )
        self._session.add(artifact)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        if content is None:
            return artifact
        return self.mark_ready(
            artifact.artifact_id,
            content,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    def get(self, artifact_id: str) -> Artifact | None:
        statement = (
            select(Artifact)
            .where(Artifact.artifact_id == artifact_id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def list_for_response(self, response_id: str) -> list[Artifact]:
        response_id = _required_string(response_id, "response_id", max_length=255)
        statement = (
            select(Artifact)
            .where(Artifact.response_id == response_id)
            .order_by(Artifact.artifact_id)
            .execution_options(populate_existing=True)
        )
        return list(self._session.scalars(statement))

    def mark_ready(
        self,
        artifact_id: str,
        content: ArtifactContent,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> Artifact:
        artifact = self._required_artifact(artifact_id)
        self._require_status(artifact, ArtifactStatus.CREATED, operation="mark_ready")
        if expected_size is not None:
            expected_size = _expected_size(expected_size, self._max_artifact_bytes)
        if expected_sha256 is not None:
            expected_sha256 = _sha256_digest(expected_sha256)

        immediate_size = _bytes_like_size(content)
        if immediate_size is not None and immediate_size > self._max_artifact_bytes:
            self._mark_failed_if_created(artifact.artifact_id)
            raise ArtifactSizeLimitError(self._max_artifact_bytes)

        ready_storage_key = _ready_storage_key(artifact.artifact_id)
        try:
            self._storage_path(artifact.storage_key)
            destination, staged, size, digest = self._stage_content(
                ready_storage_key,
                content,
            )
            if expected_size is not None and size != expected_size:
                raise ArtifactSizeMismatchError()
            if expected_sha256 is not None and digest != expected_sha256:
                raise ArtifactHashMismatchError()
        except ArtifactError:
            if "staged" in locals():
                _best_effort_unlink(staged)
            self._mark_failed_if_created(artifact.artifact_id)
            raise
        except Exception:
            if "staged" in locals():
                _best_effort_unlink(staged)
            self._mark_failed_if_created(artifact.artifact_id)
            raise ArtifactStorageError("artifact content could not be stored") from None

        statement = (
            update(Artifact)
            .where(
                Artifact.artifact_id == artifact.artifact_id,
                Artifact.status == ArtifactStatus.CREATED,
            )
            .values(
                size=size,
                sha256=digest,
                storage_key=ready_storage_key,
                status=ArtifactStatus.READY,
            )
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                _best_effort_unlink(staged)
                current = self._required_artifact(artifact.artifact_id)
                raise ArtifactStateError(
                    status=current.status.value,
                    operation="mark_ready",
                )
            try:
                os.replace(staged, destination)
                _fsync_directory(destination.parent)
            except (ArtifactStorageError, OSError):
                self._session.rollback()
                _best_effort_unlink(staged)
                _best_effort_unlink(destination)
                self._mark_failed_if_created(artifact.artifact_id)
                raise ArtifactStorageError("artifact content could not be stored") from None
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()
                self._cleanup_uncommitted_publish(
                    artifact.artifact_id,
                    ready_storage_key,
                    destination,
                )
                raise
        except Exception:
            self._session.rollback()
            _best_effort_unlink(staged)
            raise
        return self._required_artifact(artifact.artifact_id)

    def mark_failed(self, artifact_id: str) -> Artifact:
        artifact = self._required_artifact(artifact_id)
        if artifact.status is ArtifactStatus.FAILED:
            return artifact
        self._require_status(artifact, ArtifactStatus.CREATED, operation="mark_failed")
        if not self._mark_failed_if_created(artifact.artifact_id):
            current = self._required_artifact(artifact.artifact_id)
            if current.status is ArtifactStatus.FAILED:
                return current
            raise ArtifactStateError(status=current.status.value, operation="mark_failed")
        self._best_effort_remove_content(artifact.storage_key)
        return self._required_artifact(artifact.artifact_id)

    def expire(self, artifact_id: str) -> Artifact:
        artifact = self._required_artifact(artifact_id)
        if artifact.status is ArtifactStatus.EXPIRED:
            self._best_effort_remove_content(artifact.storage_key)
            return artifact

        statement = (
            update(Artifact)
            .where(
                Artifact.artifact_id == artifact.artifact_id,
                Artifact.status != ArtifactStatus.EXPIRED,
            )
            .values(status=ArtifactStatus.EXPIRED)
            .execution_options(synchronize_session=False)
        )
        quarantined: tuple[Path, Path] | None = None
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                current = self._required_artifact(artifact.artifact_id)
                if current.status is ArtifactStatus.EXPIRED:
                    self._best_effort_remove_content(current.storage_key)
                    return current
                raise ArtifactStateError(status=current.status.value, operation="expire")
            quarantined = self._quarantine_content(artifact.storage_key)
            self._session.commit()
        except Exception:
            self._session.rollback()
            if quarantined is not None:
                self._recover_failed_expiration(
                    artifact.artifact_id,
                    *quarantined,
                )
            raise
        if quarantined is not None:
            self._delete_quarantined_content(*quarantined)
        return self._required_artifact(artifact.artifact_id)

    def read(self, artifact_id: str) -> bytes:
        artifact = self._required_artifact(artifact_id)
        self._require_status(artifact, ArtifactStatus.READY, operation="read")
        if artifact.size is None or artifact.sha256 is None:
            raise ArtifactIntegrityError()
        content = self._read_content(artifact.storage_key)
        if len(content) != artifact.size:
            raise ArtifactIntegrityError()
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ArtifactIntegrityError()
        return content

    def _required_artifact(self, artifact_id: str) -> Artifact:
        artifact_id = _required_string(artifact_id, "artifact_id", max_length=36)
        artifact = self.get(artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id)
        return artifact

    @staticmethod
    def _require_status(
        artifact: Artifact,
        expected: ArtifactStatus,
        *,
        operation: str,
    ) -> None:
        if artifact.status is not expected:
            raise ArtifactStateError(status=artifact.status.value, operation=operation)

    def _mark_failed_if_created(self, artifact_id: str) -> bool:
        statement = (
            update(Artifact)
            .where(
                Artifact.artifact_id == artifact_id,
                Artifact.status == ArtifactStatus.CREATED,
            )
            .values(status=ArtifactStatus.FAILED)
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return result.rowcount == 1

    def _cleanup_uncommitted_publish(
        self,
        artifact_id: str,
        attempted_storage_key: str,
        destination: Path,
    ) -> None:
        try:
            current = self.get(artifact_id)
        except Exception:
            return
        if current is None or (
            current.status is not ArtifactStatus.READY
            or current.storage_key != attempted_storage_key
        ):
            _best_effort_unlink(destination)

    def _stage_content(
        self,
        storage_key: str,
        content: ArtifactContent,
    ) -> tuple[Path, Path, int, str]:
        stream, close_stream = _content_stream(content)
        destination = self._storage_path(storage_key)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = self._storage_path(storage_key)
            temporary_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".artifact-",
                dir=destination.parent,
            )
        except (OSError, RuntimeError):
            if close_stream:
                stream.close()
            raise ArtifactStorageError("artifact content could not be stored") from None

        temporary_path = Path(temporary_name)
        size = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(temporary_descriptor, "wb") as temporary_file:
                while True:
                    chunk = stream.read(STREAM_CHUNK_SIZE)
                    if chunk == b"":
                        break
                    if not isinstance(chunk, bytes):
                        raise ArtifactValidationError("artifact stream must return bytes")
                    size += len(chunk)
                    if size > self._max_artifact_bytes:
                        raise ArtifactSizeLimitError(self._max_artifact_bytes)
                    digest.update(chunk)
                    temporary_file.write(chunk)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
        except ArtifactError:
            temporary_path.unlink(missing_ok=True)
            raise
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise ArtifactStorageError("artifact content could not be stored") from None
        finally:
            if close_stream:
                stream.close()
        return destination, temporary_path, size, digest.hexdigest()

    def _read_content(self, storage_key: str) -> bytes:
        path = self._storage_path(storage_key)
        try:
            if not path.is_file():
                raise ArtifactStorageError()
            with path.open("rb") as artifact_file:
                content = artifact_file.read(self._max_artifact_bytes + 1)
        except ArtifactError:
            raise
        except OSError:
            raise ArtifactStorageError() from None
        if len(content) > self._max_artifact_bytes:
            raise ArtifactIntegrityError()
        return content

    def _remove_content(self, storage_key: str) -> None:
        path = self._storage_path(storage_key)
        try:
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        except (ArtifactStorageError, OSError):
            raise ArtifactStorageError("artifact content could not be removed") from None

    def _best_effort_remove_content(self, storage_key: str) -> None:
        with suppress(ArtifactError):
            self._remove_content(storage_key)

    def _quarantine_content(self, storage_key: str) -> tuple[Path, Path] | None:
        source = self._storage_path(storage_key)
        if not source.exists():
            return None
        quarantine = source.with_name(f".expired-{uuid4().hex}")
        try:
            os.replace(source, quarantine)
            _fsync_directory(source.parent)
        except (ArtifactStorageError, OSError):
            if quarantine.exists() and not source.exists():
                with suppress(OSError):
                    os.replace(quarantine, source)
            raise ArtifactStorageError("artifact content could not be expired") from None
        return source, quarantine

    @staticmethod
    def _restore_quarantined_content(source: Path, quarantine: Path) -> None:
        try:
            os.replace(quarantine, source)
            _fsync_directory(source.parent)
        except (ArtifactStorageError, OSError):
            raise ArtifactStorageError("artifact expiration could not be rolled back") from None

    def _recover_failed_expiration(
        self,
        artifact_id: str,
        source: Path,
        quarantine: Path,
    ) -> None:
        statement = (
            update(Artifact)
            .where(
                Artifact.artifact_id == artifact_id,
                Artifact.status != ArtifactStatus.EXPIRED,
            )
            .values(storage_key=Artifact.storage_key)
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
        except Exception:
            self._session.rollback()
            raise
        if result.rowcount != 1:
            self._session.rollback()
            try:
                quarantine.unlink(missing_ok=True)
                _fsync_directory(quarantine.parent)
            except (ArtifactStorageError, OSError):
                raise ArtifactStorageError(
                    "expired artifact content could not be removed"
                ) from None
            return
        try:
            self._restore_quarantined_content(source, quarantine)
        finally:
            self._session.rollback()

    @staticmethod
    def _delete_quarantined_content(source: Path, quarantine: Path) -> None:
        try:
            quarantine.unlink(missing_ok=True)
            _fsync_directory(quarantine.parent)
        except (ArtifactStorageError, OSError):
            with suppress(OSError):
                os.replace(quarantine, source)
            raise ArtifactStorageError("expired artifact content could not be removed") from None

    def _storage_path(self, storage_key: str) -> Path:
        parts = _storage_key_parts(storage_key)
        candidate = self._storage_root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            raise ArtifactStorageKeyError() from None
        if not resolved.is_relative_to(self._storage_root):
            raise ArtifactStorageKeyError()
        return resolved


def _initialize_storage_root(value: str | os.PathLike[str]) -> Path:
    if isinstance(value, str) and not value.strip():
        raise ArtifactValidationError("storage_root must not be empty")
    try:
        root = Path(value).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        raise ArtifactStorageError("artifact storage root is unavailable") from None
    if not resolved.is_dir():
        raise ArtifactStorageError("artifact storage root is unavailable")
    return resolved


def _content_stream(content: ArtifactContent) -> tuple[BinaryIO, bool]:
    if isinstance(content, (bytes, bytearray, memoryview)):
        return BytesIO(bytes(content)), True
    if not callable(getattr(content, "read", None)):
        raise ArtifactValidationError("artifact content must be bytes or a binary stream")
    return content, False


def _bytes_like_size(content: ArtifactContent) -> int | None:
    if isinstance(content, memoryview):
        return content.nbytes
    if isinstance(content, (bytes, bytearray)):
        return len(content)
    return None


def _best_effort_unlink(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise ArtifactStorageError("artifact storage directory could not be synchronized") from None


def _storage_key(artifact_id: str) -> str:
    return f"objects/{artifact_id[:2]}/{artifact_id}"


def _ready_storage_key(artifact_id: str) -> str:
    return f"objects/{artifact_id[:2]}/{artifact_id}-{uuid4().hex}"


def _storage_key_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value or ":" in value:
        raise ArtifactStorageKeyError()
    if value.startswith("/") or "//" in value:
        raise ArtifactStorageKeyError()
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ArtifactStorageKeyError()
    posix_key = PurePosixPath(value)
    windows_key = PureWindowsPath(value)
    if posix_key.is_absolute() or windows_key.is_absolute() or windows_key.drive:
        raise ArtifactStorageKeyError()
    return tuple(raw_parts)


def _artifact_kind(value: ArtifactKind | str) -> ArtifactKind:
    try:
        return ArtifactKind(value)
    except (TypeError, ValueError):
        raise ArtifactValidationError("artifact kind must be image or file") from None


def _safe_filename(value: object) -> str:
    filename = _required_string(value, "filename", max_length=255)
    if filename in {".", ".."} or any(character in filename for character in "/\\:"):
        raise ArtifactValidationError("filename must not contain a path")
    return filename


def _required_string(value: object, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactValidationError(f"{field_name} must not be empty")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ArtifactValidationError(f"{field_name} is too long")
    if any(ord(character) < 0x20 for character in normalized):
        raise ArtifactValidationError(f"{field_name} contains invalid characters")
    return normalized


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ArtifactValidationError(f"{field_name} must be a positive integer")
    return value


def _expected_size(value: object, max_size: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactValidationError("expected_size must be a nonnegative integer")
    if value > max_size:
        raise ArtifactSizeLimitError(max_size)
    return value


def _sha256_digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ArtifactValidationError("expected_sha256 must be a SHA-256 hex digest")
    normalized = value.lower()
    if any(character not in "0123456789abcdef" for character in normalized):
        raise ArtifactValidationError("expected_sha256 must be a SHA-256 hex digest")
    return normalized
