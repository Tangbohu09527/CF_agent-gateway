from __future__ import annotations

import asyncio
import hashlib
import json as json_module
from collections.abc import Callable
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx
from pydantic import ValidationError

from cf_agent_gateway.artifact.return_config import (
    RETURN_ACK_HEADER,
    RETURN_AUTH_HEADER,
    RETURN_URL_HEADER,
)
from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesAPIKeyError,
    HermesExecutionTimeoutError,
    HermesResponseError,
    HermesTimeoutError,
    HermesTransportError,
)
from cf_agent_gateway.hermes.models import (
    HermesChatCompletionRequest,
    HermesChatCompletionResponse,
    HermesChatResult,
    HermesUserMessage,
    ResponseEnvelope,
)
from cf_agent_gateway.hermes.tls import verified_ssl_context
from cf_agent_gateway.hermes_timeouts import HermesTimeoutSettings

DEFAULT_TIMEOUTS = HermesTimeoutSettings()
DEFAULT_TIMEOUT = DEFAULT_TIMEOUTS.httpx_timeout()
HERMES_SESSION_HEADER = "X-Hermes-Session-Id"
HERMES_IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_IDEMPOTENCY_KEY_LENGTH = 255
MAX_HERMES_THREAD_ID_LENGTH = 255


class HermesClient:
    """Synchronous worker interface with cancellable, per-call async HTTP I/O.

    Each worker slot owns its event loop and connection for the duration of a
    call. Cancellation unwinds HTTPX before the synchronous call returns.
    Closing our socket does not cancel Hermes execution.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        timeout: httpx.Timeout | float | None = None,
        timeouts: HermesTimeoutSettings = DEFAULT_TIMEOUTS,
        ca_file: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_base_url = _base_url(base_url)
        normalized_api_key = _api_key(api_key)
        self._model = _required_string(model, "model")
        if not isinstance(timeouts, HermesTimeoutSettings):
            raise ValueError("timeouts must be HermesTimeoutSettings")
        if timeout is not None:
            # Compatibility for bounded diagnostic callers, never unbounded I/O.
            value = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
            timeouts = HermesTimeoutSettings(
                connect_seconds=value.connect,
                read_seconds=value.read,
                write_seconds=value.write,
                pool_seconds=value.pool,
                execution_seconds=timeouts.execution_seconds,
            )
        self._base_url = normalized_base_url
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {normalized_api_key}",
        }
        self._timeouts = timeouts
        self._ssl_context = verified_ssl_context(ca_file)
        self._transport = transport
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        # Runtime drains active slots first; sockets are owned by each call.
        self._closed = True

    def chat(
        self,
        content: str,
        *,
        hermes_thread_id: str | None = None,
        profile_reference: str | None = None,
        profile_revision: int | None = None,
        thread_id: str | None = None,
        session_metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        runtime_model: str | None = None,
        runtime_provider: str | None = None,
        runtime_model_options: dict[str, object] | None = None,
        artifact_return_context: dict[str, str] | None = None,
    ) -> HermesChatResult:
        """Send one user message, creating or continuing a Hermes thread."""

        if not isinstance(content, str) or not content:
            raise ValueError("content must not be empty")
        if hermes_thread_id is not None:
            hermes_thread_id = _hermes_thread_id(hermes_thread_id)

        if idempotency_key is not None:
            idempotency_key = _idempotency_key(idempotency_key)

        operation = "chat_completion"
        request = HermesChatCompletionRequest(
            model=runtime_model or self._model,
            messages=[HermesUserMessage(content=content)],
            profile_reference=profile_reference,
            profile_revision=profile_revision,
            thread_id=thread_id,
            session_metadata=session_metadata,
        )
        request_headers = {}
        if hermes_thread_id is not None:
            request_headers[HERMES_SESSION_HEADER] = hermes_thread_id
        if idempotency_key is not None:
            request_headers[HERMES_IDEMPOTENCY_HEADER] = idempotency_key
        if artifact_return_context is not None:
            url = urlsplit(self._base_url)
            if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("artifact return requires verified TLS to the Hermes host")
            if set(artifact_return_context) != {"url", "authorization"}:
                raise ValueError("invalid artifact return context")
            # Trusted request middleware consumes these headers. They never enter
            # messages, session_metadata, model tool arguments, or persisted history.
            request_headers[RETURN_URL_HEADER] = artifact_return_context["url"]
            request_headers[RETURN_AUTH_HEADER] = artifact_return_context["authorization"]
        payload = request.model_dump(mode="json", exclude_none=True)
        if runtime_model is not None:
            # The pinned OpenAI route does not read a session's persisted /model
            # lock. An explicit provider makes its per-request selection apply.
            if not runtime_provider:
                raise ValueError("runtime_provider is required with runtime_model")
            payload["provider"] = runtime_provider
            payload["model_options"] = runtime_model_options or {}
        response = self._request(
            "POST",
            "v1/chat/completions",
            operation=operation,
            json=payload,
            headers=request_headers or None,
        )
        if artifact_return_context is not None:
            expected_ack = hashlib.sha256(
                artifact_return_context["authorization"].encode("utf-8")
            ).hexdigest()
            if response.headers.get(RETURN_ACK_HEADER) != expected_ack:
                # An ordinary official endpoint ignores the new request headers.
                # Never silently treat that as a host supporting artifact returns.
                raise HermesResponseError(operation="artifact_return_host_ack")
        try:
            payload = response.json()
            effective_thread_id = _hermes_thread_id(response.headers.get(HERMES_SESSION_HEADER))
        except (ValueError, ValidationError):
            raise HermesResponseError(operation=operation) from None
        if _explicit_incomplete_response(response, payload):
            raise HermesResponseError(operation=operation)
        is_v2_response = isinstance(payload, dict) and (
            "response_id" in payload or "parts" in payload
        )
        if not is_v2_response:
            try:
                completion = HermesChatCompletionResponse.model_validate(payload)
            except ValidationError:
                raise HermesResponseError(operation=operation) from None
            return HermesChatResult(
                assistant_content=completion.choices[0].message.content,
                hermes_thread_id=effective_thread_id,
            )
        try:
            envelope = ResponseEnvelope.model_validate(payload)
        except ValidationError:
            raise HermesResponseError(operation=operation) from None
        return HermesChatResult.from_response(
            envelope,
            hermes_thread_id=effective_thread_id,
        )

    def prepare_inbound_session(
        self,
        session_id: str,
        *,
        parent_session_id: str | None,
        runtime_model: str,
        runtime_provider: str,
        runtime_model_options: dict[str, object],
        allow_create: bool,
        expected_history_digest: str | None,
        record_history: Callable[[str], None],
    ) -> None:
        """Create once or inspect the same durable intent after an uncertain call.

        This deliberately never repairs an unprovable partial fork. The durable
        Dispatch remains uncertain for operator reconciliation, preserving FIFO.
        """
        operation = "inbound_session_preparation"
        child_path = _session_path(session_id)
        parent = None
        try:
            if parent_session_id is not None:
                parent = self._get_session(parent_session_id)
                if parent.get("model") != runtime_model:
                    raise ValueError("parent runtime differs")
            if allow_create:
                if parent is not None:
                    if parent.get("ended_at") is not None or parent.get("end_reason"):
                        raise ValueError("parent is not a live session")
                    expected_history_digest = self._history_digest(parent_session_id)
                else:
                    expected_history_digest = _history_digest([])
                # Caller commits the history proof before a possibly ambiguous
                # create/fork request. Recovery never substitutes another ID.
                record_history(expected_history_digest)
                endpoint = (
                    _session_path(parent_session_id) + "/fork"
                    if parent_session_id is not None
                    else "api/sessions"
                )
                create_payload: dict[str, Any] = {"id": session_id}
                if parent_session_id is None:
                    create_payload.update(
                        model=runtime_model,
                        provider=runtime_provider,
                        model_options=runtime_model_options,
                        require_model_lock=True,
                    )
                response = self._request("POST", endpoint, operation=operation, json=create_payload)
                if response.status_code != 201:
                    raise ValueError("creation was not acknowledged")
                _session_payload(response, session_id)
            if expected_history_digest is None:
                raise ValueError("no durable history proof")
            child = self._get_session(session_id)
            if (
                child.get("parent_session_id") != parent_session_id
                or child.get("ended_at") is not None
                or child.get("end_reason")
                or child.get("model") != runtime_model
                or self._history_digest(session_id) != expected_history_digest
                or (
                    parent is not None
                    and child.get("has_system_prompt") != parent.get("has_system_prompt")
                )
            ):
                raise ValueError("fork proof differs")
            # Official fork copies history/system_prompt, but not the provider
            # or options lock. Reapply the approved immutable execution Profile.
            response = self._request(
                "POST",
                child_path + "/model",
                operation=operation,
                json={
                    "model": runtime_model,
                    "provider": runtime_provider,
                    "model_options": runtime_model_options,
                    "require_model_lock": True,
                },
            )
            ack = response.json()
            runtime = ack.get("runtime", {})
            if (
                ack.get("object") != "hermes.session.model_lock"
                or ack.get("session_id") != session_id
                or runtime.get("model_lock") != "accepted"
                or runtime.get("model") != runtime_model
                or runtime.get("provider") != runtime_provider
                or runtime.get("requested")
                != {
                    "model": runtime_model,
                    "provider": runtime_provider,
                }
            ):
                raise ValueError("runtime lock differs")
        except Exception:
            # Includes transport timeout and partially completed fork/config.
            # None of these may become the ordinary failed/retry path.
            raise HermesResponseError(operation=operation) from None

    def verify_inbound_session_tip(self, session_id: str) -> None:
        """An echoed header is not proof: official old IDs can resolve a new tip."""
        try:
            session = self._get_session(session_id)
            if session.get("ended_at") is not None or session.get("end_reason"):
                raise ValueError("execution session rotated")
            self._history_digest(session_id)  # also checks exact resolved session ID
        except Exception:
            raise HermesResponseError(operation="inbound_session_tip") from None

    def _get_session(self, session_id: str) -> dict[str, Any]:
        response = self._request(
            "GET", _session_path(session_id), operation="inbound_session_inspect", json=None
        )
        return _session_payload(response, session_id)

    def _history_digest(self, session_id: str) -> str:
        messages = []
        # Bound both count and network operations. Oversized history is a visible
        # preparation failure, never silently truncated or replaced with empty.
        for offset in range(0, 10001, 500):
            response = self._request(
                "GET",
                _session_path(session_id) + f"/messages?order=oldest&limit=500&offset={offset}",
                operation="inbound_session_history",
                json=None,
            )
            payload = response.json()
            page = payload.get("data")
            if (
                payload.get("session_id") != session_id
                or not isinstance(page, list)
                or len(page) > 500
                or any(not isinstance(item, dict) for item in page)
            ):
                raise ValueError("history belongs to another session")
            messages.extend(page)
            if len(messages) > 10000:
                raise ValueError("history exceeds preparation limit")
            if len(page) < 500:
                return _history_digest(messages)
        raise ValueError("history exceeds preparation limit")

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        operation: str,
        json: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._closed:
            raise RuntimeError("Hermes client is closed")
        try:
            response = asyncio.run(
                self._request_async(method, endpoint, json=json, headers=headers)
            )
        except TimeoutError:
            raise HermesExecutionTimeoutError(operation=operation) from None
        except httpx.TimeoutException:
            raise HermesTimeoutError(operation=operation) from None
        except httpx.RequestError:
            raise HermesTransportError(operation=operation) from None
        if not 200 <= response.status_code < 300:
            raise HermesAPIError(operation=operation, status_code=response.status_code)
        return response

    async def _request_async(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeouts.httpx_timeout(),
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
            verify=self._ssl_context,
        ) as client:
            # Includes connect/write and the entire body, even when bytes trickle
            # in often enough to reset the independent read inactivity timeout.
            async with asyncio.timeout(self._timeouts.execution_seconds):
                return await client.request(method, endpoint, json=json, headers=headers)


def _explicit_incomplete_response(response: httpx.Response, payload: object) -> bool:
    """A Hermes partial/failed HTTP 200 must not become a successful Dispatch."""
    if (
        response.headers.get("X-Hermes-Completed", "").strip().lower() == "false"
        or response.headers.get("X-Hermes-Partial", "").strip().lower() == "true"
        or bool(response.headers.get("X-Hermes-Error", "").strip())
    ):
        return True
    metadata = payload.get("hermes") if isinstance(payload, dict) else None
    return isinstance(metadata, dict) and (
        metadata.get("completed") is False
        or metadata.get("partial") is True
        or metadata.get("failed") is True
        or bool(metadata.get("error"))
    )


def _session_path(value: str) -> str:
    return "api/sessions/" + quote(_hermes_thread_id(value), safe="")


def _session_payload(response: httpx.Response, expected_id: str) -> dict[str, Any]:
    payload = response.json()
    session = payload.get("session")
    if (
        payload.get("object") != "hermes.session"
        or not isinstance(session, dict)
        or session.get("id") != expected_id
    ):
        raise ValueError("unexpected session response")
    return session


def _history_digest(messages: list[dict[str, Any]]) -> str:
    # Row IDs/session IDs/timestamps change when official fork copies messages;
    # all public semantic content, tool calls and reasoning must be preserved.
    copied = [
        {key: value for key, value in item.items() if key not in {"id", "session_id", "timestamp"}}
        for item in messages
    ]
    return hashlib.sha256(
        json_module.dumps(copied, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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


def _idempotency_key(value: object) -> str:
    key = _required_string(value, "idempotency_key")
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH or any(
        not 0x21 <= ord(character) <= 0x7E for character in key
    ):
        raise ValueError("idempotency_key is invalid")
    return key


def _hermes_thread_id(value: object) -> str:
    thread_id = _required_string(value, "hermes_thread_id")
    if len(thread_id) > MAX_HERMES_THREAD_ID_LENGTH:
        raise ValueError("hermes_thread_id is too long")
    return thread_id
