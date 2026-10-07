"""Explicit official-Hermes HTTPS probe; never run models during pytest collection.

The default model and WeChat receiver are loopback protocol peers. Hermes, its
Agent tool loop, the return plugin, and Gateway persistence/delivery are real.
No file downloader, reader, vision implementation, or live service is used here.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, urlsplit

COMMIT = "0f4a98f87c17007b81500239d0bd5b9574027b73"
BLOBS = {
    "gateway/platforms/api_server.py": "5bd5e91fa2296f3c150a87845c047a6a69933c71",
    "gateway/platforms/api_server_openai_routes.py": "c1cb7f55d3f3fc960063cdc1194347386180331c",
    "gateway/platforms/api_server_runs.py": "8b662cacbdf4a541c7b7b0c0eb8c953d0180001e",
    "gateway/platforms/tcp_site.py": "77c9f69850093e55138a458bde1c5ac4e6aa1a86",
    "gateway/platforms/base.py": "6c544cf9e68db916219af3340de513283a73fe46",
    "hermes_cli/plugins.py": "bba82a3b4b0a394f69fbb2a54cc5a94f2a1303e5",
    "hermes_cli/middleware.py": "1c6ed2ae249a3b0539189c2fbd62916fadef65f1",
    "model_tools.py": "9a068d5a1b42f1fa7bd7e17d9e5d456b40a56713",
    "agent/tool_executor.py": "53efc4e1c21c11037bfb93a5ca73403d3ef0bb12",
    "agent/turn_context.py": "452a2f519f9209bb7cc4077ed77135991c6cc8cb",
}
APPROVED = {
    "file": (
        "CF-NATIVE-PDF-20261006-A1.pdf",
        79083,
        "ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44",
    ),
    "image": (
        "CF-NATIVE-IMAGE-20261006-B1.png",
        47740,
        "102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4",
    ),
}
PLUGIN = """from cf_agent_gateway.hermes.return_bridge.plugin import setup
from cf_return_probe_child import prepare_scope, record

def register(ctx):
    setup(ctx, prepare_scope=prepare_scope, observer=record)
    ctx.register_hook("post_tool_call", lambda **kw: record("official_tool_result", {
        key: kw.get(key) for key in
        ("session_id", "task_id", "turn_id", "tool_call_id", "tool_name", "args", "result")
    }))
