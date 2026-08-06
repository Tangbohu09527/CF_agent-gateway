from __future__ import annotations

import hashlib
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from cf_agent_gateway.artifact import (
    ArtifactHashMismatchError,
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactRepository,
    ArtifactSizeLimitError,
    ArtifactStateError,
    ArtifactStatus,
    ArtifactStorageError,
    ArtifactStorageKeyError,
    ArtifactValidationError,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)

RESPONSE_ID = "response-001"
PAYLOAD = b"artifact payload"


class ExplodingStream:
    def __init__(self) -> None:
        self._reads = 0

    def read(self, size: int = -1) -> bytes:
        del size
        self._reads += 1
        if self._reads == 1:
            return b"partial"
        raise ValueError("controlled stream failure")


class ChunkedStream:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = iter(chunks)

    def read(self, size: int = -1) -> bytes:
        del size
        return next(self._chunks, b"")


class ControlledCommitError(RuntimeError):
    pass


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as database_session:
            yield database_session
    finally:
        engine.dispose()


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "artifact-storage"


def repository(
    session: Session,
    storage_root: Path,
    *,
    max_bytes: int = 1024,
) -> ArtifactRepository:
    return ArtifactRepository(session, storage_root, max_artifact_bytes=max_bytes)


def create_artifact(repo: ArtifactRepository, **overrides: object):
    values: dict[str, object] = {
        "response_id": RESPONSE_ID,
        "kind": ArtifactKind.FILE,
        "filename": "report.txt",
        "mime_type": "text/plain",
    }
    values.update(overrides)
    return repo.create(**values)  # type: ignore[arg-type]


def stored_path(storage_root: Path, storage_key: str) -> Path:
    return storage_root.joinpath(*storage_key.split("/"))


def test_initialize_database_creates_artifacts_table_with_required_columns(
    session: Session,
) -> None:
    inspector = inspect(session.get_bind())

    assert {column["name"] for column in inspector.get_columns("artifacts")} == {
        "artifact_id",
        "response_id",
        "kind",
        "filename",
        "mime_type",
        "size",
        "sha256",
        "storage_key",
        "status",
    }
    assert inspector.get_pk_constraint("artifacts")["constrained_columns"] == ["artifact_id"]
    assert any(
        index["column_names"] == ["response_id"] for index in inspector.get_indexes("artifacts")
    )


def test_artifact_lifecycle_created_ready_expired(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root)

    artifact = create_artifact(repo)

    assert artifact.status is ArtifactStatus.CREATED
    assert artifact.size is None
    assert artifact.sha256 is None
    assert artifact.storage_key.startswith("objects/")
    assert RESPONSE_ID not in artifact.storage_key
    assert artifact.filename not in artifact.storage_key
    with pytest.raises(ArtifactStateError):
        repo.read(artifact.artifact_id)

    ready = repo.mark_ready(artifact.artifact_id, BytesIO(PAYLOAD))

    assert ready.status is ArtifactStatus.READY
    assert ready.size == len(PAYLOAD)
    assert ready.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert repo.read(ready.artifact_id) == PAYLOAD
    assert repo.list_for_response(RESPONSE_ID) == [ready]
    assert stored_path(storage_root, ready.storage_key).read_bytes() == PAYLOAD

    expired = repo.expire(ready.artifact_id)

    assert expired.status is ArtifactStatus.EXPIRED
    assert repo.expire(expired.artifact_id) is expired
    assert not stored_path(storage_root, expired.storage_key).exists()
    with pytest.raises(ArtifactStateError):
        repo.read(expired.artifact_id)
    with pytest.raises(ArtifactStateError):
        repo.mark_ready(expired.artifact_id, PAYLOAD)


def test_artifact_lifecycle_created_failed_expired(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo)

    failed = repo.mark_failed(artifact.artifact_id)

    assert failed.status is ArtifactStatus.FAILED
    assert repo.mark_failed(failed.artifact_id) is failed
    with pytest.raises(ArtifactStateError):
        repo.mark_ready(failed.artifact_id, PAYLOAD)

    assert repo.expire(failed.artifact_id).status is ArtifactStatus.EXPIRED


