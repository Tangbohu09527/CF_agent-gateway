from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from cf_agent_gateway.adapters.wechat import (
    WechatAPIError,
    WechatHttpMessageSender,
    WechatResponseError,
    WechatTimeoutError,
)
from cf_agent_gateway.config import WechatSettings

BASE_URL = "https://agent-wechat.test:6174/gateway"
TOKEN_ENV = "TEST_WECHAT_OUTBOUND_TOKEN"
TOKEN = "test-outbound-token-that-must-not-leak"
ACCOUNT_ID = "wxid_gateway"
CONVERSATION_ID = "wxid_alice"


def outbound_sender(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    environment_reader: Callable[[str], str | None] | None = None,
) -> WechatHttpMessageSender:
    settings = WechatSettings(base_url=BASE_URL, token_env=TOKEN_ENV)
    return WechatHttpMessageSender(
        account_id=ACCOUNT_ID,
        base_url=settings.base_url,
        token_env=settings.token_env,
        environment_reader=environment_reader
        or (lambda name: TOKEN if name == TOKEN_ENV else None),
        transport=httpx.MockTransport(handler),
    )


def test_send_text_posts_bearer_authenticated_text_payload() -> None:
    environment_reads: list[str] = []

    def environment_reader(name: str) -> str | None:
        environment_reads.append(name)
        return TOKEN if name == TOKEN_ENV else None

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == f"{BASE_URL}/api/messages/send"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {
            "chatId": CONVERSATION_ID,
            "text": "hello from Hermes",
        }
        return httpx.Response(200, json={"success": True, "localId": 102})

    with outbound_sender(handler, environment_reader=environment_reader) as sender:
        assert sender.account_id == ACCOUNT_ID
        result = sender.send_text(CONVERSATION_ID, "hello from Hermes")

    assert environment_reads == [TOKEN_ENV]
    assert result == {"success": True, "localId": 102}


def test_send_text_maps_401_to_sanitized_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": TOKEN})

    with outbound_sender(handler) as sender, pytest.raises(WechatAPIError) as caught:
        sender.send_text(CONVERSATION_ID, "hello")

    assert caught.value.operation == "send_text"
    assert caught.value.status_code == 401
    assert caught.value.category == "authentication"
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


def test_send_text_maps_timeout_to_sanitized_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(TOKEN, request=request)

    with outbound_sender(handler) as sender, pytest.raises(WechatTimeoutError) as caught:
        sender.send_text(CONVERSATION_ID, "hello")

    assert caught.value.operation == "send_text"
    assert caught.value.code == "wechat_timeout_error"
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_send_text_maps_agent_wechat_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": TOKEN})

    with outbound_sender(handler) as sender, pytest.raises(WechatAPIError) as caught:
        sender.send_text(CONVERSATION_ID, "hello")

    assert caught.value.operation == "send_text"
    assert caught.value.status_code == 503
    assert caught.value.category == "server"
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error": TOKEN},
        {"success": None, "error": TOKEN},
        {"success": 0, "error": TOKEN},
        {"error": TOKEN},
    ],
)
def test_send_text_rejects_agent_wechat_failure_payload(payload: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with outbound_sender(handler) as sender, pytest.raises(WechatResponseError) as caught:
        sender.send_text(CONVERSATION_ID, "hello")

    assert caught.value.operation == "send_text"
    assert caught.value.code == "wechat_response_error"
    assert TOKEN not in str(caught.value)


def test_send_text_sanitizes_invalid_json_error_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=TOKEN)

    with outbound_sender(handler) as sender, pytest.raises(WechatResponseError) as caught:
        sender.send_text(CONVERSATION_ID, "hello")

    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_sender_fails_before_http_when_configured_token_environment_is_missing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    with pytest.raises(ValueError) as caught:
        outbound_sender(handler, environment_reader=lambda name: None)

    assert TOKEN_ENV in str(caught.value)
    assert requests == []
