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
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from cf_agent_gateway.hermes.return_bridge.enterprise_files import EnterpriseFile, export_metadata
from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError, TaskFile, read_task_file
from cf_agent_gateway.hermes.return_bridge.staging import (
    inbound_bytes,
    inbound_file,
    write_staged_file,
)
from cf_agent_gateway.hermes.return_bridge.transport import (
    Publication,
    UploadTransport,
    expected_receipt,
)

_current: ContextVar[ReturnScope | None] = ContextVar("cf_artifact_return_request", default=None)
SCHEMA = "cf-artifact-return/v1"
_BINDING_CLAIMS = {
    "dispatch_id": "dispatch_id",
    "message_id": "message_id",
    "enterprise_identity_id": "identity_id",
    "thread_id": "thread_id",
    "profile_reference": "profile",
    "profile_revision": "revision",
    "session_id": "session_id",
}


def _canonical_uuid(value):
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True, repr=False)
class _Inbound:
    file: TaskFile
    check_active: Callable[[], None] = field(repr=False)

    def projection(self):
        return {
            "file_ref": self.file.reference,
            "kind": self.file.kind,
            "filename": self.file.filename,
            "size_bytes": len(self.file.content),
            "sha256": hashlib.sha256(self.file.content).hexdigest(),
        }


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
        self._binding = MappingProxyType({k: claims.get(v) for k, v in _BINDING_CLAIMS.items()})
        self._enterprise_binding = {
            **dict(self._binding),
            "account_id": claims.get("account_id"),
            "conversation_id": claims.get("conversation_id"),
            "conversation_type": claims.get("conversation_type"),
        }
        self._enterprise_allowed = "original_attachment_constraint" not in claims
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
        self._inbound: dict[int, _Inbound] = {}
        self._inbound_refs: dict[str, _Inbound] = {}
        self._inbound_objects: dict[tuple[int, int], _Inbound] = {}
        self._staging_attempts: dict[int, _Inbound] = {}
        self._enterprise_files: dict[str, tuple[EnterpriseFile, Callable]] = {}

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

    def enterprise_context(self):
        self.check_active()
        if (
            not self._enterprise_allowed
            or self._enterprise_binding.get("conversation_type") != "private"
            or any(value is None or value == "" for value in self._enterprise_binding.values())
        ):
            raise ReturnBridgeError("enterprise_context_unavailable")
        return dict(self._enterprise_binding), self.check_active

    def prepare_enterprise(self, *, export, binding, check_active):
        """Only trusted FileBridge code calls this; not a model tool parameter."""
        expected, _ = self.enterprise_context()
        if binding != expected or any(type(binding[k]) is not type(v) for k, v in expected.items()):
            raise ReturnBridgeError("enterprise_return_binding_mismatch")
        if not callable(check_active):
            raise ReturnBridgeError("enterprise_return_binding_mismatch")
        try:
            export = json.loads(json.dumps(export_metadata(export), ensure_ascii=False))
        except (ValueError, TypeError):
            raise ReturnBridgeError("enterprise_export_invalid") from None
        if export["size_bytes"] > self._max_bytes:
            raise ReturnBridgeError("enterprise_export_too_large")

        def guard():
            self.check_active()
            if check_active() is False:
                raise ReturnBridgeError("enterprise_context_unavailable")

        guard()
        signature = hashlib.sha256(json.dumps(export, sort_keys=True).encode()).hexdigest()
        reference = "enterprise-" + str(uuid5(NAMESPACE_URL, self._response_id + signature))
        file = EnterpriseFile(
            reference, export["name"], export["size_bytes"], export["sha256"], export
        )
        if not self._operation_lock.acquire(timeout=30):
            raise ReturnBridgeError("return_busy")
        try:
            guard()
            old = self._enterprise_files.get(reference)
            if old is not None:
                old[1]()
                if old[0] != file:
                    raise ReturnBridgeError("return_slot_conflict")
            else:
                if len(self._enterprise_files) >= self._max_artifacts:
                    raise ReturnBridgeError("return_slot_limit")
                self._enterprise_files[reference] = file, guard
            return file.projection()
        finally:
            self._operation_lock.release()

    def prepare_inbound(
        self, *, attachment_id, binding, stream, size_bytes, sha256, filename, check_active
    ):
        """Match the trusted host binding; never accept identity from tool arguments."""
        self.check_active()
        if (
            set(binding) != set(_BINDING_CLAIMS) | {"task_id", "turn_id"}
            or any(
                type(binding[key]) is not type(expected)
                or binding[key] != expected
                or expected is None
                for key, expected in self._binding.items()
            )
            or any(
                type(binding[key]) is not int or binding[key] < 1
                for key in (
                    "dispatch_id",
                    "message_id",
                    "profile_revision",
                )
            )
            or any(
                not isinstance(binding[key], str) or not binding[key]
                for key in ("session_id", "task_id", "turn_id", "profile_reference")
            )
            or any(
                not _canonical_uuid(binding[key]) for key in ("enterprise_identity_id", "thread_id")
            )
        ):
            raise ReturnBridgeError("inbound_return_binding_mismatch")
        if type(attachment_id) is not int or attachment_id < 1 or not callable(check_active):
            raise ReturnBridgeError("inbound_return_metadata_invalid")

        def source_active():
            self.check_active()
            try:
                if check_active() is False:
                    raise ValueError()
            except Exception:
                raise ReturnBridgeError("inbound_return_inactive") from None

        source_active()
        if not self._operation_lock.acquire(timeout=30.0):
            raise ReturnBridgeError("return_busy")
        try:
            source_active()
            existing = self._inbound.get(attachment_id)
            previous_attempt = self._staging_attempts.get(attachment_id)
            if previous_attempt is not None:
                previous_attempt.check_active()
                if existing is None:
                    raise ReturnBridgeError("inbound_return_conflict")
            if existing is not None:
                # Never replace the original closure with a fresh lease.
                existing.check_active()
                if (size_bytes, sha256, filename) != (
                    len(existing.file.content),
                    hashlib.sha256(existing.file.content).hexdigest(),
                    existing.file.filename,
                ) or type(size_bytes) is not int:
                    raise ReturnBridgeError("inbound_return_conflict")
            content = inbound_bytes(stream, size_bytes, sha256, self._max_bytes, source_active)
            if existing is not None:
                existing.check_active()
                if content != existing.file.content:
                    raise ReturnBridgeError("inbound_return_conflict")
                on_disk = read_task_file(
                    self.work_root,
                    existing.file.reference,
                    existing.file.kind,
                    existing.file.filename,
                    max_bytes=self._max_bytes,
                    check_active=existing.check_active,
                    check_identity=lambda identity: self._check_inbound_identity(
                        existing, identity
                    ),
                )
                if on_disk != existing.file:
                    raise ReturnBridgeError("inbound_return_conflict")
                return existing.projection()
            # Validate type/name before admitting an exclusive write. Failed
            # write/readback consumes its attachment attempt, bounding remnants
            # without ever deleting or overwriting an unknown file.
            file = inbound_file(filename, content)
            if attachment_id in self._staging_attempts:
                raise ReturnBridgeError("inbound_return_conflict")
            if len(self._staging_attempts) >= self._max_artifacts:
                raise ReturnBridgeError("return_slot_limit")
            staged = _Inbound(file, source_active)
            self._staging_attempts[attachment_id] = staged
            # Even a cancelled/partial exclusive write keeps its source guard.
            # Discovering a failed leaf must never turn it into an unguarded
            # ordinary generated-file return.
            self._inbound_refs[file.reference] = staged

            def created(identity):
                if identity in self._inbound_objects:
                    raise ReturnBridgeError("inbound_return_conflict")
                self._inbound_objects[identity] = staged
                source_active()

            write_staged_file(self.work_root, file, source_active, created)
            self._inbound[attachment_id] = staged
            return staged.projection()
        finally:
            self._operation_lock.release()

    def _check_inbound_identity(self, expected, identity):
        # Check the opened object before any byte read. Windows case/8.3 names
        # cannot lose a source guard or allocate another slot through an alias.
        actual = self._inbound_objects.get(identity)
        if actual is not None:
            actual.check_active()
        if actual is not expected:
            raise ReturnBridgeError("inbound_return_conflict")

    def return_file(self, file_ref: str, kind: str, filename: str | None = None) -> dict:
        self.check_active()
        # All calls in one request share a finite slot/budget ledger. Different
        # requests never share this object, even when session_id is the same.
        if not self._operation_lock.acquire(timeout=30.0):
            raise ReturnBridgeError("return_busy")
        try:
            self.check_active()
            remote = self._enterprise_files.get(file_ref) if isinstance(file_ref, str) else None
            if remote is not None:
                file, guard = remote
                guard()
                if kind != file.kind or filename not in {None, file.filename}:
                    raise ReturnBridgeError("enterprise_return_conflict")
                key = (file.reference, file.kind, file.filename)
                item = self._publications.get(key)
                if item is None:
                    slot = len(self._publications)
                    if slot >= self._max_artifacts:
                        raise ReturnBridgeError("return_slot_limit")
                    artifact_id = str(uuid5(NAMESPACE_URL, f"{SCHEMA}/{self._response_id}/{slot}"))
                    item = Publication(
                        slot,
                        file,
                        expected_receipt(
                            file, artifact_id=artifact_id, response_id=self._response_id
                        ),
                        next_method="POST",
                        check_source=guard,
                    )
                    self._publications[key] = item
                return self._transport.publish(self, item)
            inbound = self._inbound_refs.get(file_ref) if isinstance(file_ref, str) else None
            guard = self.check_active if inbound is None else inbound.check_active
            guard()
            if inbound is not None:
                filename = inbound.file.filename if filename is None else filename
                if (kind, filename) != (inbound.file.kind, inbound.file.filename):
                    raise ReturnBridgeError("inbound_return_conflict")
            file = read_task_file(
                self.work_root,
                file_ref,
                kind,
                filename,
                max_bytes=self._max_bytes,
                check_active=guard,
                check_identity=lambda identity: self._check_inbound_identity(inbound, identity),
            )
            guard()
            if inbound is not None and file != inbound.file:
                raise ReturnBridgeError("inbound_return_conflict")
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
                    check_source=guard,
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


def trusted_enterprise_context():
    """Internal trusted plugin bridge, intentionally absent from model schema."""
    scope = _current.get()
    if scope is None:
        raise ReturnBridgeError("enterprise_context_unavailable")
    return scope.enterprise_context()


def prepare_enterprise_return(*, export, binding, check_active):
    scope = _current.get()
    if scope is None:
        raise ReturnBridgeError("enterprise_context_unavailable")
    return scope.prepare_enterprise(export=export, binding=binding, check_active=check_active)


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
            ("max_bytes", 1, 128 * 1024 * 1024),
            ("max_artifacts", 1, 8),
        ):
            if type(claims[field]) is not int or not lower <= claims[field] <= upper:
                raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{64}", claims["claim"]):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ReturnBridgeError("invalid_return_context") from None
    return claims
