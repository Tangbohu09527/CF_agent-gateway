from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any, Self

import httpx

from cf_agent_gateway.adapters.wechat.errors import (
    WechatAPIError,
    WechatResponseError,
    WechatTimeoutError,
    WechatTransportError,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)


class WechatHttpMessageSender:
    """Synchronous outbound text sender scoped to one agent-wechat account."""

    def __init__(
        self,
        account_id: str,
        base_url: str,
        token_env: str,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        environment_reader: Callable[[str], str | None] = os.getenv,
    ) -> None:
        self._account_id = _required_string(account_id, "account_id")
        normalized_base_url = _required_string(base_url, "base_url").rstrip("/") + "/"
        normalized_token_env = _required_string(token_env, "token_env")
        token = environment_reader(normalized_token_env)
        if token is None or not token.strip():
            raise ValueError(f"missing WeChat token environment variable: {normalized_token_env}")
        normalized_token = _bearer_token(token)
        resolved_timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._client = httpx.Client(
            base_url=normalized_base_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {normalized_token}",
            },
            timeout=resolved_timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    @property
    def account_id(self) -> str:
        return self._account_id

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def send_text(self, conversation_id: str, content: str) -> dict[str, Any] | None:
        operation = "send_text"
        chat_id = _required_string(conversation_id, "conversation_id")
        if not isinstance(content, str) or content == "":
            raise ValueError("content must not be empty")

        response: httpx.Response | None = None
        failure: str | None = None
        try:
            response = self._client.post(
                "api/messages/send",
                json={"chatId": chat_id, "content": content},
            )
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

        payload: Any = None
        invalid_json = False
        try:
            payload = response.json()
        except ValueError:
            invalid_json = True
        if invalid_json or not isinstance(payload, Mapping) or payload.get("success") is not True:
            raise WechatResponseError(operation=operation)
        return dict(payload)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _bearer_token(value: object) -> str:
    token = _required_string(value, "token")
    if any(not 0x21 <= ord(character) <= 0x7E for character in token):
        raise ValueError("token must contain only visible ASCII characters without whitespace")
    return token
