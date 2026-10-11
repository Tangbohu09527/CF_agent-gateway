"""Pure inbound file/image response validation used by durable media intake.

READY means inline bytes are available, not durable, authorized or safe to open.
HTTP limits, message/account binding, persistence and retries belong to the caller.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

MAX_MEDIA_BYTES = 10 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_QUALITIES = frozenset({"full", "standard", "thumbnail"})


class MediaReadiness(StrEnum):
    PENDING = "pending"
    UNSUPPORTED = "unsupported"
    READY = "ready"


class OriginalComparison(StrEnum):
    NOT_CHECKED = "not_checked"
    MATCH = "match"
    DIFFERENT = "different"


class InboundMediaError(ValueError):
    """Static codes only: never include payloads, credentials, filenames or URLs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExpectedOriginal:
    """Independently supplied expectation, never copied from the media response."""

    size: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.size) is not int
            or self.size < 1
            or not isinstance(self.sha256, str)
            or _SHA256.fullmatch(self.sha256) is None
        ):
            raise InboundMediaError("invalid_original_expectation")


@dataclass(frozen=True, slots=True)
class InboundMediaResult:
    readiness: MediaReadiness
    media_type: str
    data: bytes | None = field(default=None, repr=False)
    filename: str | None = field(default=None, repr=False)
    size: int | None = None
    sha256: str | None = None
    signature: str | None = None
    declared_quality: str | None = None
    original_comparison: OriginalComparison = OriginalComparison.NOT_CHECKED

    def safe_summary(self) -> dict[str, object]:
        return {
            "readiness": self.readiness.value,
            "media_type": self.media_type,
            "bytes": self.size,
            "sha256": self.sha256,
            "signature": self.signature,
            "declared_quality": self.declared_quality,
            "original_comparison": self.original_comparison.value,
            "durable_storage_verified": False,
            "upstream_download_queued_proven": False,
        }


def _metadata_string(payload: Mapping[str, object], key: str, limit: int) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        raise InboundMediaError("invalid_media_metadata")
    return value


def _filename(payload: Mapping[str, object]) -> str | None:
    name = _metadata_string(payload, "filename", 255)
    if name is not None and (name in {".", ".."} or any(c in name for c in "/\\:")):
        raise InboundMediaError("unsafe_media_filename")
    # Metadata only. Even a valid name must never become an operator-controlled
    # storage path without separate local/remote path authorization.
    return name


def _signature(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return "other"


def parse_inbound_media(
    payload: object,
    *,
    max_bytes: int = MAX_MEDIA_BYTES,
    expected: ExpectedOriginal | None = None,
) -> InboundMediaResult:
    """Parse the observed JSON response without I/O or automatic retry.

    Deliberately supports file/image only. Future media and URL-only transports
    require an explicit contract. A quality label is a claim, not original proof.
    The caller must cap the HTTP body BEFORE parsing JSON; this function bounds
    the Base64 decoding allocation, not an already allocated HTTP/JSON response.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_MEDIA_BYTES:
        raise InboundMediaError("invalid_media_limit")
    if expected is not None and not isinstance(expected, ExpectedOriginal):
        raise InboundMediaError("invalid_original_expectation")
    if not isinstance(payload, Mapping):
        raise InboundMediaError("invalid_media_response")
    if payload.get("success") is False or payload.get("error") not in (None, ""):
        raise InboundMediaError("upstream_media_error")
    kind = payload.get("type")
    if not isinstance(kind, str) or kind not in {"pending", "unsupported", "file", "image"}:
        raise InboundMediaError("unrecognized_media_type")

    # Never dereference URLs from untrusted media payloads or treat them as bytes.
    if payload.get("url") not in (None, ""):
        raise InboundMediaError("media_url_requires_separate_contract")
    name = _filename(payload)
    _metadata_string(payload, "format", 128)
    quality = _metadata_string(payload, "quality", 32)
    if quality is not None and quality not in _QUALITIES:
        raise InboundMediaError("unrecognized_image_quality")
    if kind != "image" and quality is not None:
        raise InboundMediaError("inconsistent_media_response")

    encoded = payload.get("data")
    if kind in {"pending", "unsupported"}:
        if encoded not in (None, ""):
            raise InboundMediaError("inconsistent_media_response")
        return InboundMediaResult(readiness=MediaReadiness(kind), media_type=kind, filename=name)
    if not isinstance(encoded, str) or not encoded:
        raise InboundMediaError("inline_media_data_missing")
    if len(encoded) > 4 * ((max_bytes + 2) // 3):
        raise InboundMediaError("media_too_large")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise InboundMediaError("invalid_media_base64") from None
    if not data:
        raise InboundMediaError("inline_media_data_missing")
    if len(data) > max_bytes:
        raise InboundMediaError("media_too_large")
    if base64.b64encode(data).decode("ascii") != encoded:
        raise InboundMediaError("invalid_media_base64")

    signature = _signature(data)
    if kind == "image" and signature not in {"jpeg", "png"}:
        raise InboundMediaError("unaccepted_image_signature")
    digest = hashlib.sha256(data).hexdigest()
    comparison = OriginalComparison.NOT_CHECKED
    if expected is not None:
        comparison = (
            OriginalComparison.MATCH
            if expected.size == len(data) and expected.sha256 == digest
            else OriginalComparison.DIFFERENT
        )
    return InboundMediaResult(
        readiness=MediaReadiness.READY,
        media_type=kind,
        data=data,
        filename=name,
        size=len(data),
        sha256=digest,
        signature=signature,
        declared_quality=quality,
        original_comparison=comparison,
    )
