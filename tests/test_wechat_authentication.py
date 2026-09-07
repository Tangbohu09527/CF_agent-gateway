from __future__ import annotations

import json

import httpx
import pytest

from cf_agent_gateway.adapters.wechat.client import AgentWechatClient
from cf_agent_gateway.adapters.wechat.diagnose import inspect_authentication
from cf_agent_gateway.adapters.wechat.errors import (
    WechatAccountMismatchError,
    WechatPreSendAuthError,
    WechatResponseError,
    WechatTimeoutError,
)
from cf_agent_gateway.adapters.wechat.outbound_http import WechatHttpMessageSender
from cf_agent_gateway.config import Settings, WechatSettings
from cf_agent_gateway.delivery.worker import DeliveryFailureKind, classify_delivery_error

TOKEN = "private-auth-test-token"
ACCOUNT = "wxid_current_bot"
BASE_URL = "http://wechat.test:6174"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "logged_in", "loggedInUser": value}
        for value in (
            "",
            " ",
            " wxid_bot",
            "wxid_bot\n",
            "wxid\x00bot",
            "wxid\u200bbot",
            "x" * 256,
            12,
            True,
            [],
            {},
        )
    ]
    + [
        {"status": value, "loggedInUser": "wxid_bot"}
        for value in (None, False, [], "", "logged_in\n")
    ]
    + [
        {"status": "logged_in", "loggedInUser": "wxid_bot", "success": False},
        {"status": "logged_in", "loggedInUser": "wxid_bot", "error": "private-body"},
        {"data": {"status": "logged_in", "loggedInUser": "wxid_bot"}, "success": False},
    ],
)
def test_auth_state_and_account_are_strict_and_error_payload_never_authenticates(payload) -> None:
    with (
        AgentWechatClient(
            BASE_URL,
            TOKEN,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        ) as client,
        pytest.raises(WechatResponseError) as caught,
    ):
        client.get_auth_status()
    assert caught.value.operation == "get_auth_status"
    assert "wxid_bot" not in str(caught.value) and "private-body" not in str(caught.value)


@pytest.mark.parametrize(
    "payload,http_status,expected",
    [
        ({"status": "logged_in", "loggedInUser": ACCOUNT}, 200, "authenticated"),
        ({"status": "logged_in", "loggedInUser": ACCOUNT, "error": None}, 200, "authenticated"),
        (
            {"data": {"status": "logged_in", "loggedInUser": ACCOUNT, "error": ""}, "error": None},
            200,
            "authenticated",
        ),
        ({"data": {"status": "logged_in", "loggedInUser": ACCOUNT}}, 200, "authenticated"),
        ({"status": "logged_out", "loggedInUser": ACCOUNT}, 200, "not_logged_in"),
        ({"status": "qr_pending"}, 200, "not_logged_in"),
        ({"status": "logged_in"}, 200, "invalid_account"),
        ({"status": "logged_in", "loggedInUser": "broken\n"}, 200, "invalid_account"),
        ({"error": TOKEN}, 401, "token_error"),
        ({"error": TOKEN}, 503, "service_unavailable"),
    ],
)
def test_explicit_auth_diagnostic_uses_only_real_auth_api_and_returns_safe_fields(
    payload, http_status, expected
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/api/status/auth"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["X-Session-Id"] == "default"
        return httpx.Response(http_status, json=payload)

    settings = Settings(wechat=WechatSettings(enabled=True, base_url=BASE_URL))
    result = inspect_authentication(
        settings,
        environment_reader=lambda name: TOKEN if name == settings.wechat.token_env else None,
        client_factory=lambda base_url, token: AgentWechatClient(
            base_url, token, transport=httpx.MockTransport(handler)
        ),
    )
    assert result["status"] == expected and len(requests) == 1
    assert result["authenticated"] is (expected == "authenticated")
    assert result.get("account_id") == (ACCOUNT if expected == "authenticated" else None)
    assert TOKEN not in json.dumps(result) and "broken" not in json.dumps(result)


def test_missing_token_fails_before_diagnostic_network_access() -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("no network/client should be initialized without a real Token")

    result = inspect_authentication(
        Settings(wechat=WechatSettings(enabled=True)),
        environment_reader=lambda name: None,
        client_factory=forbidden,
    )
    assert result["status"] == "token_error" and result["authenticated"] is False


@pytest.mark.parametrize(
    "auth_body,expected_error",
    [
        ({"status": "logged_out"}, "wechat_presend_auth_unavailable"),
        ({"status": "logged_in"}, "wechat_presend_auth_unavailable"),
        (
            {"status": "logged_in", "loggedInUser": "bad\naccount"},
            "wechat_presend_auth_unavailable",
        ),
        (
            {"status": "logged_in", "loggedInUser": "wxid_other_bot"},
            "wechat_presend_account_mismatch",
        ),
    ],
)
def test_actual_auth_guard_blocks_send_for_missing_invalid_or_changed_account(
    auth_body, expected_error
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/api/status/auth"
        assert request.headers["X-Session-Id"] == "default"
        return httpx.Response(200, json=auth_body)

    with (
        WechatHttpMessageSender(
            ACCOUNT,
            BASE_URL,
            "TOKEN",
            environment_reader=lambda name: TOKEN,
            transport=httpx.MockTransport(handler),
        ) as sender,
        pytest.raises(WechatPreSendAuthError) as caught,
    ):
        sender.send_text("wxid_sender", "must not be sent")
    assert caught.value.code == expected_error and len(requests) == 1
    assert TOKEN not in str(caught.value) and "wxid_other_bot" not in str(caught.value)


def test_same_sender_checks_every_part_again_after_account_switch() -> None:
    current = ACCOUNT
    sends = []
    reads = []

    def handler(request):
        assert request.headers["X-Session-Id"] == "default"
        if request.method == "GET":
            reads.append(current)
            return httpx.Response(200, json={"status": "logged_in", "loggedInUser": current})
        sends.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    with WechatHttpMessageSender(
        ACCOUNT,
        BASE_URL,
        "TOKEN",
        environment_reader=lambda name: TOKEN,
        transport=httpx.MockTransport(handler),
    ) as sender:
        sender.send_text("wxid_sender", "approved previous reply")
        current = "wxid_other_bot"
        with pytest.raises(WechatAccountMismatchError):
            sender.send_text("wxid_sender", "must not cross accounts")
    assert reads == [ACCOUNT, "wxid_other_bot"]
    assert sends == [{"chatId": "wxid_sender", "text": "approved previous reply"}]


def test_auth_timeout_is_known_presend_but_post_timeout_still_uncertain() -> None:
    def unavailable(request):
        assert request.method == "GET"
        raise httpx.ReadTimeout(TOKEN, request=request)

    with (
        WechatHttpMessageSender(
            ACCOUNT,
            BASE_URL,
            "TOKEN",
            environment_reader=lambda name: TOKEN,
            transport=httpx.MockTransport(unavailable),
        ) as sender,
        pytest.raises(WechatPreSendAuthError) as caught,
    ):
        sender.send_text("wxid_sender", "must not be sent")
    assert classify_delivery_error(caught.value) is DeliveryFailureKind.RETRYABLE
    assert classify_delivery_error(WechatAccountMismatchError()) is DeliveryFailureKind.PERMANENT
    assert (
        classify_delivery_error(WechatTimeoutError(operation="send_text"))
        is DeliveryFailureKind.UNCERTAIN
    )
    assert caught.value.__cause__ is None and caught.value.__context__ is None
