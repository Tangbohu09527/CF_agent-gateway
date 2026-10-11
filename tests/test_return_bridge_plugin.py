"""Native aiohttp seam regressions; official Agent execution is a separate probe."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from test_hermes_tls import _test_certificates

from cf_agent_gateway.hermes.return_bridge import plugin
from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError
from cf_agent_gateway.hermes.return_bridge.protocol import (
    RETURN_ACK_HEADER,
    RETURN_AUTH_HEADER,
    RETURN_URL_HEADER,
    TOOL_NAME,
)
from cf_agent_gateway.hermes.tls import verified_ssl_context

KEY = "synthetic-official-platform-test-key"


def capability(session="same-session", dispatch_id=1):
    claims = {
        "schema": "cf-artifact-return/v1",
        "session_id": session,
        "dispatch_id": dispatch_id,
        "claim": "a" * 64,
        "expires": int(time.time()) + 300,
        "max_bytes": 1048576,
        "max_artifacts": 4,
        "profile": "approved-test",
        "revision": 1,
    }
    raw = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "Bearer " + raw + "." + "b" * 64


def headers(token, session="same-session", dispatch_id=1):
    return {
        "Authorization": "Bearer " + KEY,
        "X-Hermes-Session-Id": session,
        RETURN_AUTH_HEADER: token,
        RETURN_URL_HEADER: f"https://localhost:443/internal/hermes/returns/{dispatch_id}",
    }


@asynccontextmanager
async def bridge(tmp_path, *, deadline=5, handler_mode="agent", key=KEY):
    ca, cert, private = _test_certificates(tmp_path)
    root = tmp_path / "work"
    root.mkdir(mode=0o700, exist_ok=True)
    settings = plugin.BridgeSettings(
        gateway_origin="https://localhost:443",
        gateway_ca_file=str(ca),
        work_root=str(root),
        profile_reference="approved-test",
        tls_host="127.0.0.1",
        tls_port=0,
        tls_cert_file=str(cert),
        tls_key_file=str(private),
        request_timeout_seconds=deadline,
    )
    scopes, events, calls, late_results = [], [], [], []
    adapter = SimpleNamespace(_host="127.0.0.1", _runner=None)
    adapter._expected_api_key = lambda: key
    adapter._check_auth = lambda request: (
        None
        if request.headers.get("Authorization") == "Bearer " + key
        else web.json_response({"error": "unauthorized"}, status=401)
    )
    app = web.Application()

    async def handler(request):
        calls.append(await request.json())
        if handler_mode == "cached":
            return web.json_response({"text": "cached old completion"})
        if RETURN_AUTH_HEADER not in request.headers:
            assert plugin._execution.get() is None
            return web.json_response({"text": "ordinary"})

        def execute():
            current = plugin._execution.get()
            assert current is not None
            session = request.headers["X-Hermes-Session-Id"]
            plugin.pre_llm_call(
                session_id=session, task_id=session, turn_id=current.scope.request_id
            )
            if handler_mode == "late":
                time.sleep(1.3)
                try:
                    current.scope.check_active()
                except ReturnBridgeError:
                    late_results.append("closed")
                else:
                    late_results.append("unexpected-active")
            return {"text": "read-only result", "request": current.scope.request_id}

        result = await asyncio.get_running_loop().run_in_executor(None, execute)
        return web.json_response(result)

    app.router.add_post("/v1/chat/completions", handler)
    state = plugin.configure_app(
        app,
        adapter,
        settings,
        prepare_scope=scopes.append,
        observer=lambda name, payload: events.append((name, payload)),
    )
    adapter._runner = web.AppRunner(app)
    await adapter._runner.setup()
    async with asyncio.timeout(6):
        while not state["ready"]:
            await asyncio.sleep(0.01)
    port = state["site"]._server.sockets[0].getsockname()[1]
    async with httpx.AsyncClient(
        base_url=f"https://localhost:{port}",
        verify=verified_ssl_context(str(ca)),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            yield client, scopes, events, calls, late_results, root
        finally:
            await adapter._runner.cleanup()


def test_native_http_read_only_scope_ack_and_no_upload(tmp_path):
    async def run():
        async with bridge(tmp_path) as (client, scopes, events, calls, _, _root):
            token = capability()
            response = await client.post("/v1/chat/completions", headers=headers(token), json={})
            assert response.status_code == 200
            assert response.headers[RETURN_ACK_HEADER] == hashlib.sha256(token.encode()).hexdigest()
            assert response.json()["text"] == "read-only result"
            assert len(scopes) == len(calls) == 1
            assert list(scopes[0].work_root.iterdir()) == []
            with pytest.raises(ReturnBridgeError):
                scopes[0].check_active()
            assert [name for name, _ in events] == [
                "request_scope",
                "agent_bound",
                "completion_ack",
                "request_closed",
            ]
            assert token not in json.dumps(events)

    asyncio.run(run())


def test_no_context_keeps_request_and_response_unchanged(tmp_path):
    async def run():
        async with bridge(tmp_path) as (client, scopes, _events, calls, _, root):
            body = {"messages": [{"role": "user", "content": "read only"}]}
            response = await client.post("/v1/chat/completions", json=body)
            assert response.status_code == 200 and response.json() == {"text": "ordinary"}
            assert RETURN_ACK_HEADER not in response.headers and scopes == []
            assert calls == [body] and list(root.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["wrong-key", "missing-key", "wrong-session", "wrong-profile"])
def test_bad_origin_or_context_never_starts_agent(tmp_path, kind):
    async def run():
        async with bridge(tmp_path, key="" if kind == "missing-key" else KEY) as data:
            client, scopes, _, calls, _, _root = data
            token = capability()
            incoming = headers(token)
            if kind == "wrong-key":
                incoming["Authorization"] = "Bearer wrong-synthetic-key"
            elif kind == "wrong-session":
                incoming["X-Hermes-Session-Id"] = "old-session"
            elif kind == "wrong-profile":
                payload = json.loads(
                    base64.urlsafe_b64decode(token.split()[1].split(".")[0] + "==")
                )
                payload["profile"] = "other-profile"
                encoded = (
                    base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
                )
                incoming[RETURN_AUTH_HEADER] = "Bearer " + encoded + "." + "b" * 64
            response = await client.post("/v1/chat/completions", headers=incoming, json={})
            assert response.status_code in {401, 403}
            assert RETURN_ACK_HEADER not in response.headers and calls == scopes == []

    asyncio.run(run())


def test_same_session_concurrent_requests_have_distinct_revocable_scope(tmp_path):
    async def run():
        async with bridge(tmp_path) as (client, scopes, _events, _calls, _, _root):
            tokens = [capability(dispatch_id=i) for i in (1, 2)]
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/chat/completions", headers=headers(token, dispatch_id=i), json={}
                    )
                    for token, i in zip(tokens, (1, 2), strict=True)
                )
            )
            assert [r.status_code for r in responses] == [200, 200]
            assert len({r.json()["request"] for r in responses}) == 2
            assert len({s.work_root for s in scopes}) == 2
            for response, token in zip(responses, tokens, strict=True):
                assert (
                    response.headers[RETURN_ACK_HEADER]
                    == hashlib.sha256(token.encode()).hexdigest()
                )
            assert all(scope._cancelled.is_set() for scope in scopes)

    asyncio.run(run())


def test_spent_capability_cannot_be_reopened_after_restart(tmp_path):
    token = capability()

    async def run():
        async with bridge(tmp_path) as data:
            response = await data[0].post("/v1/chat/completions", headers=headers(token), json={})
            assert response.status_code == 200
        async with bridge(tmp_path) as data:
            response = await data[0].post("/v1/chat/completions", headers=headers(token), json={})
            assert response.status_code == 409 and RETURN_ACK_HEADER not in response.headers
            assert data[1] == data[3] == []

    asyncio.run(run())
    with sqlite3.connect(tmp_path / "work/.return-claims.sqlite3") as db:
        digest, _, _ = db.execute("SELECT * FROM spent").fetchone()
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert token.encode() not in (tmp_path / "work/.return-claims.sqlite3").read_bytes()


def test_cached_completion_without_this_agent_turn_has_no_ack(tmp_path):
    async def run():
        async with bridge(tmp_path, handler_mode="cached") as data:
            response = await data[0].post(
                "/v1/chat/completions", headers=headers(capability()), json={}
            )
            assert response.status_code == 409 and RETURN_ACK_HEADER not in response.headers

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["deadline", "disconnect"])
def test_cancelled_or_timed_out_http_revokes_late_executor_work(tmp_path, stop):
    async def run():
        async with bridge(tmp_path, deadline=1, handler_mode="late") as data:
            client, scopes, _events, _calls, late, _root = data
            pending = asyncio.create_task(
                client.post("/v1/chat/completions", headers=headers(capability()), json={})
            )
            if stop == "disconnect":
                while not scopes:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.1)
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            else:
                response = await pending
                assert response.status_code == 504 and RETURN_ACK_HEADER not in response.headers
            await asyncio.sleep(1.5)
            assert late == ["closed"] and scopes[0]._cancelled.is_set()

    asyncio.run(run())


def test_tool_requires_current_actual_turn_and_rejects_extra_parameters():
    assert (
        json.loads(plugin.handle_return({"file_ref": "x.pdf", "kind": "file"}))["success"] is False
    )
    result = plugin.tool_execution(tool_name=TOOL_NAME, args={}, next_call=lambda _: "bypassed")
    assert result != "bypassed" and json.loads(result)["success"] is False


def test_unknown_custom_executor_is_not_replaced(tmp_path):
    class CustomExecutor(plugin.ThreadPoolExecutor):
        pass

    async def run():
        executor = CustomExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(executor)
        with pytest.raises(ValueError, match="custom executor"):
            async with bridge(tmp_path):
                pytest.fail("custom executor must require explicit review")
        assert asyncio.get_running_loop()._default_executor is executor

    asyncio.run(run())


def test_host_cleanup_revokes_every_scope_before_waiting_for_any_drain(tmp_path):
    """A slow first upload must not leave other requests authorized at shutdown."""
    work = tmp_path / "private-work"
    work.mkdir()
    ca, cert, key = _test_certificates(tmp_path)
    settings = plugin.BridgeSettings(
        gateway_origin="https://localhost:443",
        gateway_ca_file=str(ca),
        work_root=str(work),
        profile_reference="approved-test",
        tls_host="127.0.0.1",
        tls_port=0,
        tls_cert_file=str(cert),
        tls_key_file=str(key),
    )
    entered, release = threading.Event(), threading.Event()
    observations = []
    scopes = []

    class Scope:
        def __init__(self, *, blocked):
            self.blocked = blocked
            self.revoked = threading.Event()

        def revoke(self):
            self.revoked.set()

        def close(self):
            # Observe all scopes at drain entry, regardless of unordered set order.
            observations.append(all(scope.revoked.is_set() for scope in scopes))
            if self.blocked:
                entered.set()
                assert release.wait(5)

    scopes.extend((Scope(blocked=True), Scope(blocked=False)))

    async def run():
        app = web.Application()
        adapter = SimpleNamespace(_host="127.0.0.1", _runner=None)
        state = plugin.configure_app(app, adapter, settings)
        state["scopes"].update(scopes)
        shutdown = asyncio.create_task(app.on_cleanup[-1](app))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert all(scope.revoked.is_set() for scope in scopes)
            assert not shutdown.done()
            assert observations and all(observations)
        finally:
            release.set()
            await shutdown
        assert observations == [True, True]

    asyncio.run(run())


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_ledger_and_sidecar_hard_links_rejected_before_sqlite_writes(tmp_path, suffix):
    root = tmp_path / "work"
    root.mkdir(mode=0o700)
    original = tmp_path / "unrelated-existing-file"
    evidence = b"unrelated file must never be modified by SQLite"
    original.write_bytes(evidence)
    os.link(original, root / (".return-claims.sqlite3" + suffix))
    with pytest.raises(ReturnBridgeError, match="return_ledger_unavailable"):
        plugin._consume_capability(root, capability(), "never-started", int(time.time()) + 300)
    assert original.read_bytes() == evidence


def _directory_link(target, link):
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_linked_root_ancestor_rejected_at_startup_and_consumption(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "work").mkdir(mode=0o700)
    link = tmp_path / "linked-ancestor"
    _directory_link(actual, link)
    root = link / "work"
    with pytest.raises(ReturnBridgeError, match="return_ledger_unavailable"):
        plugin._consume_capability(root, capability(), "never-started", int(time.time()) + 300)
    assert list((actual / "work").iterdir()) == []
    settings = plugin.BridgeSettings(
        gateway_origin="https://localhost:443",
        work_root=str(root),
        profile_reference="approved-test",
        tls_host="127.0.0.1",
        tls_port=0,
        tls_cert_file="unused-certificate-reference",
        tls_key_file="unused-key-reference",
    )
    with pytest.raises(ValueError, match="without links"):
        plugin.configure_app(web.Application(), SimpleNamespace(_host="127.0.0.1"), settings)


def test_ledger_hard_link_after_startup_stops_http_before_agent(tmp_path):
    async def run():
        async with bridge(tmp_path) as data:
            client, scopes, _events, calls, _, root = data
            original = tmp_path / "existing-file"
            evidence = b"do not alter existing file"
            original.write_bytes(evidence)
            os.link(original, root / ".return-claims.sqlite3")
            response = await client.post(
                "/v1/chat/completions", headers=headers(capability()), json={}
            )
            assert response.status_code == 403
            assert RETURN_ACK_HEADER not in response.headers and calls == scopes == []
            assert original.read_bytes() == evidence

    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows no-delete handle semantics")
def test_windows_ledger_guard_pins_file_and_root_against_replacement(tmp_path):
    root = tmp_path / "work"
    root.mkdir(mode=0o700)
    with plugin._ledger_guard(root) as ledger:
        with pytest.raises(OSError):
            ledger.rename(root / "renamed-ledger")
        with pytest.raises(OSError):
            root.rename(tmp_path / "renamed-root")
    assert ledger.is_file() and root.is_dir()
