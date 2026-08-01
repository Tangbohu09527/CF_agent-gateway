from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any, Self
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from cf_agent_gateway.adapters.wechat.errors import (
    WechatAPIError,
    WechatResponseError,
    WechatTimeoutError,
    WechatTransportError,
)
from cf_agent_gateway.adapters.wechat.raw_models import (
    AgentWechatAuthStatus,
    AgentWechatMedia,
    RawWechatMessage,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)


class AgentWechatClient:
    """Synchronous client for the local agent-wechat HTTP API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_base_url = _required_string(base_url, "base_url").rstrip("/") + "/"
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_auth_status(self) -> AgentWechatAuthStatus:
        operation = "get_auth_status"
        response = self._request("GET", "api/status/auth", operation=operation)
        payload = self._json(response, operation=operation)
        candidate = payload
        if isinstance(payload, Mapping) and "status" not in payload:
            candidate = payload.get("data")
        if not isinstance(candidate, Mapping):
            raise WechatResponseError(operation=operation)
        try:
            return AgentWechatAuthStatus.model_validate(candidate)
        except ValidationError:
            raise WechatResponseError(operation=operation) from None

    def list_chats(self) -> list[dict[str, Any]]:
        operation = "list_chats"
        response = self._request("GET", "api/chats", operation=operation)
        payload = self._json(response, operation=operation)
        items = self._list_payload(payload, key="chats", operation=operation)
        if not all(isinstance(item, Mapping) for item in items):
            raise WechatResponseError(operation=operation)
        return [dict(item) for item in items]

    def list_messages(self, chat_id: str) -> list[RawWechatMessage]:
        operation = "list_messages"
        chat = _path_segment(chat_id, "chat_id")
        response = self._request("GET", f"api/messages/{chat}", operation=operation)
        payload = self._json(response, operation=operation)
        items = self._list_payload(payload, key="messages", operation=operation)
        try:
            return [RawWechatMessage.model_validate(item) for item in items]
        except (TypeError, ValidationError):
            raise WechatResponseError(operation=operation) from None

    def get_media(self, chat_id: str, local_id: str | int) -> AgentWechatMedia:
        operation = "get_media"
        chat = _path_segment(chat_id, "chat_id")
        local = _path_segment(local_id, "local_id")
        response = self._request("GET", f"api/messages/{chat}/media/{local}", operation=operation)
        payload = self._json(response, operation=operation)
        if not isinstance(payload, Mapping):
            raise WechatResponseError(operation=operation)

        media_type = payload.get("type")
        if not isinstance(media_type, str) or not media_type.strip():
            raise WechatResponseError(operation=operation)
        media_format = _optional_response_string(payload, "format", operation=operation)
        filename = _optional_response_string(payload, "filename", operation=operation)
        if media_type == "unsupported":
            return AgentWechatMedia(
                media_type=media_type,
                data=None,
                format=media_format,
                filename=filename,
                supported=False,
            )

        encoded_data = payload.get("data")
        if "data" not in payload or not isinstance(encoded_data, str):
            raise WechatResponseError(operation=operation)
        try:
            data = base64.b64decode(encoded_data, validate=True)
        except (binascii.Error, ValueError):
            raise WechatResponseError(operation=operation) from None
        return AgentWechatMedia(
            media_type=media_type,
            data=data,
            format=media_format,
            filename=filename,
            supported=True,
        )

    def send_text(self, chat_id: str, content: str) -> dict[str, Any] | None:
        operation = "send_text"
        chat = _required_string(chat_id, "chat_id")
        if not isinstance(content, str) or content == "":
            raise ValueError("content must not be empty")
        response = self._request(
            "POST",
            "api/messages/send",
            operation=operation,
            json={"chatId": chat, "text": content},
        )
        if not response.content:
            return None
        payload = self._json(response, operation=operation)
        if not isinstance(payload, Mapping):
            raise WechatResponseError(operation=operation)
        return dict(payload)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        operation: str,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, endpoint, json=json)
        except httpx.TimeoutException:
            raise WechatTimeoutError(operation=operation) from None
        except httpx.RequestError:
            raise WechatTransportError(operation=operation) from None
        if not 200 <= response.status_code < 300:
            raise WechatAPIError(operation=operation, status_code=response.status_code)
        return response

    @staticmethod
    def _json(response: httpx.Response, *, operation: str) -> Any:
        try:
            return response.json()
        except ValueError:
            raise WechatResponseError(operation=operation) from None

    @staticmethod
    def _list_payload(payload: Any, *, key: str, operation: str) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, Mapping):
            raise WechatResponseError(operation=operation)
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return candidate
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, Mapping) and isinstance(data.get(key), list):
            return data[key]
        raise WechatResponseError(operation=operation)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _bearer_token(value: object) -> str:
    token = _required_string(value, "token")
    if any(not 0x21 <= ord(character) <= 0x7E for character in token):
        raise ValueError("token must contain only visible ASCII characters without whitespace")
    return token


def _optional_response_string(
    payload: Mapping[str, Any], key: str, *, operation: str
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WechatResponseError(operation=operation)
    return value


def _path_segment(value: str | int, field_name: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be empty")
    normalized = _required_string(str(value), field_name)
    return quote(normalized, safe="")
