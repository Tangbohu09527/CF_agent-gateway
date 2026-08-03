from __future__ import annotations

from typing import Any, Self
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesAPIKeyError,
    HermesResponseError,
    HermesTimeoutError,
    HermesTransportError,
)
from cf_agent_gateway.hermes.models import (
    HermesChatCompletionRequest,
    HermesChatCompletionResponse,
    HermesUserMessage,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=15.0, pool=5.0)


class HermesClient:
    """Synchronous client for the Hermes OpenAI-compatible chat API."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_base_url = _base_url(base_url)
        normalized_api_key = _api_key(api_key)
        self._model = _required_string(model, "model")
        resolved_timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._client = httpx.Client(
            base_url=normalized_base_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {normalized_api_key}",
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

    def chat(self, content: str) -> str:
        """Send one user message and return the first assistant message content."""

        if not isinstance(content, str) or not content:
            raise ValueError("content must not be empty")

        operation = "chat_completion"
        request = HermesChatCompletionRequest(
            model=self._model,
            messages=[HermesUserMessage(content=content)],
        )
        response = self._request(
            "POST",
            "v1/chat/completions",
            operation=operation,
            json=request.model_dump(mode="json"),
        )
        try:
            payload = response.json()
            completion = HermesChatCompletionResponse.model_validate(payload)
        except (ValueError, ValidationError):
            raise HermesResponseError(operation=operation) from None
        return completion.choices[0].message.content

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        operation: str,
        json: dict[str, Any],
    ) -> httpx.Response:
        try:
            response = self._client.request(method, endpoint, json=json)
        except httpx.TimeoutException:
            raise HermesTimeoutError(operation=operation) from None
        except httpx.RequestError:
            raise HermesTransportError(operation=operation) from None
        if not 200 <= response.status_code < 300:
            raise HermesAPIError(operation=operation, status_code=response.status_code)
        return response


def _base_url(value: object) -> str:
    base_url = _required_string(value, "base_url").rstrip("/") + "/"
    if any(character.isspace() or ord(character) < 0x20 for character in base_url):
        raise ValueError("base_url must be an HTTP or HTTPS URL")
    try:
        parsed_url = urlsplit(base_url)
        port = parsed_url.port
    except ValueError:
        raise ValueError("base_url must be an HTTP or HTTPS URL") from None
    if (
        parsed_url.scheme.lower() not in {"http", "https"}
        or parsed_url.hostname is None
        or port == 0
        or parsed_url.username is not None
        or parsed_url.password is not None
        or bool(parsed_url.query)
        or bool(parsed_url.fragment)
    ):
        raise ValueError("base_url must be an HTTP or HTTPS URL")
    return base_url


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _api_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HermesAPIKeyError()
    api_key = value.strip()
    if any(not 0x21 <= ord(character) <= 0x7E for character in api_key):
        raise HermesAPIKeyError()
    return api_key