def test_create_with_content_computes_hash_and_size(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root)

    artifact = create_artifact(
        repo,
        kind="image",
        filename="pixel.png",
        mime_type="image/png",
        content=PAYLOAD,
        expected_size=len(PAYLOAD),
        expected_sha256=hashlib.sha256(PAYLOAD).hexdigest().upper(),
    )

    assert artifact.kind is ArtifactKind.IMAGE
    assert artifact.status is ArtifactStatus.READY
    assert artifact.size == len(PAYLOAD)
    assert artifact.sha256 == hashlib.sha256(PAYLOAD).hexdigest()


def test_hash_is_computed_across_stream_chunks(
    session: Session,
    storage_root: Path,
) -> None:
    payload = (b"0123456789abcdef" * 8192) + b"tail"
    repo = repository(session, storage_root, max_bytes=len(payload))

    artifact = create_artifact(repo, content=BytesIO(payload))

    assert artifact.size == len(payload)
    assert artifact.sha256 == hashlib.sha256(payload).hexdigest()
    assert repo.read(artifact.artifact_id) == payload


def test_hash_mismatch_marks_artifact_failed_and_removes_content(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo)

    with pytest.raises(ArtifactHashMismatchError):
        repo.mark_ready(artifact.artifact_id, PAYLOAD, expected_sha256="0" * 64)

    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.FAILED
    assert not stored_path(storage_root, persisted.storage_key).exists()


def test_size_limit_accepts_exact_boundary(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root, max_bytes=4)

    artifact = create_artifact(repo, content=b"1234")

    assert artifact.status is ArtifactStatus.READY
    assert artifact.size == 4


def test_size_limit_rejects_cumulative_overflow_without_file_residue(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root, max_bytes=4)
    artifact = create_artifact(repo)

    with pytest.raises(ArtifactSizeLimitError) as caught:
        repo.mark_ready(
            artifact.artifact_id,
            ChunkedStream(b"12", b"34", b"5"),  # type: ignore[arg-type]
        )

    assert caught.value.max_size == 4
    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.FAILED
    assert not any(path.is_file() for path in storage_root.rglob("*"))


@pytest.mark.parametrize("content", [bytearray(b"12345"), memoryview(b"12345")])
def test_oversized_bytes_like_content_is_rejected_before_copy(
    session: Session,
    storage_root: Path,
    content: bytearray | memoryview,
) -> None:
    repo = repository(session, storage_root, max_bytes=4)
    artifact = create_artifact(repo)

    with pytest.raises(ArtifactSizeLimitError):
        repo.mark_ready(artifact.artifact_id, content)

    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.FAILED
    assert not any(path.is_file() for path in storage_root.rglob("*"))


def test_stream_failure_marks_artifact_failed_without_temporary_file(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo)

    with pytest.raises(ArtifactStorageError, match="could not be stored") as caught:
        repo.mark_ready(artifact.artifact_id, ExplodingStream())  # type: ignore[arg-type]

    assert "controlled stream failure" not in str(caught.value)
    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.FAILED
    assert not any(path.is_file() for path in storage_root.rglob("*"))


@pytest.mark.parametrize("max_bytes", [True, False, 0, -1, 1.5])
def test_invalid_size_limit_is_rejected(
    session: Session,
    storage_root: Path,
    max_bytes: object,
) -> None:
    with pytest.raises(ArtifactValidationError):
        ArtifactRepository(
            session,
            storage_root,
            max_artifact_bytes=max_bytes,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.txt",
        "..\\secret.txt",
        "/tmp/secret.txt",
        "C:\\secret.txt",
        "folder/report.txt",
        ".",
        "..",
    ],
)
def test_filename_cannot_supply_a_local_path(
    session: Session,
    storage_root: Path,
    filename: str,
) -> None:
    repo = repository(session, storage_root)

    with pytest.raises(ArtifactValidationError, match="filename must not contain a path"):
        create_artifact(repo, filename=filename)


@pytest.mark.parametrize(
    "storage_key",
    [
        "../outside",
        "objects/../../outside",
        "objects\\outside",
        "/absolute/path",
        "C:/absolute/path",
        "objects/artifact:stream",
        "//server/share",
        "objects//artifact",
        "objects/./artifact",
        "objects/\x00artifact",
    ],
)
def test_tampered_storage_key_cannot_escape_storage_root(
    session: Session,
    storage_root: Path,
    storage_key: str,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo)
    artifact.storage_key = storage_key
    session.commit()

    with pytest.raises(ArtifactStorageKeyError) as caught:
        repo.mark_ready(artifact.artifact_id, PAYLOAD)

    assert str(storage_root) not in str(caught.value)
    assert not (storage_root.parent / "outside").exists()
    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.FAILED


