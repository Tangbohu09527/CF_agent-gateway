"""Explicit clean official Hermes HTTP MCP boundary experiment (no paid model).

This is a probe, not production enablement. The MCP service and model peer run
outside Hermes. Only config.yaml and synthetic .env are placed in its HOME.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

COMMIT = "427d605d4985c2d49a795493ef6c5d66413057d1"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
TEXT = "SYNTHETIC-REMOTE-CONTENT-20261020"


def load_model():
    path = Path(__file__).parent / "hermes_file_return_probe/model.py"
    spec = importlib.util.spec_from_file_location("native_mcp_model_peer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Planner:
    def __init__(self, peer, file_service=False):
        self.peer = peer
        self.calls = []
        self.file_service = file_service

    def decide(self, body):
        self.calls.append(body)
        messages = body.get("messages", [])
        if not body.get("tools"):
            return {"role": "assistant", "content": "Synthetic probe"}
        visible, schemas = self.peer.schemas(body, messages)
        required = ["mcp__cf__content_probe", "mcp__cf__query_current_task"]
        if self.file_service:
            required += ["mcp__cf__employee_query_probe", "mcp__cf__employee_export_probe"]
        missing = [name for name in required if name not in schemas]
        if missing:
            return self.peer.tool_call("tool_describe", {"names": missing}, visible)
        results = json.dumps([m for m in messages if m.get("role") == "tool"])
        records = [
            o
            for m in messages
            if m.get("role") == "tool"
            for o in self.peer.objects(m.get("content"))
        ]
        if TEXT not in results:
            return self.peer.tool_call(required[0], {}, visible)
        if self.file_service and not any(o.get("employee_query") == "complete" for o in records):
            return self.peer.tool_call(required[2], {}, visible)
        if self.file_service and not any(o.get("employee_export") == "complete" for o in records):
            return self.peer.tool_call(required[3], {}, visible)
        if "trusted_task_context_missing" not in results:
            return self.peer.tool_call(required[1], {"query": "synthetic product"}, visible)
        return {"role": "assistant", "content": "NATIVE-MCP-BOUNDARY-RECORDED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api", action="store_true")
    parser.add_argument("--filebridge-source", type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.hermes_source = args.hermes_source.resolve()
    args.hermes_python = args.hermes_python.absolute()
    head = subprocess.check_output(
        ["git", "-C", str(args.hermes_source), "rev-parse", "HEAD"], text=True
    ).strip()
    assert head == COMMIT
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ("home", "user", "tmp", "empty-bundled"):
        (args.output / name).mkdir()
    import uvicorn
    from mcp.server import MCPServer
    from mcp_types import ImageContent, TextContent, ToolAnnotations

    service = MCPServer("cf-native-candidate")
    resources = ExitStack()
    file_proof = None
    if args.filebridge_source:
        path = Path(__file__).with_name("native_mcp_file_service.py")
        spec = importlib.util.spec_from_file_location("native_mcp_file_service", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        query, export, file_proof = resources.enter_context(
            module.fixture(args.filebridge_source.resolve(), args.output / "file-service")
        )

        @service.tool(annotations=ToolAnnotations(read_only_hint=True))
        def employee_query_probe() -> dict:
            """Query the isolated ordinary employee fixture; no current-task return authority."""
            return query()

        @service.tool(annotations=ToolAnnotations(read_only_hint=False))
        def employee_export_probe() -> dict:
            """Export/download synthetic employee bytes on the server; no chat publication."""
            return export()

    @service.tool(annotations=ToolAnnotations(read_only_hint=True))
    def content_probe() -> list:
        """Return synthetic text and PNG to verify the formal tool message path."""
        return [
            TextContent(type="text", text=TEXT),
            ImageContent(type="image", data=base64.b64encode(PNG).decode(), mime_type="image/png"),
        ]

    @service.tool(annotations=ToolAnnotations(read_only_hint=True))
    def query_current_task(query: str) -> dict:
        """Query only with a trusted current-task binding; model arguments carry no identity."""
        return {"ok": False, "error": "trusted_task_context_missing"}

    inner = service.streamable_http_app()
    wire = []
    token = secrets.token_hex(32)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if headers.get("authorization") != "Bearer " + token:
            await send({"type": "http.response.start", "status": 401, "headers": []})
            return await send({"type": "http.response.body", "body": b"unauthorized"})
        if scope["method"] != "POST":
            return await inner(scope, receive, send)
        chunks = []
        while True:
            event = await receive()
            chunks.append(event.get("body", b""))
            if not event.get("more_body"):
                break
        body = b"".join(chunks)
        wire.append(
            {
                "rpc": json.loads(body),
                "profile": headers.get("x-hermes-profile"),
                "header_names": sorted(headers),
                "authenticated": True,
                "transport_session": headers.get("mcp-session-id"),
            }
        )
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await inner(scope, replay, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    mcp_port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    peer = load_model()
    model = peer.ModelServer()
    model.planner = Planner(peer, bool(args.filebridge_source))
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    api_key = secrets.token_hex(32)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        api_port = sock.getsockname()[1]
    config = {
        "model": model.config,
        "models_dev": {"url": f"http://127.0.0.1:{model.server_port}/models-dev.json"},
        "mcp_servers": {
            "cf": {
                "url": f"http://127.0.0.1:{mcp_port}/mcp",
                "headers": {"Authorization": "Bearer " + token},
                "identity_header": {"name": "X-Hermes-Profile", "value_from": "profile"},
                "timeout": 20,
            }
        },
        "mcp_discovery_timeout": 30,
        "mcp_single_query_discovery_timeout": 30,
        "platform_toolsets": {"api_server": ["mcp-cf"], "cli": ["mcp-cf"]},
        "agent": {"max_iterations": 8, "environment_probe": False},
        "memory": {"enabled": False},
        "skills": {"enabled": False},
        "compression": {"enabled": False},
        "checkpoints": {"enabled": False},
        "security": {"allow_lazy_installs": False},
        "updates": {"check": False},
        "telemetry": {"shared_metrics": {"enabled": False, "send": False, "offer_version": 1}},
        "platforms": {
            "api_server": {
                "enabled": True,
                "extra": {"host": "127.0.0.1", "port": api_port, "key": api_key},
            }
        },
    }
    (args.output / "home/config.yaml").write_text(json.dumps(config), encoding="utf-8")
    (args.output / "home/.env").write_text(
        "OPENAI_API_KEY=synthetic-loopback-model-key\n", encoding="utf-8"
    )
    env = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH")
        if key in os.environ
    }
    env.update(
        {
            "HERMES_HOME": str(args.output / "home"),
            "HOME": str(args.output / "user"),
            "USERPROFILE": str(args.output / "user"),
            "APPDATA": str(args.output / "user/AppData"),
            "LOCALAPPDATA": str(args.output / "user/LocalAppData"),
            "TEMP": str(args.output / "tmp"),
            "TMP": str(args.output / "tmp"),
            "HERMES_TEST_ISOLATION": "1",
            "PYTHONUTF8": "1",
            "HERMES_BUNDLED_PLUGINS": str(args.output / "empty-bundled"),
            "HERMES_ENABLE_PROJECT_PLUGINS": "false",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "API_SERVER_KEY": api_key,
        }
    )
    command = [str(args.hermes_python), "-B", "-m", "hermes_cli.main"]
    command += (
        ["gateway", "run"]
        if args.api
        else ["chat", "-q", "Run content_probe, then query_current_task."]
    )
    result = {}
    process = None
    try:
        with (
            (args.output / "official.stdout").open("wb") as out,
            (args.output / "official.stderr").open("wb") as err,
        ):
            process = subprocess.Popen(
                command, cwd=args.hermes_source, env=env, stdout=out, stderr=err
            )
            if args.api:
                import httpx

                with httpx.Client(trust_env=False, timeout=90) as client:
                    deadline = time.monotonic() + 90
                    while True:
                        assert process.poll() is None, "official API exited during startup"
                        try:
                            response = client.get(f"http://127.0.0.1:{api_port}/health")
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert time.monotonic() < deadline, "official API readiness timeout"
                        time.sleep(0.2)
                    auth = {"Authorization": "Bearer " + api_key}
                    sessions = ["gateway-task-a", "gateway-task-b"]
                    for sid in sessions:
                        created = client.post(
                            f"http://127.0.0.1:{api_port}/api/sessions",
                            headers=auth,
                            json={"id": sid},
                        )
                        created.raise_for_status()

                    def turn(sid):
                        return client.post(
                            f"http://127.0.0.1:{api_port}/v1/chat/completions",
                            headers={
                                **auth,
                                "X-Hermes-Session-Id": sid,
                                "X-Hermes-Session-Key": "channel-" + sid,
                            },
                            json={
                                "model": peer.MODEL,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "Run content_probe, then query_current_task.",
                                    }
                                ],
                                "metadata": {"task_id": "trusted-" + sid},
                            },
                        )

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        responses = list(executor.map(turn, sessions))
                    result["api_status"] = [r.status_code for r in responses]
                    result["api_sessions"] = sessions
                    result["api_response"] = [r.json() for r in responses]
            else:
                process.wait(timeout=120)
                result["official_exit"] = process.returncode
    except Exception as error:
        result["failure"] = {"class": type(error).__name__, "message": str(error)}
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        server.should_exit = True
        thread.join(10)
        model.shutdown()
        model.server_close()
        model_thread.join(5)
        resources.close()
    calls = [record for record in wire if record["rpc"].get("method") == "tools/call"]
    tool_messages = [
        m
        for call in model.planner.calls
        for m in call.get("messages", [])
        if m.get("role") == "tool"
    ]
    result.update(
        {
            "hermes_commit": head,
            "model": "HTTP substitute",
            "wechat": "not reached",
            "wire": wire,
            "model_errors": model.errors,
            "model_calls": len(model.planner.calls),
            "official_selected_handlers": [r["rpc"]["params"]["name"] for r in calls],
            "text_in_formal_tool_message": TEXT in json.dumps(tool_messages),
            "image_in_formal_tool_message": any(
                o.get("type") == "image_url"
                for m in tool_messages
                for o in peer.objects(m.get("content"))
            ),
            "trusted_task_rejected": "trusted_task_context_missing" in json.dumps(tool_messages),
            "project_plugin_files": list((args.output / "home").glob("plugins/**/*")),
            "pythonpath_in_child": "PYTHONPATH" in env,
            "file_service": file_proof,
        }
    )
    result["project_plugin_files"] = [str(p) for p in result["project_plugin_files"]]
    result["transport_sessions_for_task_calls"] = sorted(
        {
            r["transport_session"]
            for r in calls
            if r["rpc"]["params"]["name"] == "query_current_task"
        }
    )
    result["per_call_meta"] = [r["rpc"]["params"].get("_meta") for r in calls]
    result["boundary_probe_passed"] = bool(
        calls
        and not result.get("failure")
        and not model.errors
        and result["trusted_task_rejected"]
        and result["text_in_formal_tool_message"]
        and result["image_in_formal_tool_message"]
        and not result["project_plugin_files"]
        and all(not meta for meta in result["per_call_meta"])
        and (not args.filebridge_source or file_proof.get("members_bytes_verified"))
    )
    (args.output / "safe-summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (args.output / "synthetic-model-messages.json").write_text(
        json.dumps([call.get("messages", []) for call in model.planner.calls], indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"wire", "api_response"}}
        )
    )
    return 0 if result["boundary_probe_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