"""


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_new(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)


def verify_source(source: Path) -> dict:
    """Verify only the explicitly named install's immutable source, no other worktree."""
    result = {}
    for name, expected in BLOBS.items():
        raw = (source / name).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if actual != expected:
            raise ValueError(f"Official source does not match {COMMIT}: {name}")
        result[name] = actual
    return result


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def synthetic_model(filename: str, kind: str):
    """Deterministic external model peer, not a replacement Hermes/tool executor."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            tools = [t.get("function", {}).get("name") for t in body.get("tools", [])]
            messages = body.get("messages", [])
            done = any(m.get("role") == "tool" for m in messages)
            tool = "cf_return_current_chat"
            if not tools:
                message = {"role": "assistant", "content": "Isolated return test"}
                reason = "stop"
            elif not done and kind != "text":
                assert tool in tools or "tool_call" in tools, tools
                args = {"file_ref": filename, "kind": kind, "filename": filename}
                if tool not in tools:
                    args = {"calls": [{"name": tool, "arguments": args}]}
                    tool = "tool_call"
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-" + secrets.token_hex(8),
                            "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(args)},
                        }
                    ],
                }
                reason = "tool_calls"
            else:
                message = {
                    "role": "assistant",
                    "content": (
                        "已交给 Gateway，等待任务完成及投递。" if kind != "text" else "仅返回文字。"
                    ),
                }
                reason = "stop"
            envelope = {
                "id": "chatcmpl-synthetic-return",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = {k: v for k, v in message.items() if k != "role"}
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
                for part, finish in (({"role": "assistant", **delta}, None), ({}, reason)):
                    chunk = {k: v for k, v in envelope.items() if k not in {"choices", "usage"}}
                    chunk["object"] = "chat.completion.chunk"
                    chunk["choices"] = [{"index": 0, "delta": part, "finish_reason": finish}]
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                data = json.dumps(envelope).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            {
                "provider": "custom",
                "default": "cf-return-synthetic-model",
                "api_mode": "chat_completions",
                "context_length": 131072,
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "api_key": "public-loopback-model-test-key",
            },
            calls,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def gateway_https(settings, certificates, observations, lease_seconds=None):
    import uvicorn

    from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
    from cf_agent_gateway.gateway.app import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"https://localhost:{sock.getsockname()[1]}"
    settings = replace(
        settings,
        artifact_return=ArtifactReturnSettings(
            enabled=True,
            host_contract_confirmed=True,
            public_base_url=origin,
            profile_reference="test-profile",
            signing_key_env="TEST_ARTIFACT_RETURN_KEY",
        ),
    )
    if lease_seconds is not None:
        settings = replace(settings, worker=replace(settings.worker, lease_seconds=lease_seconds))
    app = create_app(settings)

    @app.middleware("http")
    async def observed(request, call_next):
        response = await call_next(request)
        entry = {"method": request.method, "path": request.url.path, "status": response.status_code}
        if request.method == "PUT" and response.status_code == 200:
            from cf_agent_gateway.database import (
                create_database_engine,
                create_database_session_factory,
            )
            from cf_agent_gateway.task.model import HermesDispatchRecord

            engine = create_database_engine(settings.database.url)
            try:
                with create_database_session_factory(engine)() as session:
                    dispatch_id = int(request.url.path.split("/")[4])
                    row = session.get(HermesDispatchRecord, dispatch_id)
                    if row and row.lease_expires_at and row.claimed_at:
                        held = (row.lease_expires_at - row.claimed_at).total_seconds()
                        entry["lease_renewed_before_upload"] = held > settings.worker.lease_seconds
            finally:
                engine.dispose()
        observations.append(entry)
        return response

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            ssl_certfile=str(certificates[1]),
            ssl_keyfile=str(certificates[2]),
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield settings
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def isolated_environment(case: Path) -> dict[str, str]:
    env = {
        k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT") if k in os.environ
    }
    env.update(
        {
            "HERMES_HOME": str(case / "home"),
            "HERMES_BUNDLED_PLUGINS": str(case / "empty-bundled"),
            "HERMES_ENABLE_PROJECT_PLUGINS": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "PYTHONUTF8": "1",
            "HOME": str(case / "user"),
            "USERPROFILE": str(case / "user"),
            "APPDATA": str(case / "user/AppData"),
            "LOCALAPPDATA": str(case / "user/LocalAppData"),
            "TEMP": str(case / "tmp"),
            "TMP": str(case / "tmp"),
            "TMPDIR": str(case / "tmp"),
        }
    )
    return env


def run_case(
    *,
    source: Path,
    hermes_python: Path,
    site_packages: Path,
    output: Path,
    kind: str,
    model_config: dict,
    model_label: str,
    input_path: Path | None = None,
    model_calls: list | None = None,
    agent_options: dict | None = None,
    lease_seconds: float | None = None,
) -> dict:
    """One business POST, never retry the Agent after a timeout or failed assertion.

    Callers may supply the already-approved model configuration in memory. This
    function does not discover or read real model credentials/configurations.
    """
    import httpx
    import pytest
    import test_artifact_return_integration as existing
    from sqlalchemy import select
    from test_hermes_tls import _test_certificates

    from cf_agent_gateway.artifact import ArtifactRepository
    from cf_agent_gateway.artifact.models import Artifact
    from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryReceipt
    from cf_agent_gateway.hermes import HermesClient
    from cf_agent_gateway.response.models import ResponseRecord
    from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
    from cf_agent_gateway.task.model import HermesDispatchRecord

    source_proof = verify_source(source)
    agent_options = dict(agent_options or {})
    if set(agent_options) - {"reasoning_effort", "service_tier"}:
        raise ValueError("Only approved reasoning effort/service tier may be forwarded")
    case = output.resolve()
    case.mkdir()  # Refuse an existing intent/evidence directory; never rerun in it.
    for name in ("home", "empty-bundled", "tmp", "user", "inputs", "work", "certificates"):
        (case / name).mkdir()
    certificate = _test_certificates(case / "certificates")
    if input_path is not None:
        filename, expected_size, expected_sha = APPROVED[kind]
        original = input_path.read_bytes()
        assert (len(original), digest(original)) == (expected_size, expected_sha)
    else:
        filename = "isolated-input.pdf" if kind == "file" else "isolated-input.png"
        original = existing.PDF if kind == "file" else existing.PNG
    if kind != "text":
        (case / "inputs" / filename).write_bytes(original)
    key = secrets.token_hex(32)
    tls_port = _unused_port()
    session_id = "cf-return-" + secrets.token_hex(16)
    prompt = (
        f"请将本次任务工作目录中的 {filename} 作为"
        + ("PDF 文件" if kind == "file" else "图片")
        + "返回发起任务的当前聊天。不要解析文档或分析图像，不要下载文件。"
        + "请自己调用 cf_return_current_chat 工具；工具 READY 只表示已交给 Gateway 等待投递。"
        if kind != "text"
        else "只回答一条简短文字，不调用文件返回工具，也不发送附件。"
    )
    write_new(
        case / "intent.json",
        {
            "intent_id": session_id,
            "kind": kind,
            "prompt": prompt,
            "source_commit": COMMIT,
            "source_files": source_proof,
            "bridge_files": {
                str(path.relative_to(Path(__file__).resolve().parents[2])): digest(
                    path.read_bytes()
                )
                for path in (
                    Path(__file__).resolve().parents[2]
                    / "src/cf_agent_gateway/hermes/return_bridge"
                ).glob("*.py")
            },
            "approved_agent_options": agent_options,
            "model": model_label,
            "receiver": "loopback substitute, not WeChat",
            "input": None
            if kind == "text"
            else {
                "filename": filename,
                "bytes": len(original),
                "sha256": digest(original),
                "origin": "approved local sample copy" if input_path else "synthetic fixture",
                "new_download": False,
            },
        },
    )
    receiver = existing.WechatReceiver("wxid-official-return-test")
    http_observations = []
    gateway_observations = []
    secrets_for_scan = {key.encode(), model_config["api_key"].encode()}

    class ObservedClient(HermesClient):
        async def _request_async(self, method, endpoint, *, json, headers):
            capability = (headers or {}).get("X-CF-Artifact-Return-Authorization", "")
            if capability:
                secrets_for_scan.update(
                    {
                        capability.encode(),
                        capability.removeprefix("Bearer ").encode(),
                    }
                )
            response = await super()._request_async(method, endpoint, json=json, headers=headers)
            ack = response.headers.get("X-CF-Artifact-Return-Accepted")
            http_observations.append(
                {
                    "method": method,
                    "endpoint": endpoint,
                    "status": response.status_code,
                    "request": json,
                    "response": response.json(),
                    "session_id": response.headers.get("X-Hermes-Session-Id"),
                    "accepted_ack_present": bool(ack),
                    "accepted_ack_matches": bool(capability) and ack == digest(capability.encode()),
                }
            )
            return response

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            existing,
            "gateway_http",
            lambda s: gateway_https(s, certificate, gateway_observations, lease_seconds),
        )
        with existing.runtime(
            case, patch, account=receiver.account, group=kind == "image", content=prompt
        ) as (settings, factory, admitted, chat):
            plugin = case / "home/plugins/cf-artifact-return"
            plugin.mkdir(parents=True)
            (plugin / "plugin.yaml").write_text(
                'name: cf-artifact-return\nversion: "1.0.0"\nkind: standalone\n', encoding="utf-8"
            )
            (plugin / "__init__.py").write_text(PLUGIN, encoding="utf-8")
            safe_model = {k: v for k, v in model_config.items() if k != "api_key"}
            config = {
                "model": safe_model,
                "plugins": {
                    "enabled": ["cf-artifact-return"],
                    "entries": {
                        "cf-artifact-return": {
                            "settings": {
                                "enabled": True,
                                "gateway_origin": settings.artifact_return.public_base_url,
                                "gateway_ca_file": str(certificate[0]),
                                "work_root": str(case / "work"),
                                "profile_reference": "test-profile",
                                "profile_revision": 1,
                                "tls_host": "127.0.0.1",
                                "tls_port": tls_port,
                                "tls_cert_file": str(certificate[1]),
                                "tls_key_file": str(certificate[2]),
                                "request_timeout_seconds": 600,
                            }
                        },
                    },
                },
                "platform_toolsets": {"api_server": ["cf_artifact_return"]},
                "agent": {**agent_options, "max_iterations": 6},
                "memory": {"enabled": False},
                "skills": {"enabled": False},
                "compression": {"enabled": False},
                "terminal": {"cwd": str(case / "work")},
                "checkpoints": {"enabled": False},
            }
            (case / "home/config.yaml").write_text(json.dumps(config), encoding="utf-8")
            child_payload = {
                "source": str(source),
                "repository": str(Path(__file__).resolve().parents[2]),
                "case": str(case),
                "filename": filename if kind != "text" else None,
                "service_key": key,
                "model_key": model_config["api_key"],
                "model_label": model_label,
            }
            command = [
                str(hermes_python),
                "-I",
                "-S",
                "-B",
                str(Path(__file__).resolve()),
                "--child",
                "--site-packages",
                str(site_packages),
            ]
            with (
                (case / "child.stdout.txt").open("x", encoding="utf-8") as stdout,
                (case / "child.stderr.txt").open("x", encoding="utf-8") as stderr,
                subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    encoding="utf-8",
                    cwd=case,
                    env=isolated_environment(case),
                ) as process,
            ):
                report = None
                assert process.stdin is not None
                process.stdin.write(json.dumps(child_payload) + "\n")
                process.stdin.flush()
                try:
                    deadline = time.monotonic() + 45
                    while not (case / "ready.json").exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError(
                                "Official isolated process exited; inspect its evidence"
                            )
                        time.sleep(0.05)
                    assert (case / "ready.json").exists(), "Official isolated process not ready"
                    origin = json.loads((case / "ready.json").read_text())["origin"]
                    with ObservedClient(
                        origin,
                        key,
                        model_config["default"],
                        ca_file=str(certificate[0]),
                        timeout=httpx.Timeout(600, connect=10, write=10, pool=10),
                    ) as client:
                        worker = build_dispatch_worker(
                            settings,
                            session_factory=factory,
                            hermes_client=client,
                            sender_factory=None,
                        )
                        result = worker.run_once()  # The sole business submission.
                        write_new(case / "http.json", http_observations)
                        assert result is not None and result.status.value == "success", result
                        assert worker.run_once() is None
                    from cf_agent_gateway.hermes.tls import verified_ssl_context

                    with httpx.Client(
                        verify=verified_ssl_context(str(certificate[0])),
                        trust_env=False,
                        follow_redirects=False,
                    ) as observer:
                        actual_session = http_observations[-1]["session_id"]
                        history = observer.get(
                            origin
                            + "/api/sessions/"
                            + quote(actual_session, safe="")
                            + "/messages?order=oldest&limit=500",
                            headers={"Authorization": "Bearer " + key},
                        )
                        assert history.status_code == 200
                        write_new(case / "official-session.json", history.json())
                    with existing.protocol_peer(receiver) as peer:
                        outcome = existing.deliver(settings, factory, peer)
                        assert outcome.status.value == "delivered"
                        assert existing.deliver(settings, factory, peer) is None
                    with factory() as db:
                        artifacts = list(db.scalars(select(Artifact)))
                        response = db.scalar(select(ResponseRecord))
                        delivery = db.scalar(select(DeliveryOutboxRecord))
                        dispatch = db.get(HermesDispatchRecord, admitted.dispatch_record_id)
                        receipts = list(db.scalars(select(DeliveryReceipt)))
                        assert response and delivery and dispatch
                        assert all(r["chatId"] == chat for r in receiver.requests)
                        assert dispatch.attempt_count == 1 and delivery.attempt_count == 1
                        assert len(artifacts) == (0 if kind == "text" else 1)
                        if artifacts:
                            artifact = artifacts[0]
                            actual = ArtifactRepository(db, settings.artifact.storage_root).read(
                                artifact.artifact_id
                            )
                            sent = next(item[kind] for item in receiver.requests if kind in item)
                            received = base64.b64decode(sent["data"], validate=True)
                            assert actual == received == original
                            if kind == "file":
                                assert sent["filename"] == filename
                            (case / ("receiver-" + filename)).write_bytes(received)
                        report = {
                            "ok": True,
                            "contract": "cf-artifact-return/v1",
                            "hermes_commit": COMMIT,
                            "model": model_label,
                            "official_http_and_agent": True,
                            "tls_both_directions": True,
                            "http_business_posts": sum(
                                o["method"] == "POST" for o in http_observations
                            ),
                            "ack_verified": all(
                                o["accepted_ack_matches"] for o in http_observations
                            ),
                            "gateway_http": gateway_observations,
                            "lease_seconds": settings.worker.lease_seconds,
                            "message_id": admitted.message_id,
                            "dispatch_id": dispatch.id,
                            "hermes_session_id": http_observations[-1]["session_id"],
                            "response_id": response.response_id,
                            "delivery_id": delivery.id,
                            "dispatch_status": dispatch.status.value,
                            "delivery_status": delivery.status.value,
                            "receipt_count": len(receipts),
                            "target": chat,
                            "account": receiver.account,
                            "artifacts": [
                                {
                                    "id": a.artifact_id,
                                    "size": a.size,
                                    "sha256": a.sha256,
                                    "status": a.status.value,
                                }
                                for a in artifacts
                            ],
                            "final_answer": http_observations[-1]["response"]["choices"][0][
                                "message"
                            ]["content"],
                            "receiver": "loopback substitute; no real WeChat",
                        }
                    write_new(case / "receiver.json", receiver.requests)
                    if model_calls is not None:
                        write_new(case / "synthetic-model.json", model_calls)
                    return report
                finally:
                    if process.poll() is None:
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                        try:
                            process.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            process.kill()  # This owned temporary child only, never a service.
                            process.wait(timeout=10)
                    if not (case / "http.json").exists():
                        write_new(case / "http.json", http_observations)
                    write_new(case / "gateway-http.json", gateway_observations)
                    matches = 0
                    checked_files = 0
                    for evidence in case.rglob("*"):
                        if evidence.is_file():
                            checked_files += 1
                            raw = evidence.read_bytes()
                            matches += sum(value in raw for value in secrets_for_scan)
                    scan = {
                        "files": checked_files,
                        "matches": matches,
                        "included": "all files including SQLite/WAL/SHM and sample bytes",
                    }
                    write_new(case / "credential-scan.json", scan)
                    if matches:
                        raise AssertionError("Credential leaked into probe evidence")
                    if report is not None:
                        report["credential_scan"] = scan
                        write_new(case / "report.json", report)


_PAYLOAD = {}
_RECORD_LOCK = threading.Lock()


def record(kind, payload):
    """Private evidence callback. Never serialize a capability or process config."""
    data = {"kind": kind, "time": time.time(), **payload}
    encoded = json.dumps(data, ensure_ascii=False, default=str)
    for name in ("service_key", "model_key"):
        secret = _PAYLOAD.get(name)
        if secret and secret in encoded:
            raise RuntimeError("Credential in evidence callback")
    with (
        _RECORD_LOCK,
        (Path(_PAYLOAD["case"]) / "events.jsonl").open("a", encoding="utf-8") as stream,
    ):
        stream.write(encoded + "\n")


def prepare_scope(scope):
    filename = _PAYLOAD.get("filename")
    if filename:
        source = Path(_PAYLOAD["case"]) / "inputs" / filename
        content = source.read_bytes()
        with (scope.work_root / filename).open("xb") as stream:
            stream.write(content)
        record(
            "test_input_copy",
            {
                "request_id": scope.request_id,
                "filename": filename,
                "bytes": len(content),
                "sha256": digest(content),
                "path": str(scope.work_root / filename),
                "new_download": False,
            },
        )


def _readonly_metadata_paths(platform_name: str, process_id: int) -> set[Path]:
    if platform_name == "nt":
        return set()
    return {
        Path(value).resolve()
        for value in (
            "/proc/stat",
            "/proc/version",
            "/proc/1/cgroup",
            f"/proc/{process_id}/stat",
            "/dev/null",
            # OpenAI 2.24.0 platform_headers -> distro.id -> os_release_attr.
            # distro selects these two public OS metadata files, not an /etc tree.
            "/etc/os-release",
            "/usr/lib/os-release",
        )
    }


def _install_child_audit(source: Path, repository: Path, case: Path, model: dict):
    """Bound this probe's I/O; this Python audit is not a host OS sandbox."""
    allowed = [
        source.resolve(),
        repository.resolve(),
        case.resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
    ]
    for path in sys.path:
        if path and "site-packages" in path:
            allowed.append(Path(path).resolve())
    metadata = _readonly_metadata_paths(os.name, os.getpid())
    host = urlsplit(model["base_url"]).hostname
    approved = {"127.0.0.1", "::1", "localhost"}
    if host not in approved:
        if urlsplit(model["base_url"]).scheme != "https":
            raise ValueError("An approved remote model still requires HTTPS")
        approved.add(host)
        approved.update(item[4][0] for item in socket.getaddrinfo(host, 443))

    def reject(message: str, event: str, path: Path | None = None):
        # Only the private trace gets a denied path, never the model-facing error.
        details = {"event": event, "reason": message}
        if path is not None:
            details["path"] = str(path)
        record("probe_audit_denied", details)
        raise PermissionError(message)

    def audit(event, args):
        if event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple) and address[0] not in approved:
                reject("Probe rejected network destination", event)
        if event == "socket.getaddrinfo":
            requested_host = args[0].decode("ascii") if isinstance(args[0], bytes) else args[0]
            if requested_host not in approved | {None}:
                reject("Probe rejected DNS destination", event)
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
            reject("Probe does not authorize subprocess tools", event)
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.name == ".env" and not path.is_relative_to(case):
                reject("Probe does not read installed credentials", event, path)
            if (
                not any(path.is_relative_to(root) for root in allowed)
                and path not in metadata
                and os.path.normcase(str(path)) not in {"nul", os.path.normcase(os.devnull)}
            ):
                reject("Probe rejected file outside isolated/read-only roots", event, path)
            mode = args[1] if len(args) > 1 else "r"
            flags = args[2] if len(args) > 2 else 0
            writing = isinstance(mode, str) and any(v in mode for v in "wax+")
            writing = writing or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            if writing and not path.is_relative_to(case):
                reject("Probe rejected write outside its task directory", event, path)
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.rename"}:
            candidates = args[:2] if event == "os.rename" else args[:1]
            for candidate in candidates:
                if isinstance(candidate, (str, bytes, os.PathLike)) and not Path(
                    os.fsdecode(candidate)
                ).resolve().is_relative_to(case):
                    reject(
                        "Probe rejected mutation outside its task directory",
                        event,
                        Path(os.fsdecode(candidate)),
                    )

    sys.addaudithook(audit)


