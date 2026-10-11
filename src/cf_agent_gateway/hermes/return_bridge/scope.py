"""One authenticated HTTP request's revocable, private return authorization.

The official entry adapter must authenticate the caller before constructing this
object and explicitly propagate its instance to worker threads. A session ID is
only a consistency check, never a scope lookup key or authorization source.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError, read_task_file
from cf_agent_gateway.hermes.return_bridge.transport import (
    Publication,
    UploadTransport,
    expected_receipt,
)

_current: ContextVar[ReturnScope | None] = ContextVar("cf_artifact_return_request", default=None)
SCHEMA = "cf-artifact-return/v1"


class ReturnScope:
    def __init__(
        self,
        *,
        return_url: str,
        authorization: str,
        gateway_origin: str,
        request_id: str,
        work_root: Path,
        session_id: str,
        ca_file: str | None = None,
    ):
        claims = _claims(authorization, session_id)
        self._url = _return_url(return_url, gateway_origin, claims["dispatch_id"])
        self._authorization = authorization
        self._ack = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        self._expires = claims["expires"]
        self._max_bytes = claims["max_bytes"]
        self._max_artifacts = claims["max_artifacts"]
        snapshot = {k: v for k, v in claims.items() if k not in {"schema", "session_id"}}
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._response_id = f"return:{claims['dispatch_id']}:{claims['claim']}:{digest}"
        self.request_id = request_id
        self.work_root = Path(work_root)
        self._cancelled = threading.Event()
        self._operation_lock = threading.Lock()
        self._network_lock = threading.Lock()
        self._publications: dict[tuple[str, str, str], Publication] = {}
        self._transport = UploadTransport(ca_file)

    def __repr__(self):
        return "<ReturnScope active>" if not self._cancelled.is_set() else "<ReturnScope closed>"

    @property
    def accepted_ack(self) -> str:
        """Backend response header only; not a model-visible tool field."""
        return self._ack

    def check_active(self):
        if self._cancelled.is_set() or time.time() >= self._expires:
            raise ReturnBridgeError("return_scope_closed")

    def wait_active(self, seconds: float):
        if self._cancelled.wait(seconds):
            raise ReturnBridgeError("return_scope_closed")
        self.check_active()

    @contextmanager
    def network_guard(self):
        # close() revokes before waiting. Copied ContextVars and queued thread
        # calls retain this same instance and cannot start another PUT or GET.
        with self._network_lock:
            self.check_active()
            yield

    def _authorization_for_transport(self):
        self.check_active()
        return self._authorization

    def _slot_url(self, slot):
        return f"{self._url}/artifacts/{slot}"

    def return_file(self, file_ref: str, kind: str, filename: str | None = None) -> dict:
        self.check_active()
        # All calls in one request share a finite slot/budget ledger. Different
        # requests never share this object, even when session_id is the same.
        if not self._operation_lock.acquire(timeout=30.0):
            raise ReturnBridgeError("return_busy")
        try:
            self.check_active()
            file = read_task_file(
                self.work_root, file_ref, kind, filename, max_bytes=self._max_bytes
            )
            self.check_active()
            key = (file.reference, file.kind, file.filename)
            item = self._publications.get(key)
            if item is not None:
                if file.content != item.file.content:
                    raise ReturnBridgeError("return_slot_conflict")
            else:
                slot = len(self._publications)
                if slot >= self._max_artifacts:
                    raise ReturnBridgeError("return_slot_limit")
                artifact_id = str(uuid5(NAMESPACE_URL, f"{SCHEMA}/{self._response_id}/{slot}"))
                item = Publication(
                    slot,
                    file,
                    expected_receipt(file, artifact_id=artifact_id, response_id=self._response_id),
                )
                self._publications[key] = item
            return self._transport.publish(self, item)
        finally:
            self._operation_lock.release()

    def revoke(self):
        """Immediate event-loop-safe revocation, with no network or blocking wait."""
        self._cancelled.set()
        self._authorization = ""

    def close(self):
        """Revoke immediately, then drain an admitted HTTPS exchange boundedly.

        A PUT already received by Gateway cannot be recalled. Its READY artifact
        still needs the Gateway's successful current-claim completion fence.
        No retry/query/new upload is authorized after revocation.
        """
        self.revoke()
        acquired = self._network_lock.acquire(timeout=30.0)
        try:
            self._transport.close()
        finally:
            if acquired:
                self._network_lock.release()


@contextmanager
def activate_scope(scope: ReturnScope, *, close_on_exit: bool = False):
    """Explicit worker propagation; request middleware owns lifecycle/close."""
    scope.check_active()
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
        if close_on_exit:
            scope.close()


def return_current_chat(file_ref: str, kind: str, filename: str | None = None) -> dict:
    """The complete model-visible API: no URL, capability, session or chat ID."""
    try:
        scope = _current.get()
        if scope is None:
            raise ReturnBridgeError("return_scope_unavailable")
        return scope.return_file(file_ref, kind, filename)
    except ReturnBridgeError as error:
        return {"success": False, "error": error.code}
    except Exception:
        return {"success": False, "error": "return_unavailable"}


def _return_url(value, origin, dispatch_id):
    try:
        if not isinstance(value, str) or not isinstance(origin, str):
            raise ValueError()
        if any(ord(c) < 33 or ord(c) > 126 for c in value + origin):
            raise ValueError()
        target, approved = urlsplit(value), urlsplit(origin)
        for item in (target, approved):
            if (
                item.scheme != "https"
                or not item.hostname
                or item.username
                or item.password
                or item.query
                or item.fragment
                or item.port == 0
                or not item.port
                and item.netloc.endswith(":")
                or "%" in item.netloc
                or "\\" in item.netloc
            ):
                raise ValueError()
        if (
            approved.path not in {"", "/"}
            or target.netloc != approved.netloc
            or target.path != f"/internal/hermes/returns/{dispatch_id}"
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ReturnBridgeError("invalid_return_context") from None
    return value


def _claims(authorization, session_id):
    # This is consistency validation, NOT signature verification. Only Gateway
    # knows its signing secret; the entry adapter first authenticates its caller.
    try:
        if not isinstance(authorization, str) or len(authorization) > 4096:
            raise ValueError()
        scheme, token = authorization.split(" ", 1)
        payload, signature = token.split(".")
        if scheme != "Bearer" or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError()
        claims = json.loads(
            base64.b64decode(
                payload + "=" * (-len(payload) % 4),
                altchars=b"-_",
                validate=True,
            )
        )
        if not isinstance(claims, dict) or claims.get("schema") != SCHEMA:
            raise ValueError()
        if not isinstance(session_id, str) or not session_id or claims["session_id"] != session_id:
            raise ValueError()
        for field, lower, upper in (
            ("dispatch_id", 1, 2**63 - 1),
            ("expires", int(time.time()) + 1, 2**63 - 1),
            ("max_bytes", 1, 1_048_576),
            ("max_artifacts", 1, 8),
        ):
            if type(claims[field]) is not int or not lower <= claims[field] <= upper:
                raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{64}", claims["claim"]):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ReturnBridgeError("invalid_return_context") from None
    return claims
