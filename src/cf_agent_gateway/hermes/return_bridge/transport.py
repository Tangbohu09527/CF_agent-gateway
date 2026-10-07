"""Bounded HTTPS publication into the existing Gateway artifact endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError, TaskFile
from cf_agent_gateway.hermes.tls import verified_ssl_context

if TYPE_CHECKING:
    from cf_agent_gateway.hermes.return_bridge.scope import ReturnScope

RETURN_BUDGET_SECONDS = 30.0


@dataclass(slots=True)
class Publication:
    slot: int
    file: TaskFile
    expected: dict
    attempts: int = 0
    deadline: float = 0.0
    next_method: str = "PUT"
    terminal: bool = False
    receipt: dict | None = None


def expected_receipt(file: TaskFile, *, artifact_id: str, response_id: str) -> dict:
    return {
        "artifact_id": artifact_id,
        "response_id": response_id,
        "status": "ready",
        "filename": file.filename,
        "kind": file.kind,
        "mime_type": file.mime_type,
        "size": len(file.content),
        "sha256": hashlib.sha256(file.content).hexdigest(),
    }


class UploadTransport:
    def __init__(self, ca_file: str | None = None):
        self._ssl_context = verified_ssl_context(ca_file)
        self._closed = threading.Event()

    def close(self):
        # A client belongs only to the temporary event loop executing one HTTP
        # exchange. The revocation watcher cancels it on its own loop; callers
        # never close an AsyncClient from another thread or event loop.
        self._closed.set()

    def publish(self, scope: ReturnScope, item: Publication) -> dict:
        if item.terminal:
            raise ReturnBridgeError("return_unavailable")
        if item.receipt is not None:
            scope.check_active()
            return _projection(item.receipt)
        if not item.deadline:
            item.deadline = time.monotonic() + RETURN_BUDGET_SECONDS
        while item.attempts < 3 and time.monotonic() < item.deadline:
            scope.check_active()
            headers = {"Authorization": scope._authorization_for_transport()}
            method = item.next_method
            if method == "PUT":
                headers.update(
                    {
                        "X-CF-Return-Intent": "current-chat",
                        "X-CF-Filename": quote(item.file.filename, safe=""),
                        "X-CF-Artifact-Kind": item.file.kind,
                        "Content-Type": item.file.mime_type,
                        "Content-Length": str(len(item.file.content)),
                        "X-CF-Content-SHA256": item.expected["sha256"],
                    }
                )
            item.attempts += 1
            # Any ambiguous PUT is followed by a query of this same slot, never
            # a new artifact/session. Attempts and deadline survive tool replays.
            item.next_method = "GET"
            try:
                with scope.network_guard():
                    remaining = item.deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    status, retry, receipt = asyncio.run(
                        self._exchange(scope, item, method, headers, remaining)
                    )
            except (httpx.TransportError, OSError, TimeoutError):
                scope.check_active()
                continue
            except ReturnBridgeError:
                raise
            except (ValueError, UnicodeError):
                item.terminal = True
                raise ReturnBridgeError("invalid_gateway_receipt") from None
            finally:
                # Local references are dropped promptly; no normal logs/traceback
                # include headers or raw upstream error strings.
                headers.clear()
            scope.check_active()
            if status == 200:
                # bool is an int subclass, but is never a valid content length.
                if (
                    not isinstance(receipt, dict)
                    or type(receipt.get("size")) is not int
                    or receipt != item.expected
                ):
                    item.terminal = True
                    raise ReturnBridgeError("invalid_gateway_receipt")
                item.receipt = receipt
                return _projection(receipt)
            if status == 404 and method == "GET":
                item.next_method = "PUT"
                continue
            if status in {429, 503}:
                try:
                    delay = min(2.0, max(0.0, float(retry)))
                except ValueError:
                    delay = 1.0
                scope.wait_active(min(delay, max(0.0, item.deadline - time.monotonic())))
                continue
            if status >= 500:
                # A server error can follow a committed write. Keep querying its
                # original slot within the same finite budget.
                continue
            item.terminal = True
            raise ReturnBridgeError("gateway_rejected_return")
        raise ReturnBridgeError("return_receipt_uncertain")

    async def _exchange(self, scope, item, method, headers, remaining):
        # HTTPX timeouts are per phase/read, not a total wall-clock budget. The
        # outer timeout includes connect/TLS, request, headers and the whole body.
        async with asyncio.timeout(max(0.0, item.deadline - time.monotonic())):
            async with httpx.AsyncClient(
                verify=self._ssl_context,
                trust_env=False,
                follow_redirects=False,
                timeout=min(5.0, remaining),
            ) as client:
                transfer = asyncio.create_task(self._request(client, scope, item, method, headers))
                revoked = asyncio.create_task(self._watch_revocation(scope))
                try:
                    done, _ = await asyncio.wait(
                        (transfer, revoked),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if revoked in done:
                        # Prefer revocation if completion and cancellation race.
                        await revoked
                    result = await transfer
                    scope.check_active()
                    return result
                finally:
                    for task in (transfer, revoked):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(transfer, revoked, return_exceptions=True)

    async def _watch_revocation(self, scope):
        while not self._closed.is_set():
            scope.check_active()
            await asyncio.sleep(0.01)
        raise ReturnBridgeError("return_scope_closed")

    async def _request(self, client, scope, item, method, headers):
        scope.check_active()
        async with client.stream(
            method,
            scope._slot_url(item.slot),
            headers=headers,
            content=item.file.content if method == "PUT" else None,
        ) as response:
            status = response.status_code
            receipt = json.loads(await _bounded_read(response, scope)) if status == 200 else None
            return status, response.headers.get("Retry-After", "1"), receipt


async def _bounded_read(response: httpx.Response, scope: ReturnScope) -> bytes:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        scope.check_active()
        data.extend(chunk)
        if len(data) > 8192:
            raise ValueError()
    return bytes(data)


def _projection(receipt: dict) -> dict:
    return {
        "success": True,
        "status": "pending_delivery",
        "artifact_id": receipt["artifact_id"],
        "filename": receipt["filename"],
        "kind": receipt["kind"],
        "size": receipt["size"],
        "sha256": receipt["sha256"],
        "message": (
            "Handed to Gateway; delivery waits for final task success. Not a WeChat receipt."
        ),
    }
