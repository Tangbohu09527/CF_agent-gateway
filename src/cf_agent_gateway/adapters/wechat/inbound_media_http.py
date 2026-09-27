"""Bounded, account/message-bound HTTP intake for an admitted stored attachment.

No model calls, file publication or retry loop. The caller must apply admission
before constructing MediaSource and persist pending/backoff decisions separately.
Base URL and token are operator configuration, never message/model input.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from cf_agent_gateway.adapters.wechat.inbound_media import (
    MAX_MEDIA_BYTES,
    ExpectedOriginal,
    InboundMediaError,
    InboundMediaResult,
    MediaReadiness,
    parse_inbound_media,
)

_METADATA_LIMIT = 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9_.@-]{1,255}\Z")


class MediaFetchError(ValueError):
    """Codes are static; do not propagate upstream exception text/body/URL."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _id(value: object) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise MediaFetchError("invalid_media_source")
    return value


def _time(value: object) -> datetime:
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        raise MediaFetchError("invalid_media_source_time") from None


@dataclass(frozen=True, slots=True, repr=False)
class MediaSource:
    """Snapshot from a persisted, admitted inbound message, NOT a model request."""

    account_id: str
    chat_id: str
    sender_id: str
    local_id: str
    server_id: str | None
    raw_type: int
    occurred_at: datetime
    content_sha256: str

    def __post_init__(self) -> None:
        for name in ("account_id", "chat_id", "sender_id", "local_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _id(value) != value:
                raise MediaFetchError("invalid_media_source")
        if self.server_id is not None and (
            not isinstance(self.server_id, str) or _id(self.server_id) != self.server_id
        ):
            raise MediaFetchError("invalid_media_source")
        if (
            type(self.raw_type) is not int
            or self.raw_type not in {3, 49}
            or not self.local_id.isascii()
            or not self.local_id.isdecimal()
            or not 1 <= int(self.local_id) <= 2**63 - 1
            or self.local_id != str(int(self.local_id))
            or not isinstance(self.content_sha256, str)
            or _DIGEST.fullmatch(self.content_sha256) is None
        ):
            raise MediaFetchError("invalid_media_source")
        _time(self.occurred_at)

    @property
    def fingerprint(self) -> str:
        values = [
            "wechat-media-source/v1",
            self.account_id,
            self.chat_id,
            self.sender_id,
            self.local_id,
            self.server_id,
            self.raw_type,
            _time(self.occurred_at).isoformat(),
            self.content_sha256,
        ]
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()

    def __repr__(self) -> str:
        return f"MediaSource(fingerprint={self.fingerprint!r})"


@dataclass(frozen=True, slots=True)
class BoundMediaResult:
    source_fingerprint: str
    media: InboundMediaResult = field(repr=False)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MediaFetchError("duplicate_media_json_key")
        result[key] = value
    return result


def _nonfinite(_value: str) -> object:
    raise MediaFetchError("invalid_media_json")


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or "\\" in value
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MediaFetchError("invalid_media_endpoint") from None
    return value.rstrip("/") + "/"


class InboundMediaHTTPClient:
    """One bounded fetch attempt. Transient errors are classified, never retried here.

    Empty image body is legitimate: identity is bound by account/chat/message,
    sender/type/time and a content hash (including the hash of an empty string).
    Reads may cause upstream cache/download side effects; they are not DB writes
    by this client and are not proof that a native download was queued.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        max_bytes: int = MAX_MEDIA_BYTES,
        deadline_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _base_url(base_url)
        if (
            not isinstance(token, str)
            or not token
            or any(not 0x21 <= ord(c) <= 0x7E for c in token)
        ):
            raise MediaFetchError("invalid_media_credential")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_MEDIA_BYTES:
            raise MediaFetchError("invalid_media_limit")
        if (
            isinstance(deadline_seconds, bool)
            or not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= 120
        ):
            raise MediaFetchError("invalid_media_deadline")
        self._token = token
        self._limit = max_bytes
        self._deadline = deadline_seconds
        self._transport = transport

    def fetch(
        self, source: MediaSource, *, expected: ExpectedOriginal | None = None
    ) -> BoundMediaResult:
        """Synchronous worker entry; async runtimes should use fetch_async."""
        return asyncio.run(self.fetch_async(source, expected=expected))

    async def fetch_async(
        self,
        source: MediaSource,
        *,
        expected: ExpectedOriginal | None = None,
    ) -> BoundMediaResult:
        if not isinstance(source, MediaSource):
            raise MediaFetchError("invalid_media_source")
        if expected is not None and not isinstance(expected, ExpectedOriginal):
            raise MediaFetchError("invalid_original_expectation")
        try:
            async with asyncio.timeout(self._deadline):
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "X-Session-Id": "default",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    timeout=httpx.Timeout(connect=5, read=15, write=5, pool=5),
                    trust_env=False,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    await self._binding(client, source)
                    payload = await self._json(
                        client,
                        f"api/messages/{quote(source.chat_id, safe='')}/media/{source.local_id}",
                        4 * ((self._limit + 2) // 3) + 65536,
                    )
                    media = parse_inbound_media(payload, max_bytes=self._limit, expected=expected)
                    if media.readiness is MediaReadiness.READY:
                        expected_kind = "image" if source.raw_type == 3 else "file"
                        if media.media_type != expected_kind:
                            raise MediaFetchError("media_type_message_mismatch")
                    # Guard against account switch/local-id reuse during a slow GET.
                    # This does not prove the internals of the native cache, only
                    # the observable account/message bindings around this request.
                    await self._binding(client, source)
                    return BoundMediaResult(source.fingerprint, media)
        except MediaFetchError:
            raise
        except InboundMediaError as error:
            raise MediaFetchError(error.code) from None
        except (TimeoutError, httpx.TimeoutException):
            raise MediaFetchError("media_fetch_timeout", retryable=True) from None
        except httpx.RequestError:
            raise MediaFetchError("media_transport_failed", retryable=True) from None

    async def _binding(self, client: httpx.AsyncClient, source: MediaSource) -> None:
        auth = await self._json(client, "api/status/auth", 65536)
        if isinstance(auth, dict) and "status" not in auth:
            auth = auth.get("data")
        if not isinstance(auth, dict) or auth.get("status") != "logged_in":
            raise MediaFetchError("media_account_not_authenticated")
        if auth.get("loggedInUser") != source.account_id:
            raise MediaFetchError("media_account_changed")
        rows = await self._json(
            client,
            f"api/messages/{quote(source.chat_id, safe='')}",
            _METADATA_LIMIT,
        )
        if isinstance(rows, dict):
            rows = rows.get("messages", rows.get("data"))
            if isinstance(rows, dict):
                rows = rows.get("messages")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise MediaFetchError("invalid_media_message_list")
        selected = [
            item
            for item in rows
            if isinstance(item, dict)
            and type(item.get("localId")) in (str, int)
            and str(item["localId"]) == source.local_id
        ]
        if len(selected) != 1:
            raise MediaFetchError("media_source_not_unique_or_not_visible")
        item = selected[0]
        try:
            content = item.get("content")
            matches = (
                item.get("chatId") == source.chat_id
                and item.get("sender") == source.sender_id
                and type(item.get("type")) is int
                and item["type"] == source.raw_type
                and item.get("isSelf") is not True
                and _time(item.get("timestamp")) == _time(source.occurred_at)
                and isinstance(content, str)
                and hashlib.sha256(content.encode("utf-8")).hexdigest() == source.content_sha256
                and (
                    (source.server_id is None and item.get("serverId") in (None, "", 0, "0"))
                    or (
                        source.server_id is not None
                        and type(item.get("serverId")) in (str, int)
                        and str(item["serverId"]) == source.server_id
                    )
                )
            )
        except (UnicodeError, MediaFetchError):
            matches = False
        if not matches:
            raise MediaFetchError("media_source_binding_changed")

    @staticmethod
    async def _json(client: httpx.AsyncClient, path: str, limit: int) -> object:
        async with client.stream("GET", path) as response:
            if response.status_code != 200:
                transient = response.status_code in {408, 425, 429} or response.status_code >= 500
                raise MediaFetchError(
                    f"media_http_{response.status_code}",
                    retryable=transient,
                )
            if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise MediaFetchError("media_content_encoding_not_allowed")
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise MediaFetchError("media_json_content_type_required")
            lengths = response.headers.get_list("content-length")
            declared = None
            if lengths:
                if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
                    raise MediaFetchError("invalid_media_content_length")
                declared = int(lengths[0])
                if declared > limit:
                    raise MediaFetchError("media_http_body_too_large")
            body = bytearray()
            async for chunk in response.aiter_raw(chunk_size=65536):
                if len(body) + len(chunk) > limit:
                    raise MediaFetchError("media_http_body_too_large")
                body.extend(chunk)
            if declared is not None and declared != len(body):
                raise MediaFetchError("media_http_body_incomplete", retryable=True)
        try:
            result = json.loads(
                body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_nonfinite
            )
        except (ValueError, UnicodeError, RecursionError):
            raise MediaFetchError("invalid_media_json") from None
        if isinstance(result, dict) and (
            result.get("success") is False or result.get("error") not in (None, "")
        ):
            raise MediaFetchError("upstream_media_error")
        return result
