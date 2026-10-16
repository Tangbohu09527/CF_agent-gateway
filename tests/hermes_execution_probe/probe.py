"""Opt-in native 427d605 execution-session probe; no live credentials or models.

Run with existing pinned Hermes source/interpreter/dependencies and a new output
directory. Only the external model is synthetic; HTTP handlers, session DB,
Agent and Gateway HermesClient are unmodified.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import secrets
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMMIT = "427d605d4985c2d49a795493ef6c5d66413057d1"
MODEL = "cf-approved-execution-model"
FALLBACK = "cf-fallback-execution-model"
PUBLIC_MODEL_KEY = "public-isolated-model-protocol-test-key"
RUNTIME = {
    "runtime_model": MODEL,
    "runtime_provider": "custom",
    "runtime_model_options": {"reasoning": {"enabled": False}},
}
REPO = Path(__file__).resolve().parents[2]


def existing_probe():
    path = REPO / "tests/hermes_return_probe/probe.py"
    spec = importlib.util.spec_from_file_location("execution_probe_base", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True)


def verify_source(source, base):
    result = base.verify_source(source, COMMIT)
    for name in (
        "hermes_state_sessions.py",
        "hermes_state_messages.py",
        "hermes_state_compression.py",
        "gateway/run_config_loaders.py",
        "hermes_cli/fallback_config.py",
    ):
        expected = subprocess.check_output(["git", "-C", str(source), "show", COMMIT + ":" + name])
        raw = (source / name).read_bytes()
        if raw != expected:
            raise AssertionError("Official source identity differs: " + name)
        result[name] = hashlib.sha256(raw).hexdigest()
    return result


@contextmanager
def local_model():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = body.get("messages", [])
            latest = next(
                (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
            )
            calls.append(
                {
                    "model": body.get("model"),
                    "reasoning_effort": body.get("reasoning_effort"),
                    "latest": latest,
                    "messages": messages,
                }
            )
            if body.get("model") == MODEL and "FAIL_PRIMARY" in str(latest):
                data = json.dumps(
                    {
                        "error": {
                            "message": "model not found: synthetic primary",
                            "type": "invalid_request_error",
                            "code": "model_not_found",
                        }
                    }
                ).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            message = {"role": "assistant", "content": "synthetic result " + str(latest)}
            envelope = {
                "id": "chatcmpl-isolated-execution",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in ((message, None), ({}, "stop")):
                    chunk = {k: v for k, v in envelope.items() if k not in {"choices", "usage"}}
                    chunk.update(
                        object="chat.completion.chunk",
                        choices=[{"index": 0, "delta": delta, "finish_reason": finish}],
                    )
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                data = json.dumps(envelope).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port) + "/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


async def child(args):
    sys.path.append(str(args.site_packages))
    payload = json.loads(sys.stdin.readline())
    case, source = Path(payload["case"]), Path(payload["source"])
    base = existing_probe()
    base._PAYLOAD = payload
    save(case / (payload["phase"] + "-stage-payload.json"), {"stage": "payload"})
    base.verify_source(source, COMMIT)
    sys.path[:0] = [str(source), str(REPO / "src")]
    import mimetypes

    mimetypes.knownfiles = []
    mimetypes.inited = True
    mimetypes._db = mimetypes.MimeTypes(filenames=())
    mimetypes.init(files=[])
    config = json.loads((case / "home/config.yaml").read_text())
    base._install_child_audit(source, REPO, case, config["model"])
    from openai._base_client import get_platform

    get_platform()
    save(case / (payload["phase"] + "-stage-sdk.json"), {"stage": "sdk"})
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    save(case / (payload["phase"] + "-stage-imports.json"), {"stage": "imports"})
    token = set_secret_scope(
        {"OPENAI_API_KEY": PUBLIC_MODEL_KEY, "CUSTOM_API_KEY": PUBLIC_MODEL_KEY}
    )
    discover_plugins()
    manager = get_plugin_manager()
    assert not any(item.enabled for item in manager._plugins.values())
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "key": payload["service_key"],
                "model_name": "hermes-agent",
            },
        )
    )
    try:
        assert await adapter.connect()
        port = adapter._site._server.sockets[0].getsockname()[1]
        save(
            case / ("ready-" + payload["phase"] + ".json"),
            {"origin": "http://127.0.0.1:" + str(port)},
        )
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await adapter.disconnect()
        manager.unload()
        reset_secret_scope(token)


@contextmanager
def official_process(args, case, key, phase):
    base = existing_probe()
    command = [
        str(args.hermes_python),
        "-I",
        "-S",
        "-B",
        str(Path(__file__).resolve()),
        "--child",
        "--site-packages",
        str(args.site_packages),
    ]
    with (
        (case / (phase + ".stdout.txt")).open("x", encoding="utf-8") as stdout,
        (case / (phase + ".stderr.txt")).open("x", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            cwd=case,
            env=base.isolated_environment(case),
        )
        try:
            process.stdin.write(
                json.dumps(
                    {
                        "source": str(args.hermes_source),
                        "case": str(case),
                        "service_key": key,
                        "model_key": PUBLIC_MODEL_KEY,
                        "phase": phase,
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            ready = case / ("ready-" + phase + ".json")
            deadline = time.monotonic() + 60
            while not ready.exists():
                if process.poll() is not None:
                    raise RuntimeError("Isolated Hermes stopped before readiness")
                if time.monotonic() > deadline:
                    raise TimeoutError("Isolated Hermes readiness timed out")
                time.sleep(0.05)
            yield json.loads(ready.read_text())["origin"]
        finally:
            if process.poll() is None:
                process.stdin.write("stop\n")
                process.stdin.flush()
                try:
                    process.wait(20)
                except subprocess.TimeoutExpired:
                    process.terminate()  # This exact disposable child only.
                    process.wait(10)
            if process.returncode != 0:
                raise RuntimeError("Isolated Hermes process exit was not zero")


def run(args):
    import httpx

    sys.path.insert(0, str(REPO / "src"))
    from cf_agent_gateway.hermes.client import HermesClient
    from cf_agent_gateway.hermes.errors import HermesError

    base = existing_probe()
    case = args.output.resolve()
    case.mkdir(parents=True, exist_ok=False)
    for directory in (
        "home",
        "empty-bundled",
        "user",
        "user/AppData",
        "user/LocalAppData",
        "tmp",
        "work",
    ):
        (case / directory).mkdir(parents=True, exist_ok=True)
    source_blobs = verify_source(args.hermes_source.resolve(), base)
    key = secrets.token_hex(32)
    report = {
        "hermes_commit": COMMIT,
        "source_blobs": source_blobs,
        "official_http_and_session_db": True,
        "synthetic_model_only": True,
    }
    with local_model() as (model_url, calls):
        config = {
            "model": {
                "provider": "custom",
                "default": "cf-global-execution-model",
                "api_mode": "chat_completions",
                "context_length": 131072,
                "base_url": model_url,
            },
            "fallback_providers": [
                {
                    "provider": "custom",
                    "model": FALLBACK,
                    "base_url": model_url,
                    "api_key": PUBLIC_MODEL_KEY,
                }
            ],
            "platform_toolsets": {"api_server": []},
            "plugins": {"enabled": []},
            "agent": {"max_iterations": 2},
            "auxiliary": {"title_generation": {"enabled": False}},
            "memory": {"enabled": False},
            "skills": {"enabled": False},
            "compression": {"enabled": False},
            "checkpoints": {"enabled": False},
            "terminal": {"cwd": str(case / "work")},
        }
        save(case / "home/config.yaml", config)
        headers = {"Authorization": "Bearer " + key}
        proofs = {}

        def prepare(client, sid, parent=None):
            digests = []
            client.prepare_inbound_session(
                sid,
                parent_session_id=parent,
                **RUNTIME,
                allow_create=True,
                expected_history_digest=None,
                record_history=digests.append,
            )
            assert len(digests) == 1
            proofs[sid] = digests[0]

        def session(http, sid):
            reply = http.get("/api/sessions/" + sid)
            assert reply.status_code == 200
            return reply.json()["session"]

        def messages(http, sid):
            reply = http.get("/api/sessions/" + sid + "/messages?order=oldest&limit=500")
            assert reply.status_code == 200
            body = reply.json()
            assert body["session_id"] == sid
            return body["data"]

        def chat(client, sid, marker):
            before = len(calls)
            result = client.chat(marker, hermes_thread_id=sid, **RUNTIME)
            assert result.hermes_thread_id == sid
            assert "synthetic result " + marker in result.assistant_content
            client.verify_inbound_session_tip(sid)
            observed = [row for row in calls[before:] if row["model"] is not None]
            assert observed and all(row["model"] == MODEL for row in observed), [
                row["model"] for row in observed
            ]
            assert all(row["reasoning_effort"] == "none" for row in observed), [
                row["reasoning_effort"] for row in observed
            ]

        with official_process(args, case, key, "first") as origin:  # noqa: SIM117
            with (
                httpx.Client(
                    base_url=origin,
                    headers=headers,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=90,
                ) as http,
                HermesClient(origin, key, "hermes-agent", timeout=90) as client,
            ):
                assert httpx.get(origin + "/api/sessions", trust_env=False).status_code == 401
                prepare(client, "synthetic-legacy")
                chat(client, "synthetic-legacy", "LEGACY_ONLY_INSTRUCTION")
                legacy_messages = messages(http, "synthetic-legacy")
                legacy_meta = session(http, "synthetic-legacy")
                prepare(client, "synthetic-fresh")
                assert messages(http, "synthetic-fresh") == []
                assert messages(http, "synthetic-legacy") == legacy_messages
                assert session(http, "synthetic-legacy") == legacy_meta
                assert session(http, "synthetic-fresh")["parent_session_id"] is None
                report["fresh_create_preserved_legacy"] = True
                chat(client, "synthetic-fresh", "EXPLICIT_INSTRUCTION_AND_PDF")
                chat(client, "synthetic-fresh", "FOLLOWUP_TEXT")
                before_fork = messages(http, "synthetic-fresh")
                prepare(client, "synthetic-child-one", "synthetic-fresh")
                assert (
                    client._history_digest("synthetic-child-one") == proofs["synthetic-child-one"]
                )
                assert len(messages(http, "synthetic-child-one")) == len(before_fork)
                assert session(http, "synthetic-fresh")["end_reason"] == "branched"
                chat(client, "synthetic-child-one", "SECOND_ATTACHMENT_PNG")
                expected_restart_digest = client._history_digest("synthetic-child-one")
                report["explicit_runtime_text_and_first_fork"] = True
        with official_process(args, case, key, "restart") as origin:  # noqa: SIM117
            with (
                httpx.Client(
                    base_url=origin,
                    headers=headers,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=90,
                ) as http,
                HermesClient(origin, key, "hermes-agent", timeout=90) as client,
            ):
                assert client._history_digest("synthetic-child-one") == expected_restart_digest
                chat(client, "synthetic-child-one", "AFTER_RESTART_TEXT")
                prepare(client, "synthetic-child-two", "synthetic-child-one")
                chat(client, "synthetic-child-two", "THIRD_ATTACHMENT")
                report["restart_and_second_fork_preserved_history"] = True
                assert messages(http, "synthetic-legacy") == legacy_messages
                legacy_after = session(http, "synthetic-legacy")
                report["legacy_metadata_changed_fields"] = sorted(
                    k for k in legacy_meta if legacy_meta[k] != legacy_after.get(k)
                )
                for field in (
                    "id",
                    "source",
                    "model",
                    "parent_session_id",
                    "ended_at",
                    "end_reason",
                    "has_system_prompt",
                    "has_model_config",
                ):
                    assert legacy_after.get(field) == legacy_meta.get(field), field
                report["legacy_history_identity_lifecycle_preserved"] = True

                # A real failing primary + real configured local fallback. Observe;
                # never turn an unsafe compatibility result into a passing assertion.
                before = len(calls)
                try:
                    client.chat(
                        "FAIL_PRIMARY_OPENAI", hermes_thread_id="synthetic-child-two", **RUNTIME
                    )
                    accepted = True
                except HermesError:
                    accepted = False
                failure_calls = calls[before:]
                report["openai_provider_failure"] = {
                    "gateway_accepted_success": accepted,
                    "primary_calls": sum(row["model"] == MODEL for row in failure_calls),
                    "fallback_calls": sum(row["model"] == FALLBACK for row in failure_calls),
                }

                prepare(client, "synthetic-body-lock")
                before = len(calls)
                reply = http.post(
                    "/v1/chat/completions",
                    headers={"X-Hermes-Session-Id": "synthetic-body-lock"},
                    json={
                        "model": MODEL,
                        "provider": "custom",
                        "model_options": RUNTIME["runtime_model_options"],
                        "require_model_lock": True,
                        "messages": [{"role": "user", "content": "FAIL_PRIMARY_BODY_LOCK"}],
                    },
                )
                report["openai_body_lock"] = {
                    "http_status": reply.status_code,
                    "fallback_calls": sum(row["model"] == FALLBACK for row in calls[before:]),
                }

                prepare(client, "synthetic-native-lock")
                before = len(calls)
                reply = http.post(
                    "/api/sessions/synthetic-native-lock/chat",
                    json={
                        "message": "FAIL_PRIMARY_NATIVE_LOCK",
                        "model": MODEL,
                        "provider": "custom",
                        "model_options": RUNTIME["runtime_model_options"],
                        "require_model_lock": True,
                    },
                )
                body = reply.json()
                report["native_session_lock"] = {
                    "http_status": reply.status_code,
                    "fallback_calls": sum(row["model"] == FALLBACK for row in calls[before:]),
                    "top_level_failed": body.get("failed") is True,
                    "top_level_completed_false": body.get("completed") is False,
                    "hermes_failed": isinstance(body.get("hermes"), dict)
                    and body["hermes"].get("failed") is True,
                    "error_present": bool(body.get("error")),
                    "response_keys": sorted(body),
                }
        # Deployment boundary: the official OpenAI handler has no request-level
        # no-fallback switch. Test a protected execution profile with BOTH chains
        # empty; this config is only inside the disposable probe HOME.
        save(case / "config-before-no-fallback.json", config)
        config["fallback_providers"] = []
        config["fallback_model"] = []
        (case / "home/config.yaml").write_text(json.dumps(config), encoding="utf-8")
        with official_process(args, case, key, "without-fallback") as origin:  # noqa: SIM117
            with HermesClient(origin, key, "hermes-agent", timeout=90) as client:
                prepare(client, "synthetic-no-fallback")
                before = len(calls)
                caught = None
                try:
                    client.chat(
                        "FAIL_PRIMARY_WITHOUT_FALLBACK",
                        hermes_thread_id="synthetic-no-fallback",
                        **RUNTIME,
                    )
                except HermesError as error:
                    caught = type(error).__name__
                observed = calls[before:]
                report["empty_effective_fallback_chain"] = {
                    "gateway_error_class": caught,
                    "primary_calls": sum(row["model"] == MODEL for row in observed),
                    "fallback_calls": sum(row["model"] == FALLBACK for row in observed),
                }
                assert caught == "HermesResponseError", caught
                assert report["empty_effective_fallback_chain"]["primary_calls"] == 1
                assert report["empty_effective_fallback_chain"]["fallback_calls"] == 0
        report["normal_sequence_passed"] = True
        report["no_fallback_on_openai_provider_failure"] = (
            not report["openai_provider_failure"]["gateway_accepted_success"]
            and report["openai_provider_failure"]["fallback_calls"] == 0
        )
    matches = []
    for path in case.rglob("*"):
        if path.is_file() and key.encode() in path.read_bytes():
            matches.append(str(path.relative_to(case)))
    report["service_credential_scan_matches"] = matches
    assert not matches
    save(case / "report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "source_blobs"}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--hermes-python", type=Path)
    parser.add_argument("--site-packages", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        asyncio.run(child(args))
    else:
        if not all((args.hermes_source, args.hermes_python, args.output)):
            parser.error("source, interpreter and new output are required")
        run(args)


if __name__ == "__main__":
    main()
