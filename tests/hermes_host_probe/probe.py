"""Run real, pinned Hermes HTTP/session/agent APIs in a disposable profile.

The model and observer plugin are test doubles; Hermes itself is unmodified.
Explicit command only: this is not a pytest test which silently downloads code.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import platform
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMMIT = "4d55ca91656ac5f83e1506679b7f81e0238e5e16"
BLOBS = {
    "gateway/platforms/api_server.py": "5c4a2bf4ae7ac21191cdac72631aaf08509c2d88",
    "gateway/platforms/api_server_openai_routes.py": "6da8690fa55716652134890d0371da4c1d78619a",
    "hermes_cli/plugins.py": "553f80a73b5594371b0225df3bf8ecc9e0038a9d",
    "hermes_cli/middleware.py": "897e4afc07ba0d78ba928baf39a8573422b648be",
    "model_tools.py": "924cd94413b18c3a069228950939c4c43fa43b3b",
    "agent/tool_executor.py": "fa73ec6b625b50005259dfd1614d55a9b93b096e",
    "hermes_state_sessions.py": "cfd7811fdf5965b735d7407114d1bb4d1c4041c8",
    "hermes_state_messages.py": "4e7b96faa7b82f23c703a4b9f58c2dcf759b712e",
    "hermes_state_compression.py": "9f8415faba7e3a356bb3b427286e86b15813c33e",
    "gateway/platforms/api_server_runs.py": "3976ff02de219fa0c0de6bb7919fbb51e8cffecc",
}

OBSERVER = """import json, os
from pathlib import Path
def record(kind, context):
    keep = {k: context.get(k) for k in ("session_id", "task_id", "completed", "interrupted")}
    keep["kind"] = kind
    with Path(os.environ["CF_HERMES_PROBE_EVENTS"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(keep) + "\\n")
def execute(*, next_call, tool_name, args, **context):
    record("tool_execution", context)
    return next_call(args)
def end(**context):
    record("on_session_end", context)
def handler(args, **kwargs):
    return json.dumps({"ok": True, "synthetic_observer": True})
def register(ctx):
    ctx.register_middleware("tool_execution", execute)
    ctx.register_hook("on_session_end", end)
    ctx.register_tool("cf_probe_observe", "cf_probe", {
        "name": "cf_probe_observe", "description": "Synthetic test observer only",
        "parameters": {"type": "object", "properties": {}}}, handler)
"""


def verify(source: Path) -> None:
    if (source / ".git" / "HEAD").read_text().strip() != COMMIT:
        raise ValueError("Probe requires the exact detached official Hermes commit")
    for relative, expected in BLOBS.items():
        data = (source / relative).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual != expected:
            raise ValueError(f"Require unmodified Hermes {COMMIT}: {relative}")


class ModelStub(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.requests = []


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        messages = body.get("messages", [])
        latest = next((m.get("content", "") for m in reversed(messages) if m["role"] == "user"), "")
        if "probe-model-error" in str(latest):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":{"message":"synthetic model rejection","type":"invalid_request_error"}}'
            )
            return
        tools = [tool.get("function", {}).get("name") for tool in body.get("tools", [])]
        last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        called = any(m["role"] == "tool" for m in messages[last_user + 1 :])
        entry = "cf_probe_observe" if "cf_probe_observe" in tools else "tool_call"
        arguments = (
            {}
            if entry == "cf_probe_observe"
            else {"calls": [{"name": "cf_probe_observe", "arguments": {}}]}
        )
        if entry in tools and not called:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_" + secrets.token_hex(8),
                        "type": "function",
                        "function": {"name": entry, "arguments": json.dumps(arguments)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "synthetic response: " + str(latest)}
            finish = "stop"
        envelope = {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = {key: value for key, value in message.items() if key != "role"}
            if "tool_calls" in delta:
                delta["tool_calls"] = [
                    {"index": index, **call} for index, call in enumerate(delta["tool_calls"])
                ]
            chunk = {
                key: value for key, value in envelope.items() if key not in {"choices", "usage"}
            }
            chunk["object"] = "chat.completion.chunk"
            for piece, reason in (({"role": "assistant", **delta}, None), ({}, finish)):
                chunk["choices"] = [{"index": 0, "delta": piece, "finish_reason": reason}]
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        raw = json.dumps(envelope).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def guard(source: Path, sandbox: Path) -> list[str]:
    """Allow only isolated inputs/runtime files, loopback networking, no subprocesses."""
    roots = [source, sandbox, Path(sys.prefix), Path(sys.base_prefix), Path(__file__).parent]
    allowed = [os.path.normcase(os.path.abspath(path)) for path in roots]
    metadata = {
        os.path.normcase(os.path.abspath(path))
        for path in (
            "/proc/stat",
            "/proc/version",
            "/proc/1/cgroup",
            f"/proc/{os.getpid()}/stat",
            "/dev/null",
        )
    }
    rejected = []

    def audit(event, args):
        if event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
                rejected.append(event)
                raise RuntimeError("probe denies non-loopback network")
        elif event == "socket.getaddrinfo" and args[0] not in {
            None,
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            rejected.append(event)
            raise RuntimeError("probe denies external DNS")
        elif event in {"subprocess.Popen", "os.system"}:
            rejected.append(event)
            raise RuntimeError("probe denies subprocess")
        elif event in {"open", "os.listdir", "os.scandir"} and isinstance(args[0], (str, bytes)):
            path = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
            if path not in metadata and not any(
                path == root or path.startswith(root + os.sep) for root in allowed
            ):
                rejected.append(event + ": " + path)
                raise RuntimeError("probe denies access outside isolated roots: " + path)

    sys.addaudithook(audit)
    return rejected


def normalized(messages):
    return [
        {
            key: item.get(key)
            for key in ("role", "content", "tool_calls", "tool_call_id", "tool_name")
        }
        for item in messages
    ]


def initialize_builtin_mime() -> None:
    """Use only Python's built-in MIME table, without OS files or Windows registry.

    init(files=[]) alone still consults knownfiles and the Windows registry when
    its database is absent. Seed the stdlib database from MimeTypes' built-in
    table first. This changes only disposable test-process stdlib state, never
    the pinned Hermes implementation or the filesystem audit allow-list.
    """
    checking = True

    def no_mime_environment_reads(event, _args):
        if checking and (event == "open" or event.startswith("winreg.")):
            raise AssertionError("MIME bootstrap attempted environment file/registry access")

    sys.addaudithook(no_mime_environment_reads)
    try:
        mimetypes.knownfiles = []
        mimetypes.inited = True
        mimetypes._db = mimetypes.MimeTypes(filenames=())
        mimetypes.init(files=[])
        assert mimetypes.guess_type("synthetic.pdf") == ("application/pdf", None)
        assert mimetypes.guess_type("synthetic.jpg") == ("image/jpeg", None)
    finally:
        checking = False


def verify_outside_reads_denied(source: Path) -> None:
    # Opening is stopped by the audit hook before filesystem access, including
    # the exact Linux MIME file which exposed the original CI bootstrap failure.
    for path in (source.parent / "outside-isolated-roots", Path("/etc/mime.types")):
        try:
            path.read_bytes()
        except RuntimeError as error:
            assert str(error).startswith("probe denies access outside isolated roots:")
        else:
            raise AssertionError("Isolation audit allowed an external read")


async def child(source: Path):
    platform.system()  # Windows stdlib may query the OS before the no-process guard.
    sandbox = Path.cwd()
    home = Path(os.environ["HERMES_HOME"])
    model = ModelStub()
    threading.Thread(target=model.serve_forever, daemon=True).start()
    config = json.loads((home / "config.yaml").read_text(encoding="utf-8"))
    config["model"] = {
        "provider": "custom",
        "default": "cf-global-model",
        "api_mode": "chat_completions",
        "context_length": 131072,
        "base_url": f"http://127.0.0.1:{model.server_port}/v1",
        "api_key": "public-isolated-model-stub-not-a-credential",
    }
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    rejected = guard(source, sandbox)
    initialize_builtin_mime()
    verify_outside_reads_denied(source)
    sys.path.insert(0, str(source))
    from aiohttp import ClientSession, ClientTimeout, web
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins()
    manager = get_plugin_manager()
    assert manager._plugins["cf-probe"].error is None, (
        manager._plugins["cf-probe"].error,
        rejected,
    )
    key = secrets.token_hex(32)
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "key": key, "model_name": "hermes-agent"},
        )
    )
    relay_runner = None
    report = {
        "hermes_commit": COMMIT,
        "official_http": True,
        "official_agent": True,
        "model": "loopback synthetic stub",
        "plugin": "synthetic observer, not FileBridge",
        "mime_source": "stdlib built-in table; no OS files or registry",
        "outside_file_audit_self_check": True,
    }
    try:
        assert await adapter.connect()
        port = adapter._site._server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        headers = {"Authorization": "Bearer " + key}
        async with ClientSession(timeout=ClientTimeout(total=60)) as client:

            async def request(method, path, body=None, expected=200, extra=None):
                async with client.request(
                    method, origin + path, json=body, headers={**headers, **(extra or {})}
                ) as response:
                    result = await response.json()
                    assert response.status == expected, (path, response.status, result)
                    return result, dict(response.headers)

            for path in ("/api/sessions", "/api/sessions/missing/messages"):
                async with client.get(origin + path) as response:
                    assert response.status == 401
            await request("GET", "/api/sessions/missing", expected=404)
            report["authenticated_api_required"] = True
            lock = {
                "model": "cf-locked-model",
                "provider": "custom",
                "require_model_lock": True,
                "model_options": {"service_tier": "priority", "reasoning": {"enabled": False}},
            }
            parent = "cf-probe-parent"
            await request(
                "POST",
                "/api/sessions",
                {
                    "id": parent,
                    "system_prompt": "PUBLIC-PROFILE-MARKER",
                    "title": "parent-synthetic",
                    **lock,
                },
                201,
            )
            await request("POST", "/api/sessions", {"id": parent}, 409)
            for label in ("history-one", "history-two"):
                before = len(model.requests)
                await request("POST", f"/api/sessions/{parent}/chat", {"message": label})
                actual = model.requests[before:]
                assert any(item.get("model") == "cf-locked-model" for item in actual), actual
            original, _ = await request(
                "GET", f"/api/sessions/{parent}/messages?limit=500&order=oldest"
            )
            assert original["session_id"] == parent
            assert all(label in json.dumps(original) for label in ("history-one", "history-two"))
            report["native_session_chat_uses_persisted_lock"] = True
            fork_id = "cf-probe-claim-child"
            fork, _ = await request("POST", f"/api/sessions/{parent}/fork", {"id": fork_id}, 201)
            assert fork["session"]["parent_session_id"] == parent
            copied, _ = await request(
                "GET", f"/api/sessions/{fork_id}/messages?limit=500&order=oldest"
            )
            assert copied["session_id"] == fork_id
            assert normalized(copied["data"]) == normalized(original["data"])
            parent_row, _ = await request("GET", f"/api/sessions/{parent}")
            assert parent_row["session"]["end_reason"] == "branched"
            assert fork["session"]["has_system_prompt"]
            db = await adapter._ensure_session_db_async()
            raw_parent = db.get_session(parent)
            raw_child = db.get_session(fork_id)
            assert raw_child["system_prompt"] == raw_parent["system_prompt"]
            assert json.loads(raw_child["model_config"]) == {"_branched_from": parent}
            report["fork_preserves_history_and_system_prompt"] = True
            report["fork_drops_provider_model_options_lock"] = True
            report["fork_ends_parent_as_branched"] = True
            report["http_session_projection_hides_model_config_and_prompt"] = True

            # Public /model can restore the stored lock, but the unchanged OpenAI route
            # does NOT consume it. Observe the actual upstream model request as evidence.
            await request("POST", f"/api/sessions/{fork_id}/model", lock)
            before = len(model.requests)
            answer, echoed = await request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "child-openai"}],
                },
                extra={"X-Hermes-Session-Id": fork_id},
            )
            assert not answer.get("hermes", {}).get("failed"), answer
            current = [
                item
                for item in model.requests[before:]
                if "cf_probe_observe" in json.dumps(item.get("tools", []))
            ]
            assert current and all(item["model"] == "cf-global-model" for item in current), current
            assert all("history-one" in json.dumps(item["messages"]) for item in current)
            assert echoed["X-Hermes-Session-Id"] == fork_id
            report["openai_route_ignores_persisted_model_lock"] = True
            before = len(model.requests)
            await request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "cf-locked-model",
                    "provider": "custom",
                    "model_options": lock["model_options"],
                    "messages": [{"role": "user", "content": "explicit-profile-options"}],
                },
                extra={"X-Hermes-Session-Id": fork_id},
            )
            current = [
                item
                for item in model.requests[before:]
                if "cf_probe_observe" in json.dumps(item.get("tools", []))
            ]
            assert current and all(item["model"] == "cf-locked-model" for item in current), current
            assert all(item.get("reasoning_effort") == "none" for item in current), current
            report["explicit_approved_openai_runtime_options_work"] = True

            # A branch is deliberately NOT the parent's compression continuation tip.
            old_messages, _ = await request("GET", f"/api/sessions/{parent}/messages?limit=500")
            assert old_messages["session_id"] == parent
            report["branch_does_not_redirect_old_parent_to_child"] = True
            # Use the official atomic SessionDB compression primitive for the
            # fixture; automatic LLM summarization is not under test. The resolver,
            # public HTTP routes, agent and middleware remain real and unchanged.
            compression_root = "cf-probe-compression-root"
            tip = "cf-probe-compression-tip"
            await request("POST", "/api/sessions", {"id": compression_root, **lock}, 201)
            await request(
                "POST", f"/api/sessions/{compression_root}/chat", {"message": "before-compression"}
            )
            assert db.try_acquire_compression_lock(compression_root, "synthetic-probe-lease")
            db.publish_compression_child(
                parent_session_id=compression_root,
                child_session_id=tip,
                source="api_server",
                messages=db.get_messages(compression_root),
                model="cf-locked-model",
                model_config=json.loads(db.get_session(compression_root)["model_config"]),
                compression_lock_holder="synthetic-probe-lease",
            )
            resolved, _ = await request("GET", f"/api/sessions/{compression_root}/messages")
            assert resolved["session_id"] == tip
            _, old_echo = await request(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "cf-locked-model",
                    "provider": "custom",
                    "messages": [{"role": "user", "content": "after-compression"}],
                },
                extra={"X-Hermes-Session-Id": compression_root},
            )
            assert old_echo["X-Hermes-Session-Id"] == compression_root
            assert "after-compression" in json.dumps(db.get_messages(tip))
            assert "after-compression" not in json.dumps(db.get_messages(compression_root))
            report["compression_tip_fixture"] = "official atomic SessionDB publication"
            report["old_header_executes_at_tip_but_response_echoes_old_id"] = True

            # Create contention uses the real SQLite transaction, not a mocked
            # response. One deterministic ID is created exactly once.
            outcomes = await asyncio.gather(
                *[
                    client.post(
                        origin + "/api/sessions",
                        json={"id": "cf-probe-concurrent-create"},
                        headers=headers,
                    )
                    for _ in range(2)
                ]
            )
            assert sorted(item.status for item in outcomes) == [201, 409]
            for item in outcomes:
                await item.read()
                item.release()
            report["concurrent_create_same_id_201_409"] = True

            idem_body = {"messages": [{"role": "user", "content": "idempotent-turn"}]}
            idem_headers = {
                "X-Hermes-Session-Id": tip,
                "Idempotency-Key": "cf-public-probe-fixed-key",
            }
            first, _ = await request("POST", "/v1/chat/completions", idem_body, extra=idem_headers)
            request_count = len(model.requests)
            second, _ = await request("POST", "/v1/chat/completions", idem_body, extra=idem_headers)
            assert first["choices"] == second["choices"]
            assert len(model.requests) == request_count
            report["openai_identical_request_idempotency_preserved"] = True

            # Drop the fork response at a loopback transport relay, after the real
            # upstream API committed it. Retry only the SAME preallocated child ID.
            async def delayed_response(incoming):
                async with client.post(
                    origin + incoming.path, json=await incoming.json(), headers=headers
                ) as upstream:
                    raw = await upstream.read()
                    status = upstream.status
                await asyncio.sleep(0.3)
                return web.Response(body=raw, status=status, content_type="application/json")

            relay = web.Application()
            relay.router.add_post("/api/sessions/{sid}/fork", delayed_response)
            relay_runner = web.AppRunner(relay)
            await relay_runner.setup()
            relay_site = web.TCPSite(relay_runner, "127.0.0.1", 0)
            await relay_site.start()
            relay_port = relay_site._server.sockets[0].getsockname()[1]
            recovering = "cf-probe-fixed-lost-response"
            before_copy, _ = await request(
                "GET", f"/api/sessions/{fork_id}/messages?limit=500&order=oldest"
            )
            try:
                await client.post(
                    f"http://127.0.0.1:{relay_port}/api/sessions/{fork_id}/fork",
                    json={"id": recovering},
                    timeout=ClientTimeout(total=0.1),
                )
                raise AssertionError("response-loss fault did not time out")
            except TimeoutError:
                pass
            await asyncio.sleep(0.35)
            recovered, _ = await request("GET", f"/api/sessions/{recovering}")
            assert recovered["session"]["parent_session_id"] == fork_id
            recovered_copy, _ = await request(
                "GET", f"/api/sessions/{recovering}/messages?limit=500&order=oldest"
            )
            assert normalized(recovered_copy["data"]) == normalized(before_copy["data"])
            await request("POST", f"/api/sessions/{fork_id}/fork", {"id": recovering}, 409)
            report["lost_fork_response_same_id_reconciliation"] = True
            # Official fork can return an error AFTER child, parent and messages
            # were changed: duplicate title fails the final nontransactional step.
            partial = "cf-probe-partial-fork"
            await request(
                "POST",
                f"/api/sessions/{recovering}/fork",
                {"id": partial, "title": "parent-synthetic"},
                400,
            )
            persisted, _ = await request("GET", f"/api/sessions/{partial}")
            assert persisted["session"]["parent_session_id"] == recovering
            await request("POST", f"/api/sessions/{recovering}/fork", {"id": partial}, 409)
            report["fork_error_can_leave_durable_child"] = True

            failure, _ = await request(
                "POST",
                "/v1/chat/completions",
                {"messages": [{"role": "user", "content": "probe-model-error"}]},
                extra={"X-Hermes-Session-Id": "cf-probe-model-error"},
            )
            assert failure["hermes"]["failed"] is True
            events_path = Path(os.environ["CF_HERMES_PROBE_EVENTS"])
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            assert not any(
                item["kind"] == "on_session_end" and item["session_id"] == "cf-probe-model-error"
                for item in events
            )
            assert any(
                item["kind"] == "tool_execution"
                and item["session_id"] == fork_id
                and item["task_id"] == fork_id
                for item in events
            )
            assert any(
                item["kind"] == "tool_execution"
                and item["session_id"] == tip
                and item["task_id"] == tip
                for item in events
            )
            assert key not in json.dumps(model.requests)
            assert key not in events_path.read_text()
            for material in home.rglob("*"):
                if material.is_file():
                    assert key.encode() not in material.read_bytes(), (
                        "API credential persisted in synthetic profile"
                    )
            report["http_200_model_failure_without_end_hook"] = True
            report["real_middleware_session_equals_task"] = True
            report["api_credential_absent_from_model_history_and_profile_logs"] = True
            report["copied_history_messages"] = len(original["data"])
            report["blocked_external_operations"] = sorted(set(rejected))
            report["ok"] = True
            print(json.dumps(report))
    finally:
        if relay_runner:
            await relay_runner.cleanup()
        await adapter.disconnect()
        manager.unload()
        model.shutdown()
        model.server_close()


def run(source: Path):
    verify(source)
    with tempfile.TemporaryDirectory(prefix="run-", dir=source.parent) as temporary:
        sandbox = Path(temporary).resolve()
        home = sandbox / "profile"
        observer = home / "plugins" / "cf-probe"
        observer.mkdir(parents=True)
        (observer / "plugin.yaml").write_text(
            'name: cf-probe\nversion: "1.0.0"\nkind: standalone\n', encoding="utf-8"
        )
        (observer / "__init__.py").write_text(OBSERVER, encoding="utf-8")
        bundled = sandbox / "empty-bundled"
        bundled.mkdir()
        config = {
            "plugins": {"enabled": ["cf-probe"]},
            "platform_toolsets": {"api_server": ["cf_probe"]},
            "agent": {"max_iterations": 4},
            "memory": {"enabled": False},
            "skills": {"enabled": False},
            "compression": {"enabled": False},
        }
        (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
        }
        env.update(
            {
                "HERMES_HOME": str(home),
                "HERMES_BUNDLED_PLUGINS": str(bundled),
                "TEMP": str(sandbox),
                "TMP": str(sandbox),
                "TMPDIR": str(sandbox),
                "HERMES_ENABLE_PROJECT_PLUGINS": "false",
                "USERPROFILE": str(sandbox / "user"),
                "HOME": str(sandbox / "user"),
                "APPDATA": str(sandbox / "user" / "AppData"),
                "LOCALAPPDATA": str(sandbox / "user" / "LocalAppData"),
                "CF_HERMES_PROBE_EVENTS": str(sandbox / "events.jsonl"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                "--hermes-source",
                str(source),
                "--child",
            ],
            cwd=sandbox,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report.get("ok") is True
        print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", required=True, type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    source = options.hermes_source.resolve(strict=True)
    verify(source)
    if options.child:
        asyncio.run(child(source))
    else:
        run(source)
