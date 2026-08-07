from __future__ import annotations

import base64
import json
from collections.abc import Callable

import httpx
import pytest

from cf_agent_gateway.adapters.wechat import (
    WechatAPIError,
    WechatHttpMediaSender,
    WechatResponseError,
    WechatTimeoutError,
)

BASE_URL = "https://agent-wechat.test:6174/gateway"
TOKEN_ENV = "TEST_WECHAT_MEDIA_TOKEN"
TOKEN = "test-media-token-that-must-not-leak"
ACCOUNT_ID = "wxid_gateway"
CONVERSATION_ID = "wxid_alice"
PNG_DATA = b"\x89PNG\r\n\x1a\nminimal-test-data"
JPEG_DATA = b"\xff\xd8\xff\xe0minimal-test-data\xff\xd9"
GIF_DATA = b"GIF89aminimal-test-data"


def media_sender(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_media_bytes: int = 25 * 1024 * 1024,
) -> WechatHttpMediaSender:
    return WechatHttpMediaSender(
        account_id=ACCOUNT_ID,
        base_url=BASE_URL,
        token_env=TOKEN_ENV,
        max_media_bytes=max_media_bytes,
        environment_reader=lambda name: TOKEN if name == TOKEN_ENV else None,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("image_data", "mime_type"),
    [
        (PNG_DATA, "image/png"),
        (JPEG_DATA, "image/jpeg"),
        (GIF_DATA, "image/gif"),
    ],
)
def test_send_image_posts_bearer_authenticated_base64_payload(
    image_data: bytes,
    mime_type: str,
) -> None:
    encoded = base64.b64encode(image_data).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == f"{BASE_URL}/api/messages/send"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {
            "chatId": CONVERSATION_ID,
            "image": {"data": encoded, "mimeType": mime_type},
        }
        return httpx.Response(200, json={"success": True, "localId": 201})

    with media_sender(handler) as sender:
        result = sender.send_media(
            CONVERSATION_ID,
            "image",
            encoded,
            mime_type.upper(),
        )

    assert result == {"success": True, "localId": 201}


def test_send_file_posts_bearer_authenticated_base64_payload() -> None:
    file_data = b"quarterly report"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == f"{BASE_URL}/api/messages/send"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {
            "chatId": CONVERSATION_ID,
            "file": {
                "data": base64.b64encode(file_data).decode("ascii"),
                "filename": "quarterly report.txt",
            },
        }
        return httpx.Response(200, json={"success": True})

    with media_sender(handler) as sender:
        result = sender.send_media(
            CONVERSATION_ID,
            "file",
            file_data,
            "text/plain",
            "quarterly report.txt",
        )

    assert result == {"success": True}


