from __future__ import annotations

import base64
import binascii
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import PurePath
from typing import Any

import httpx

from cf_agent_gateway.adapters.wechat.errors import (
    WechatAPIError,
    WechatResponseError,
    WechatTimeoutError,
    WechatTransportError,
)
from cf_agent_gateway.adapters.wechat.media import WechatMediaType
from cf_agent_gateway.adapters.wechat.outbound_http import (
    DEFAULT_TIMEOUT,
    WechatHttpMessageSender,
)

MAX_MEDIA_BYTES = 25 * 1024 * 1024
IMAGE_MIME_TYPES = frozenset({"image/gif", "image/jpeg", "image/png"})

_MIME_TYPE_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}\Z"
)
_FILE_MIME_TYPES_BY_SUFFIX = {
    ".7z": frozenset({"application/x-7z-compressed"}),
    ".csv": frozenset({"text/csv"}),
    ".doc": frozenset({"application/msword"}),
    ".docx": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    ),
    ".json": frozenset({"application/json"}),
    ".gz": frozenset({"application/gzip"}),
    ".html": frozenset({"text/html"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
    ".pdf": frozenset({"application/pdf"}),
    ".ppt": frozenset({"application/vnd.ms-powerpoint"}),
    ".pptx": frozenset(
        {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    ),
    ".rar": frozenset({"application/vnd.rar", "application/x-rar-compressed"}),
    ".rtf": frozenset({"application/rtf", "text/rtf"}),
    ".tar": frozenset({"application/x-tar"}),
    ".txt": frozenset({"text/plain"}),
    ".xls": frozenset({"application/vnd.ms-excel"}),
    ".xlsx": frozenset(
        {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    ),
    ".xml": frozenset({"application/xml", "text/xml"}),
    ".zip": frozenset({"application/zip", "application/x-zip-compressed"}),
}
_WINDOWS_INVALID_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"AUX", "CON", "CONIN$", "CONOUT$", "NUL", "PRN"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {
        f"{prefix}{suffix}"
        for prefix in ("COM", "LPT")
        for suffix in ("\u00b9", "\u00b2", "\u00b3")
    }
)


class WechatHttpMediaSender(WechatHttpMessageSender):
    """Bearer-authenticated agent-wechat V2 image and file sender."""

    def __init__(
        self,
        account_id: str,
        base_url: str,
        token_env: str,
        *,
        max_media_bytes: int = MAX_MEDIA_BYTES,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        environment_reader: Callable[[str], str | None] = os.getenv,
    ) -> None:
        self._max_media_bytes = _media_size_limit(max_media_bytes)
        super().__init__(
            account_id=account_id,
            base_url=base_url,
            token_env=token_env,
            timeout=timeout,
            transport=transport,
            environment_reader=environment_reader,
        )

    def send_media(
        self,
        conversation_id: str,
        media_type: WechatMediaType | str,
        data: bytes | bytearray | memoryview | str,
        mime_type: str,
        filename: str | None = None,
    ) -> dict[str, Any] | None:
        chat_id = _required_string(conversation_id, "conversation_id")
        normalized_type = _media_type(media_type)
        normalized_mime = _mime_type(mime_type)

        if normalized_type is WechatMediaType.IMAGE:
            if filename is not None:
                raise ValueError("filename is only valid for file media")
            _validate_image_mime(normalized_mime)
            normalized_filename = None
        else:
            normalized_filename = _filename(filename)
            _validate_file_mime(normalized_filename, normalized_mime)

        encoded_data, decoded_data = _media_data(data, max_bytes=self._max_media_bytes)

        if normalized_type is WechatMediaType.IMAGE:
            _validate_image_signature(decoded_data, normalized_mime)
            payload = {
                "chatId": chat_id,
                "image": {"data": encoded_data, "mimeType": normalized_mime},
            }
        else:
            assert normalized_filename is not None
            payload = {
                "chatId": chat_id,
                "file": {"data": encoded_data, "filename": normalized_filename},
            }

        return self._send_media(
            operation=f"send_{normalized_type.value}",
            payload=payload,
        )

    def _send_media(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        response: httpx.Response | None = None
        failure: str | None = None
        try:
            response = self._client.post("api/messages/send", json=payload)
        except httpx.TimeoutException:
            failure = "timeout"
        except httpx.RequestError:
            failure = "transport"

        if failure == "timeout":
            raise WechatTimeoutError(operation=operation)
        if failure == "transport" or response is None:
            raise WechatTransportError(operation=operation)

        if not 200 <= response.status_code < 300:
            raise WechatAPIError(operation=operation, status_code=response.status_code)
        if response.status_code == httpx.codes.NO_CONTENT:
            return None
        if not response.content:
            raise WechatResponseError(operation=operation)

        response_payload: Any = None
        invalid_json = False
        try:
            response_payload = response.json()
        except ValueError:
            invalid_json = True
        if (
            invalid_json
            or not isinstance(response_payload, Mapping)
            or response_payload.get("success") is not True
        ):
            raise WechatResponseError(operation=operation)
        return dict(response_payload)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _media_size_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_MEDIA_BYTES
    ):
        raise ValueError(f"max_media_bytes must be between 1 and {MAX_MEDIA_BYTES}")
    return value


def _media_type(value: object) -> WechatMediaType:
    normalized = _required_string(value, "media_type").lower()
    try:
        return WechatMediaType(normalized)
    except ValueError:
        raise ValueError("media_type must be image or file") from None


def _mime_type(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("mime_type must be a concrete MIME type without parameters")
    normalized = value.lower()
    if _MIME_TYPE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("mime_type must be a concrete MIME type without parameters")
    return normalized


def _media_data(
    value: object,
    *,
    max_bytes: int,
) -> tuple[str, bytes]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        decoded = bytes(value)
        if not decoded:
            raise ValueError("media data must not be empty")
        if len(decoded) > max_bytes:
            raise ValueError("media data exceeds the configured size limit")
        return base64.b64encode(decoded).decode("ascii"), decoded

    if not isinstance(value, str) or not value:
        raise ValueError("media data must be bytes or canonical Base64")

    max_encoded_length = 4 * ((max_bytes + 2) // 3)
    if len(value) > max_encoded_length:
        raise ValueError("media data exceeds the configured size limit")

    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError("media data must be canonical Base64") from None
    if not decoded:
        raise ValueError("media data must not be empty")
    if len(decoded) > max_bytes:
        raise ValueError("media data exceeds the configured size limit")
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("media data must be canonical Base64")
    return value, decoded


def _validate_image_mime(mime_type: str) -> None:
    if mime_type not in IMAGE_MIME_TYPES:
        raise ValueError("image mime_type must be image/png, image/jpeg, or image/gif")


def _validate_image_signature(data: bytes, mime_type: str) -> None:
    matches = False
    if mime_type == "image/png":
        matches = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif mime_type == "image/jpeg":
        matches = data.startswith(b"\xff\xd8\xff")
    elif mime_type == "image/gif":
        matches = data.startswith((b"GIF87a", b"GIF89a"))
    if not matches:
        raise ValueError("image data does not match mime_type")


def _filename(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("filename must be a safe basename")
    filename = value
    device_name = filename.split(".", maxsplit=1)[0].rstrip(" .").upper()
    if (
        filename in {".", ".."}
        or filename.endswith((".", " "))
        or any(character in _WINDOWS_INVALID_FILENAME_CHARACTERS for character in filename)
        or device_name in _WINDOWS_RESERVED_FILENAMES
        or any(
            unicodedata.category(character)[0] == "C"
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in filename
        )
    ):
        raise ValueError("filename must be a safe basename")
    if len(filename.encode("utf-8")) > 255:
        raise ValueError("filename must not exceed 255 UTF-8 bytes")
    return filename


def _validate_file_mime(filename: str, mime_type: str) -> None:
    expected = _FILE_MIME_TYPES_BY_SUFFIX.get(PurePath(filename).suffix.lower())
    if expected is None:
        if mime_type != "application/octet-stream":
            raise ValueError("file mime_type does not match filename")
        return
    if mime_type not in expected:
        raise ValueError("file mime_type does not match filename")
