"""Private Linux intake cache, not FileBrowser's formal storage or READY Artifact.

Publication is no-replace and fsynced. A database admission/registration/retention
transaction is still required before these references can enter an Agent task.
No guessed original filename becomes an on-disk path; no business files are run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from cf_agent_gateway.adapters.wechat.inbound_media import (
    MAX_MEDIA_BYTES,
    InboundMediaResult,
    MediaReadiness,
)
from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult, MediaFetchError

_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class StagedMedia:
    reference: str
    source_fingerprint: str
    size: int
    sha256: str
    original_comparison: str
    existing: bool


class InboundMediaStaging:
    """Preprovisioned private root; never creates/chmods the operator's root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        if (
            not self._root.is_absolute()
            or self._root == Path("/")
            or ".." in self._root.parts
            or os.name != "posix"
            or not hasattr(os, "O_NOFOLLOW")
        ):
            raise MediaFetchError("invalid_media_staging_root")

    def _root_fd(self) -> int:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self._root.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise MediaFetchError("media_staging_permissions")
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _verify(fd: int, name: str, expected: bytes) -> None:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != len(expected)
            ):
                raise MediaFetchError("media_staging_conflict")
            digest = hashlib.sha256()
            count = 0
            while True:
                chunk = os.read(file_fd, min(65536, len(expected) + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                digest.update(chunk)
                if count > len(expected):
                    raise MediaFetchError("media_staging_conflict")
            if count != len(expected) or digest.digest() != hashlib.sha256(expected).digest():
                raise MediaFetchError("media_staging_conflict")
            after = os.fstat(file_fd)
            if (info.st_ino, info.st_dev, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_ino,
                after.st_dev,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise MediaFetchError("media_staging_conflict")
        finally:
            os.close(file_fd)

    @classmethod
    def _publish(cls, fd: int, name: str, data: bytes) -> bool:
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            cls._verify(fd, name, data)
            # May be a recoverable prior publication lacking its directory sync.
            os.fsync(fd)
            return True
        temp = ".intake-" + uuid.uuid4().hex
        file_fd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        # Do not automatically remove a temp on I/O failure: retain local evidence.
        try:
            os.fchmod(file_fd, 0o600)
            remaining = memoryview(data)
            while remaining:
                written = os.write(file_fd, remaining)
                if written <= 0:
                    raise MediaFetchError("media_staging_write_failed")
                remaining = remaining[written:]
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        reused = False
        try:
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            reused = True
        os.unlink(temp, dir_fd=fd)  # Only this call's UUID temp, never prior records.
        os.fsync(fd)
        cls._verify(fd, name, data)
        return reused

    def publish(self, bound: BoundMediaResult) -> StagedMedia:
        if (
            not isinstance(bound, BoundMediaResult)
            or not isinstance(bound.source_fingerprint, str)
            or _HEX.fullmatch(bound.source_fingerprint) is None
            or not isinstance(bound.media, InboundMediaResult)
        ):
            raise MediaFetchError("invalid_media_staging_input")
        media = bound.media
        if (
            media.readiness is not MediaReadiness.READY
            or not isinstance(media.data, bytes)
            or not 1 <= len(media.data) <= MAX_MEDIA_BYTES
            or media.size != len(media.data)
            or media.sha256 != hashlib.sha256(media.data).hexdigest()
            or media.media_type not in {"image", "file"}
        ):
            raise MediaFetchError("media_not_ready_for_staging")
        # Keep variants separate. A later original does not overwrite a preview.
        key = hashlib.sha256(
            (bound.source_fingerprint + ":" + media.sha256).encode("ascii")
        ).hexdigest()
        metadata = {
            "schema": "cf-inbound-staging/v1",
            "source_fingerprint": bound.source_fingerprint,
            "blob": key + ".blob",
            "bytes": media.size,
            "sha256": media.sha256,
            "media_type": media.media_type,
            "filename": media.filename,
            "declared_quality": media.declared_quality,
            "original_comparison": media.original_comparison.value,
            "formal_archive": False,
        }
        raw = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(raw) > 16384:
            raise MediaFetchError("invalid_media_staging_input")
        fd = None
        try:
            fd = self._root_fd()
            self._publish(fd, key + ".blob", media.data)
            existing = self._publish(fd, key + ".json", raw)
            return StagedMedia(
                key,
                bound.source_fingerprint,
                media.size,
                media.sha256,
                media.original_comparison.value,
                existing,
            )
        except OSError:
            raise MediaFetchError("media_staging_io_failed") from None
        finally:
            if fd is not None:
                os.close(fd)
