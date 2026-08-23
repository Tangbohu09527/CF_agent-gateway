from __future__ import annotations

from collections import deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Bound request bodies before framework parsing, including chunked bodies."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is None:
            await _error_response(scope, receive, send, 400, "invalid content length")
            return
        if content_length > self._max_body_bytes:
            await _error_response(scope, receive, send, 413, "request body too large")
            return

        buffered: deque[Message] = deque()
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self._max_body_bytes:
                await _error_response(scope, receive, send, 413, "request body too large")
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            if buffered:
                return buffered.popleft()
            return await receive()

        await self._app(scope, replay, send)


def _content_length(scope: Scope) -> int | None:
    values = [value for key, value in scope.get("headers", []) if key.lower() == b"content-length"]
    if not values:
        return 0
    if len(values) != 1:
        return None
    try:
        decoded = values[0].decode("ascii")
        if not decoded.isdigit():
            return None
        return int(decoded)
    except (UnicodeDecodeError, ValueError, OverflowError):
        return None


async def _error_response(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    detail: str,
) -> None:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    await response(scope, receive, send)