def test_symlinked_storage_parent_cannot_escape_root(
    session: Session,
    storage_root: Path,
    tmp_path: Path,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (storage_root / "objects").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ArtifactStorageKeyError) as caught:
        repo.mark_ready(artifact.artifact_id, PAYLOAD)

    assert str(storage_root) not in str(caught.value)
    assert not any(outside.iterdir())
    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.FAILED


def test_read_detects_content_tampering(
    session: Session,
    storage_root: Path,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo, content=PAYLOAD)
    stored_path(storage_root, artifact.storage_key).write_bytes(b"x" * len(PAYLOAD))

    with pytest.raises(ArtifactIntegrityError):
        repo.read(artifact.artifact_id)


def test_expire_restores_ready_content_when_database_commit_fails(
    session: Session,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo, content=PAYLOAD)
    original_commit = session.commit

    def fail_commit() -> None:
        raise ControlledCommitError("controlled commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)

    with pytest.raises(ControlledCommitError, match="controlled commit failure"):
        repo.expire(artifact.artifact_id)

    monkeypatch.setattr(session, "commit", original_commit)
    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.READY
    assert repo.read(artifact.artifact_id) == PAYLOAD


def test_expire_does_not_restore_content_after_ambiguous_committed_error(
    session: Session,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo, content=PAYLOAD)
    original_commit = session.commit

    def commit_then_fail() -> None:
        original_commit()
        raise ControlledCommitError("ambiguous committed failure")

    monkeypatch.setattr(session, "commit", commit_then_fail)

    with pytest.raises(ControlledCommitError, match="ambiguous committed failure"):
        repo.expire(artifact.artifact_id)

    monkeypatch.setattr(session, "commit", original_commit)
    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.EXPIRED
    assert not stored_path(storage_root, persisted.storage_key).exists()


def test_mark_ready_preserves_content_after_ambiguous_committed_error(
    session: Session,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository(session, storage_root)
    artifact = create_artifact(repo)
    original_commit = session.commit

    def commit_then_fail() -> None:
        original_commit()
        raise ControlledCommitError("ambiguous committed failure")

    monkeypatch.setattr(session, "commit", commit_then_fail)

    with pytest.raises(ControlledCommitError, match="ambiguous committed failure"):
        repo.mark_ready(artifact.artifact_id, PAYLOAD)

    monkeypatch.setattr(session, "commit", original_commit)
    persisted = repo.get(artifact.artifact_id)
    assert persisted is not None
    assert persisted.status is ArtifactStatus.READY
    assert repo.read(artifact.artifact_id) == PAYLOAD


def test_stale_mark_ready_cannot_overwrite_the_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "artifact-concurrency.db"
    storage_root = tmp_path / "artifact-storage"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as setup_session:
            artifact = create_artifact(repository(setup_session, storage_root))
            artifact_id = artifact.artifact_id
        with factory() as stale_session:
            stale_repo = repository(stale_session, storage_root)
            stale_artifact = stale_repo.get(artifact_id)
            assert stale_artifact is not None
            assert stale_artifact.status is ArtifactStatus.CREATED
            stale_session.expunge(stale_artifact)
            stale_session.rollback()

            with factory() as winner_session:
                winner_repo = repository(winner_session, storage_root)
                winner = winner_repo.mark_ready(artifact_id, b"winner payload")
                assert winner.status is ArtifactStatus.READY

            original_required_artifact = stale_repo._required_artifact
            lookups = 0

            def stale_then_current(requested_artifact_id: str):
                nonlocal lookups
                lookups += 1
                if lookups == 1:
                    return stale_artifact
                return original_required_artifact(requested_artifact_id)

            monkeypatch.setattr(stale_repo, "_required_artifact", stale_then_current)

            with pytest.raises(ArtifactStateError):
                stale_repo.mark_ready(artifact_id, b"losing payload")

            persisted = stale_repo.get(artifact_id)
            assert persisted is not None
            assert persisted.status is ArtifactStatus.READY
            assert stale_repo.read(artifact_id) == b"winner payload"
            assert persisted.size == len(b"winner payload")
            assert persisted.sha256 == hashlib.sha256(b"winner payload").hexdigest()
        assert not any(path.name.startswith(".artifact-") for path in storage_root.rglob("*"))
    finally:
        engine.dispose()