async def child():
    global _PAYLOAD
    _PAYLOAD = json.loads(sys.stdin.readline())
    case, source = Path(_PAYLOAD["case"]), Path(_PAYLOAD["source"])
    assert platform.python_version() == "3.14.7", "Use selected existing Python 3.14.7"
    verify_source(source)
    sys.path[:0] = [str(source), str(Path(_PAYLOAD["repository"]) / "src")]
    sys.modules["cf_return_probe_child"] = sys.modules[__name__]
    import mimetypes

    mimetypes.knownfiles = []
    mimetypes.inited = True
    mimetypes._db = mimetypes.MimeTypes(filenames=())
    mimetypes.init(files=[])
    config = json.loads((case / "home/config.yaml").read_text())
    _install_child_audit(source, Path(_PAYLOAD["repository"]), case, config["model"])
    # Fail before submitting any business request if SDK platform detection needs
    # an unapproved OS file; otherwise its internal provider retry delays obscure it.
    from openai._base_client import get_platform

    record("sdk_platform_preflight", {"platform": str(get_platform())})
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    # Model credential is request-context memory, not an environment/config value.
    secret_token = set_secret_scope(
        {"OPENAI_API_KEY": _PAYLOAD["model_key"], "CUSTOM_API_KEY": _PAYLOAD["model_key"]}
    )
    discover_plugins()
    manager = get_plugin_manager()
    assert manager._plugins["cf-artifact-return"].error is None, "Return plugin failed to load"
    enabled = [name for name, plugin in manager._plugins.items() if plugin.enabled]
    assert enabled == ["cf-artifact-return"], enabled
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "key": _PAYLOAD["service_key"],
                "model_name": "hermes-agent",
            },
        )
    )
    try:
        assert await adapter.connect()
        config = json.loads((case / "home/config.yaml").read_text())
        port = config["plugins"]["entries"]["cf-artifact-return"]["settings"]["tls_port"]
        # Readiness is an HTTPS handshake to the exact trusted temporary listener.
        import httpx

        from cf_agent_gateway.hermes.tls import verified_ssl_context

        context = verified_ssl_context(str(case / "certificates/ca.pem"))
        async with httpx.AsyncClient(
            verify=context, trust_env=False, follow_redirects=False, timeout=2
        ) as client:
            readiness = []
            for _ in range(50):
                try:
                    response = await client.get(
                        f"https://localhost:{port}/v1/models",
                        headers={
                            "Authorization": "Bearer " + _PAYLOAD["service_key"],
                        },
                    )
                    if response.status_code == 200:
                        break
                    readiness.append({"status": response.status_code})
                except httpx.TransportError as exc:
                    readiness.append({"error": type(exc).__name__, "detail": str(exc)})
                await asyncio.sleep(0.1)
            else:
                write_new(case / "readiness-failure.json", readiness)
                raise RuntimeError("Official HTTPS listener did not become ready")
        write_new(
            case / "ready.json",
            {
                "origin": f"https://localhost:{port}",
                "python": platform.python_version(),
                "plugins": enabled,
            },
        )
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await adapter.disconnect()
        manager.unload()
        reset_secret_scope(secret_token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--hermes-python", type=Path)
    parser.add_argument("--site-packages", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--kind", choices=("file", "image", "text"), default="file")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        sys.path.append(str(args.site_packages))
        asyncio.run(child())
        return
    if not all((args.hermes_source, args.hermes_python, args.output)):
        parser.error("--hermes-source, --hermes-python and --output are required")
    sys.path[:0] = [str(Path(__file__).parents[1]), str(Path(__file__).parents[2] / "src")]
    filename = "isolated-input.pdf" if args.kind == "file" else "isolated-input.png"
    with synthetic_model(filename, args.kind) as (model, calls):
        report = run_case(
            source=args.hermes_source,
            hermes_python=args.hermes_python,
            site_packages=args.site_packages,
            output=args.output,
            kind=args.kind,
            model_config=model,
            model_label="loopback synthetic model",
            model_calls=calls,
            lease_seconds=3,
        )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
