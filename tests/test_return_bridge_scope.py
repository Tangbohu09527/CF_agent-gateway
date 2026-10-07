"""Host return constraints with real loopback TLS; the Gateway peer is synthetic.

Full Gateway/official Hermes/Delivery tests are separate. No installed profile,
credential, service, model or approved business sample is touched here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import unquote
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_hermes_tls import _test_certificates

from cf_agent_gateway.hermes.return_bridge import files
from cf_agent_gateway.hermes.return_bridge import transport as return_transport
from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError, read_task_file
from cf_agent_gateway.hermes.return_bridge.scope import (
    ReturnScope,
    activate_scope,
    return_current_chat,
)

PDF = b"%PDF-1.7\nsynthetic test input\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRsynthetic\x00\x00\x00\x00IEND\xaeB`\x82"


def _capability(session="same-session", dispatch=42, **overrides):
    claims = {
        "schema": "cf-artifact-return/v1",
        "session_id": session,
        "dispatch_id": dispatch,
        "claim": "a" * 64,
        "expires": int(time.time()) + 600,
        "max_bytes": 1_048_576,
        "max_artifacts": 4,
        "source": "synthetic-source",
        **overrides,
    }
    data = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    return "Bearer " + base64.urlsafe_b64encode(data).rstrip(b"=").decode() + "." + "b" * 64


def _receipt(authorization, slot, headers, content):
    encoded = authorization.split(" ", 1)[1].split(".")[0]
    claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    snapshot = {key: value for key, value in claims.items() if key not in {"schema", "session_id"}}
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    response = f"return:{claims['dispatch_id']}:{claims['claim']}:{digest}"
    return {
        "response_id": response,
        "artifact_id": str(uuid5(NAMESPACE_URL, f"cf-artifact-return/v1/{response}/{slot}")),
        "status": "ready",
        "filename": unquote(headers["X-CF-Filename"]),
        "kind": headers["X-CF-Artifact-Kind"],
        "mime_type": headers["Content-Type"],
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


@contextmanager
def _https_gateway(tmp_path, *, mode="normal", change=None, block=None):
    ca, cert, key = _test_certificates(tmp_path)
    calls, stored = [], {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_PUT(self):
            authorization = self.headers.get("Authorization", "")
            content = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(("PUT", self.path, hashlib.sha256(content).hexdigest()))
            if block is not None:
                block[0].set()
                assert block[1].wait(10)
            if mode == "redirect":
                self._reply(307, {}, {"Location": "http://127.0.0.1:1/forbidden"})
                return
            if mode == "unavailable":
                self._reply(503, {}, {"Retry-After": "0"})
                return
            value = _receipt(authorization, int(self.path.rsplit("/", 1)[1]), self.headers, content)
            stored[(authorization, self.path)] = value
            if mode == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if change:
                value = {**value, **change}
            if mode == "slow_headers":
                time.sleep(4.0)
            elif mode == "slow_body":
                content = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(content[:1])
                    self.wfile.flush()
                    time.sleep(4.0)
                    self.wfile.write(content[1:])
                return
            self._reply(200, value)

        def do_GET(self):
            calls.append(("GET", self.path, None))
            value = stored.get((self.headers.get("Authorization"), self.path))
            if mode == "unavailable":
                self._reply(503, {}, {"Retry-After": "0"})
            else:
                self._reply(200 if value else 404, value or {})

        def _reply(self, status, data, headers=None):
            content = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(content)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield SimpleNamespace(
            origin=f"https://localhost:{server.server_port}", ca=ca, calls=calls, stored=stored
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def _scope(tmp_path, peer, *, session="same-session", dispatch=42, **kwargs):
    root = tmp_path / f"task-{dispatch}"
    root.mkdir(exist_ok=True)
    (root / "sample.pdf").write_bytes(PDF)
    (root / "sample.png").write_bytes(PNG)
    return ReturnScope(
        return_url=f"{peer.origin}/internal/hermes/returns/{dispatch}",
        authorization=_capability(session, dispatch),
        gateway_origin=peer.origin,
        request_id=f"request-{dispatch}",
        work_root=root,
        session_id=session,
        ca_file=str(peer.ca),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("reference", "kind", "expected"),
    [
        ("sample.pdf", "file", PDF),
        ("sample.png", "image", PNG),
    ],
)
def test_explicit_return_real_tls_ready_and_same_slot_replay(tmp_path, reference, kind, expected):
    with _https_gateway(tmp_path) as peer:
        scope = _scope(tmp_path, peer)
        try:
            with activate_scope(scope):
                result = return_current_chat(reference, kind)
                assert result["success"] and result["status"] == "pending_delivery"
                assert return_current_chat(reference, kind) == result
            assert peer.calls == [
                (
                    "PUT",
                    "/internal/hermes/returns/42/artifacts/0",
                    hashlib.sha256(expected).hexdigest(),
                )
            ]
            assert result["sha256"] == hashlib.sha256(expected).hexdigest()
            assert "response_id" not in result
            assert "Bearer" not in repr(scope) + json.dumps(result)
            assert "Not a WeChat receipt" in result["message"]
            actual_authorization = next(iter(peer.stored))[0]
            assert scope.accepted_ack == hashlib.sha256(actual_authorization.encode()).hexdigest()
        finally:
            scope.close()


def test_read_only_scope_never_uploads_and_ordinary_request_has_no_tool_authority(tmp_path):
    assert return_current_chat("sample.pdf", "file") == {
        "success": False,
        "error": "return_scope_unavailable",
    }
    with _https_gateway(tmp_path) as peer:
        scope = _scope(tmp_path, peer)
        with activate_scope(scope, close_on_exit=True):
            assert scope.accepted_ack
        assert peer.calls == []


def test_uncertain_put_queries_original_slot_without_new_upload(tmp_path):
    with _https_gateway(tmp_path, mode="drop") as peer:
        scope = _scope(tmp_path, peer)
        try:
            result = scope.return_file("sample.pdf", "file")
            assert result["success"]
            assert [call[:2] for call in peer.calls] == [
                ("PUT", "/internal/hermes/returns/42/artifacts/0"),
                ("GET", "/internal/hermes/returns/42/artifacts/0"),
            ]
        finally:
            scope.close()


def test_retry_budget_persists_after_tool_replay(tmp_path):
    with _https_gateway(tmp_path, mode="unavailable") as peer:
        scope = _scope(tmp_path, peer)
        try:
            for _ in range(2):
                with pytest.raises(ReturnBridgeError, match="return_receipt_uncertain"):
                    scope.return_file("sample.pdf", "file")
            assert [call[0] for call in peer.calls] == ["PUT", "GET", "GET"]
        finally:
            scope.close()


def test_elapsed_deadline_cannot_be_refreshed_by_another_tool_call(tmp_path):
    with _https_gateway(tmp_path, mode="unavailable") as peer:
        scope = _scope(tmp_path, peer)
        try:
            with pytest.raises(ReturnBridgeError, match="return_receipt_uncertain"):
                scope.return_file("sample.pdf", "file")
            item = next(iter(scope._publications.values()))
            item.attempts = 1  # independently exercise deadline, not the attempt limit
            item.deadline = time.monotonic() - 1
            count = len(peer.calls)
            with pytest.raises(ReturnBridgeError, match="return_receipt_uncertain"):
                scope.return_file("sample.pdf", "file")
            assert len(peer.calls) == count
        finally:
            scope.close()


@pytest.mark.parametrize("mode", ["slow_headers", "slow_body"])
def test_total_https_deadline_bounds_headers_and_stalled_body(tmp_path, monkeypatch, mode):
    # Accelerate only the fixed total budget; the real TLS and async HTTP path
    # remain unchanged. Each new phase/read must not reset the remaining budget.
    monkeypatch.setattr(return_transport, "RETURN_BUDGET_SECONDS", 2.0)
    with _https_gateway(tmp_path, mode=mode) as peer:
        scope = _scope(tmp_path, peer)
        try:
            started = time.monotonic()
            with pytest.raises(ReturnBridgeError, match="return_receipt_uncertain"):
                scope.return_file("sample.pdf", "file")
            assert time.monotonic() - started < 2.7
            assert [call[0] for call in peer.calls] == ["PUT"]
            with pytest.raises(ReturnBridgeError, match="return_receipt_uncertain"):
                scope.return_file("sample.pdf", "file")
            assert [call[0] for call in peer.calls] == ["PUT"]
        finally:
            scope.close()


def test_revocation_cancels_waiting_https_before_upstream_releases(tmp_path):
    entered, release = threading.Event(), threading.Event()
    with _https_gateway(tmp_path, block=(entered, release)) as peer:
        scope = _scope(tmp_path, peer)
        try:
            with ThreadPoolExecutor(1) as pool:
                upload = pool.submit(scope.return_file, "sample.pdf", "file")
                assert entered.wait(5)
                started = time.monotonic()
                scope.revoke()
                with pytest.raises(ReturnBridgeError, match="return_scope_closed"):
                    upload.result(timeout=1)
                assert time.monotonic() - started < 0.7
                assert not release.is_set()
                assert [call[0] for call in peer.calls] == ["PUT"]
        finally:
            release.set()
            scope.close()


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "f" * 64},
        {"response_id": "another-task"},
        {"artifact_id": "foreign"},
        {"status": "created"},
        {"size": True},
        {"filename": "other.pdf"},
        {"extra": "untrusted"},
    ],
)
def test_receipt_metadata_or_task_mismatch_fails_closed(tmp_path, change):
    with _https_gateway(tmp_path, change=change) as peer:
        scope = _scope(tmp_path, peer)
        try:
            with pytest.raises(ReturnBridgeError, match="invalid_gateway_receipt"):
                scope.return_file("sample.pdf", "file")
            with pytest.raises(ReturnBridgeError, match="return_unavailable"):
                scope.return_file("sample.pdf", "file")
            assert len(peer.calls) == 1
        finally:
            scope.close()


def test_redirect_is_not_followed(tmp_path):
    with _https_gateway(tmp_path, mode="redirect") as peer:
        scope = _scope(tmp_path, peer)
        try:
            with pytest.raises(ReturnBridgeError, match="gateway_rejected_return"):
                scope.return_file("sample.pdf", "file")
            assert len(peer.calls) == 1
        finally:
            scope.close()


@pytest.mark.parametrize("wrong", ["ca", "hostname"])
def test_https_wrong_ca_and_hostname_no_plaintext_fallback(tmp_path, wrong):
    with _https_gateway(tmp_path) as peer:
        if wrong == "ca":
            other = tmp_path / "other-ca"
            other.mkdir()
            peer.ca = _test_certificates(other)[0]
        else:
            peer.origin = peer.origin.replace("localhost", "127.0.0.1")
        scope = _scope(tmp_path, peer)
        try:
            with pytest.raises(ReturnBridgeError, match="return_receipt_uncertain"):
                scope.return_file("sample.pdf", "file")
            assert peer.calls == []
        finally:
            scope.close()


@pytest.mark.parametrize(
    "suffix",
    [
        "/internal/hermes/returns/43",
        "/internal/hermes/returns/042",
        "/internal/hermes/returns/42/",
        "/internal/hermes/returns/42?x=y",
        "/internal/hermes/returns/42#fragment",
        "/internal/hermes/returns/42/artifacts/0",
        "/internal/hermes/returns/../42",
    ],
)
def test_url_must_be_exact_configured_origin_and_canonical_dispatch_path(tmp_path, suffix):
    with pytest.raises(ReturnBridgeError, match="invalid_return_context"):
        ReturnScope(
            return_url="https://gateway.invalid" + suffix,
            gateway_origin="https://gateway.invalid",
            authorization=_capability(),
            request_id="request",
            session_id="same-session",
            work_root=tmp_path,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.invalid/internal/hermes/returns/42",
        "https://attacker.invalid/internal/hermes/returns/42",
        "https://gateway.invalid:444/internal/hermes/returns/42",
        "https://user:secret@gateway.invalid/internal/hermes/returns/42",
    ],
)
def test_url_rejects_other_origins_plaintext_and_userinfo(tmp_path, url):
    with pytest.raises(ReturnBridgeError, match="invalid_return_context"):
        ReturnScope(
            return_url=url,
            gateway_origin="https://gateway.invalid",
            authorization=_capability(),
            request_id="request",
            session_id="same-session",
            work_root=tmp_path,
        )


@pytest.mark.parametrize(
    "claims",
    [
        {"session": "old-session"},
        {"expires": int(time.time()) - 1},
        {"max_bytes": 1_048_577},
        {"max_artifacts": 9},
        {"dispatch_id": True},
        {"claim": "bad"},
    ],
)
def test_invalid_expired_or_different_session_context_rejected(tmp_path, claims):
    with pytest.raises(ReturnBridgeError, match="invalid_return_context"):
        ReturnScope(
            return_url="https://gateway.invalid/internal/hermes/returns/42",
            gateway_origin="https://gateway.invalid",
            authorization=_capability(**claims),
            request_id="request",
            session_id="same-session",
            work_root=tmp_path,
        )


def test_concurrent_requests_same_session_have_distinct_authority_and_slots(tmp_path):
    with _https_gateway(tmp_path) as peer:
        first, second = _scope(tmp_path, peer, dispatch=42), _scope(tmp_path, peer, dispatch=43)
        try:

            def perform(scope, reference, kind):
                with activate_scope(scope):
                    return return_current_chat(reference, kind)

            with ThreadPoolExecutor(2) as pool:
                one = pool.submit(perform, first, "sample.pdf", "file")
                two = pool.submit(perform, second, "sample.png", "image")
                one, two = one.result(), two.result()
            assert one["artifact_id"] != two["artifact_id"]
            assert {call[1] for call in peer.calls} == {
                "/internal/hermes/returns/42/artifacts/0",
                "/internal/hermes/returns/43/artifacts/0",
            }
            first.close()
            with pytest.raises(ReturnBridgeError, match="return_scope_closed"):
                first.return_file("sample.pdf", "file")
            assert second.return_file("sample.png", "image") == two
        finally:
            first.close()
            second.close()


def test_copied_thread_context_late_call_rejected_after_request_close(tmp_path):
    with _https_gateway(tmp_path) as peer:
        scope = _scope(tmp_path, peer)
        with activate_scope(scope):
            copied = copy_context()
        scope.close()
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(copied.run, return_current_chat, "sample.pdf", "file").result()
        assert result == {"success": False, "error": "return_scope_closed"}
        assert peer.calls == []


def test_immediate_revoke_and_expiry_prevent_file_read_or_new_upload(tmp_path):
    with _https_gateway(tmp_path) as peer:
        scope = _scope(tmp_path, peer)
        try:
            scope._expires = time.time() - 1
            with pytest.raises(ReturnBridgeError, match="return_scope_closed"):
                scope.return_file("missing.pdf", "file")
            scope._expires = time.time() + 600
            scope.revoke()
            assert scope._authorization == ""
            with pytest.raises(ReturnBridgeError, match="return_scope_closed"):
                scope.return_file("missing.pdf", "file")
            assert peer.calls == []
        finally:
            scope.close()


def test_concurrent_duplicate_call_uses_one_slot_and_one_put(tmp_path):
    with _https_gateway(tmp_path) as peer:
        scope = _scope(tmp_path, peer)
        try:
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(scope.return_file, "sample.pdf", "file")
                second = pool.submit(scope.return_file, "sample.pdf", "file")
                assert first.result() == second.result()
            assert len(peer.calls) == 1
        finally:
            scope.close()


def test_model_argument_type_errors_are_safe_tool_results(tmp_path):
    with _https_gateway(tmp_path) as peer:
        scope = _scope(tmp_path, peer)
        try:
            with activate_scope(scope):
                assert return_current_chat("sample.pdf", {}) == {
                    "success": False,
                    "error": "invalid_file_type",
                }
                assert return_current_chat([], "file") == {
                    "success": False,
                    "error": "invalid_file_reference",
                }
                assert return_current_chat("sample.pdf", "file", "\ud800.pdf") == {
                    "success": False,
                    "error": "invalid_file_reference",
                }
            assert peer.calls == []
        finally:
            scope.close()


def test_cancel_inflight_does_not_authorize_retry_or_report_success(tmp_path):
    admitted, release = threading.Event(), threading.Event()
    with _https_gateway(tmp_path, mode="drop", block=(admitted, release)) as peer:
        scope = _scope(tmp_path, peer)
        with ThreadPoolExecutor(2) as pool:
            upload = pool.submit(scope.return_file, "sample.pdf", "file")
            assert admitted.wait(5)
            closed = pool.submit(scope.close)
            assert scope._cancelled.wait(5)
            release.set()
            with pytest.raises(ReturnBridgeError, match="return_scope_closed"):
                upload.result(timeout=10)
            closed.result(timeout=10)
        assert len(peer.calls) == 1  # admitted PUT is irreversible; no GET after cancel


def test_mutated_file_cannot_replace_an_existing_slot(tmp_path):
    with _https_gateway(tmp_path) as peer:
        scope = _scope(tmp_path, peer)
        try:
            scope.return_file("sample.pdf", "file")
            (scope.work_root / "sample.pdf").write_bytes(PDF.replace(b"synthetic", b"changed"))
            with pytest.raises(ReturnBridgeError, match="return_slot_conflict"):
                scope.return_file("sample.pdf", "file")
            assert len(peer.calls) == 1
        finally:
            scope.close()


@pytest.mark.parametrize(
    "reference",
    [
        "../other.pdf",
        "/tmp/a.pdf",
        "C:/user/secret.pdf",
        "sample.pdf:stream",
        "folder//a.pdf",
        "folder/./a.pdf",
        "folder\\a.pdf",
        "CON.pdf",
        "CON .pdf",
        "sample.pdf ",
        "a\x00.pdf",
    ],
)
def test_unsafe_paths_are_rejected_without_reading(tmp_path, reference):
    with pytest.raises(ReturnBridgeError, match="invalid_file_reference"):
        read_task_file(tmp_path, reference, "file")


def test_oversize_hardlinks_and_wrong_content_rejected(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    os.link(source, tmp_path / "link.pdf")
    with pytest.raises(ReturnBridgeError, match="file_unavailable"):
        read_task_file(tmp_path, "link.pdf", "file")
    source = tmp_path / "large.pdf"
    source.write_bytes(b"x" * 1_048_577)
    with pytest.raises(ReturnBridgeError, match="file_unavailable"):
        read_task_file(tmp_path, "large.pdf", "file")
    (tmp_path / "false.png").write_bytes(PDF)
    with pytest.raises(ReturnBridgeError, match="invalid_file_type"):
        read_task_file(tmp_path, "false.png", "image")


def test_symlink_file_and_directory_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.pdf").write_bytes(PDF)
    root = tmp_path / "task"
    root.mkdir()
    try:
        (root / "link.pdf").symlink_to(outside / "source.pdf")
        (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip(
                "Windows symlink creation privilege unavailable; reparse guard tested in CI"
            )
        raise
    for reference in ("link.pdf", "linked-dir/source.pdf"):
        with pytest.raises(ReturnBridgeError, match="file_unavailable"):
            read_task_file(root, reference, "file")


def test_replacement_during_read_is_blocked_or_detected(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(PDF)
    original = files._read_descriptor
    blocked = []

    def replace_during_read(fd, limit):
        content = original(fd, limit)
        try:
            path.rename(tmp_path / "moved.pdf")
            path.write_bytes(PDF.replace(b"synthetic", b"replacement"))
        except PermissionError:
            blocked.append(True)
        return content

    monkeypatch.setattr(files, "_read_descriptor", replace_during_read)
    if os.name == "nt":
        assert read_task_file(tmp_path, "source.pdf", "file").content == PDF
        assert blocked
    else:
        with pytest.raises(ReturnBridgeError, match="file_unavailable"):
            read_task_file(tmp_path, "source.pdf", "file")


@pytest.mark.skipif(os.name != "nt", reason="Windows directory share-denial semantics")
def test_windows_task_root_and_ancestor_rename_are_denied_during_read(tmp_path, monkeypatch):
    parent = tmp_path / "private-parent"
    root = parent / "task"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "source.pdf").write_bytes(PDF)
    original = files._read_descriptor
    blocked = []

    def rename_ancestors_while_handles_open(fd, limit):
        for path in (nested, root, parent):
            with pytest.raises(PermissionError):
                path.rename(path.with_name(path.name + "-replacement"))
            blocked.append(path.name)
        return original(fd, limit)

    monkeypatch.setattr(files, "_read_descriptor", rename_ancestors_while_handles_open)
    assert read_task_file(root, "nested/source.pdf", "file").content == PDF
    assert blocked == ["nested", "task", "private-parent"]
    assert (nested / "source.pdf").read_bytes() == PDF