@pytest.mark.parametrize(
    ("media_type", "data", "mime_type", "filename"),
    [
        ("image", PNG_DATA, "image/png", None),
        ("file", b"12345", "application/octet-stream", "oversized.bin"),
    ],
)
def test_send_media_rejects_oversized_data_before_http(
    media_type: str,
    data: bytes,
    mime_type: str,
    filename: str | None,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    with (
        media_sender(handler, max_media_bytes=4) as sender,
        pytest.raises(ValueError, match="size limit"),
    ):
        sender.send_media(CONVERSATION_ID, media_type, data, mime_type, filename)

    assert requests == []


def test_send_file_accepts_data_at_exact_size_limit() -> None:
    file_data = b"1234"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["file"]["data"] == base64.b64encode(file_data).decode(
            "ascii"
        )
        return httpx.Response(200, json={"success": True})

    with media_sender(handler, max_media_bytes=len(file_data)) as sender:
        result = sender.send_media(
            CONVERSATION_ID,
            "file",
            file_data,
            "application/octet-stream",
            "exact.bin",
        )

    assert result == {"success": True}


def test_send_file_rejects_oversized_base64_before_http() -> None:
    requests: list[httpx.Request] = []
    encoded = base64.b64encode(b"1234567").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    with (
        media_sender(handler, max_media_bytes=4) as sender,
        pytest.raises(ValueError, match="size limit"),
    ):
        sender.send_media(
            CONVERSATION_ID,
            "file",
            encoded,
            "application/octet-stream",
            "oversized.bin",
        )

    assert requests == []


@pytest.mark.parametrize(
    ("media_type", "data", "mime_type", "filename", "message"),
    [
        ("video", b"data", "video/mp4", "clip.mp4", "image or file"),
        ("image", "not-base64!", "image/png", None, "canonical Base64"),
        ("file", "Zh==", "text/plain", "notes.txt", "canonical Base64"),
        ("image", PNG_DATA, "image/webp", None, "image/png"),
        ("image", b"not a png", "image/png", None, "does not match"),
        ("file", b"data", "application/pdf", "notes.txt", "does not match"),
        ("file", b"data", "text/plain", "payload.exe", "does not match"),
        ("file", b"data", "application/octet-stream", "report.pdf", "does not match"),
        (
            "file",
            b"data",
            "text/plain; charset=utf-8",
            "notes.txt",
            "concrete MIME",
        ),
        ("file", b"data", "*/*", "notes.unknown", "concrete MIME"),
        ("file", b"data", " text/plain", "notes.txt", "concrete MIME"),
        ("file", b"data", "text/plain", "../notes.txt", "safe basename"),
        ("file", b"data", "text/plain", "C:notes.txt", "safe basename"),
        ("file", b"data", "text/plain", "CON.txt", "safe basename"),
        ("file", b"data", "application/octet-stream", "CONIN$", "safe basename"),
        ("file", b"data", "text/plain", "COM\u00b9.txt", "safe basename"),
        ("file", b"data", "text/plain", "bad?.txt", "safe basename"),
        ("file", b"data", "text/plain", "notes.txt.", "safe basename"),
        ("file", b"data", "text/plain", " notes.txt", "safe basename"),
        ("file", b"data", "text/plain", "a\u202eb.txt", "safe basename"),
    ],
)
def test_send_media_validates_parameters_before_http(
    media_type: str,
    data: bytes | str,
    mime_type: str,
    filename: str | None,
    message: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    with media_sender(handler) as sender, pytest.raises(ValueError, match=message):
        sender.send_media(CONVERSATION_ID, media_type, data, mime_type, filename)

    assert requests == []


@pytest.mark.parametrize(
    ("media_type", "data", "mime_type", "filename", "operation"),
    [
        ("image", PNG_DATA, "image/png", None, "send_image"),
        ("file", b"data", "application/octet-stream", "report.bin", "send_file"),
    ],
)
def test_send_media_rejects_sanitized_failure_response(
    media_type: str,
    data: bytes,
    mime_type: str,
    filename: str | None,
    operation: str,
) -> None:
    encoded = base64.b64encode(data).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": f"{TOKEN}:{encoded}"})

    with (
        media_sender(handler) as sender,
        pytest.raises(WechatResponseError) as caught,
    ):
        sender.send_media(CONVERSATION_ID, media_type, data, mime_type, filename)

    assert caught.value.operation == operation
    assert TOKEN not in str(caught.value)
    assert encoded not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_send_file_maps_http_error_without_exposing_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": TOKEN})

    with media_sender(handler) as sender, pytest.raises(WechatAPIError) as caught:
        sender.send_media(
            CONVERSATION_ID,
            "file",
            b"data",
            "application/octet-stream",
            "report.bin",
        )

    assert caught.value.operation == "send_file"
    assert caught.value.status_code == 503
    assert caught.value.category == "server"
    assert TOKEN not in str(caught.value)


def test_send_file_maps_invalid_json_to_sanitized_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"{TOKEN}:not-json")

    with media_sender(handler) as sender, pytest.raises(WechatResponseError) as caught:
        sender.send_media(
            CONVERSATION_ID,
            "file",
            b"data",
            "application/octet-stream",
            "report.bin",
        )

    assert caught.value.operation == "send_file"
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_send_image_maps_timeout_without_exposing_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(TOKEN, request=request)

    with media_sender(handler) as sender, pytest.raises(WechatTimeoutError) as caught:
        sender.send_media(CONVERSATION_ID, "image", PNG_DATA, "image/png")

    assert caught.value.operation == "send_image"
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_media_sender_inherits_existing_text_contract_unchanged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "chatId": CONVERSATION_ID,
            "text": "existing text behavior",
        }
        return httpx.Response(200, json={"success": True})

    with media_sender(handler) as sender:
        result = sender.send_text(CONVERSATION_ID, "existing text behavior")

    assert result == {"success": True}
