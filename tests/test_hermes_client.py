from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from cf_agent_gateway.hermes import (
    HermesAPIError,
    HermesAPIKeyError,
    HermesClient,
    HermesResponseError,
    HermesTimeoutError,
)

BASE_URL = "https://hermes.test"
API_KEY = "test-hermes-api-key"
MODEL = "hermes-agent"
USER_CONTENT = "Hello, Hermes"


def hermes_client(handler: Callable[[httpx.Request], httpx.Response]) -> HermesClient:
    return HermesClient(
        BASE_URL,
        API_KEY,
        MODEL,
        transport=httpx.MockTransport(handler),
    )


def test_chat_posts_expected_request_and_returns_assistant_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        assert request.headers["accept"] == "application/json"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "model": MODEL,
            "messages": [{"role": "user", "content": USER_CONTENT}],
        }
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello from Hermes",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
        )

    with hermes_client(handler) as client:
        result = client.chat(USER_CONTENT)

    assert result == "Hello from Hermes"


def test_http_error_has_stable_sanitized_metadata() -> None:
    sensitive_body = "upstream-sensitive-error-body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": sensitive_body})

    with hermes_client(handler) as client, pytest.raises(HermesAPIError) as caught:
        client.chat(USER_CONTENT)

    assert caught.value.code == "hermes_api_error"
    assert caught.value.operation == "chat_completion"
    assert caught.value.status_code == 503
    assert caught.value.category == "server"
    assert sensitive_body not in str(caught.value)
    assert API_KEY not in str(caught.value)
    assert USER_CONTENT not in str(caught.value)


@pytest.mark.parametrize("api_key", [None, "", "   ", "key with space", "key\nvalue"])
def test_missing_or_invalid_api_key_fails_before_http(api_key: object) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal requests
        requests += 1
        raise AssertionError("HTTP must not be called")

    with pytest.raises(HermesAPIKeyError) as caught:
        HermesClient(
            BASE_URL,
            api_key,  # type: ignore[arg-type]
            MODEL,
            transport=httpx.MockTransport(handler),
        )

    assert caught.value.code == "hermes_api_key_error"
    assert requests == 0


def test_timeout_has_stable_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            f"{API_KEY}:{USER_CONTENT}",
            request=request,
        )

    with hermes_client(handler) as client, pytest.raises(HermesTimeoutError) as caught:
        client.chat(USER_CONTENT)

    assert caught.value.code == "hermes_timeout_error"
    assert caught.value.operation == "chat_completion"
    assert API_KEY not in str(caught.value)
    assert USER_CONTENT not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": {"role": "user", "content": "wrong role"}}]},
        {"choices": [{"message": {"role": "assistant"}}]},
    ],
)
def test_invalid_success_response_raises_response_error(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with hermes_client(handler) as client, pytest.raises(HermesResponseError) as caught:
        client.chat(USER_CONTENT)

    assert caught.value.code == "hermes_response_error"
    assert caught.value.operation == "chat_completion"


def test_non_json_success_response_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with hermes_client(handler) as client, pytest.raises(HermesResponseError):
        client.chat(USER_CONTENT)
