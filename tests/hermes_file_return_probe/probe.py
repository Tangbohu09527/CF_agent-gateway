"""Explicit Linux: real Gateway -> official Hermes427 -> FileBridge -> return.

Only model HTTP and external WeChat fetch/receipt are substitutes. Admission,
POSIX staging, claims, session API, native download, parsers, middleware,
Artifact/Response/Delivery persistence and outbound protocol are production code.
Never imported by pytest; no installed HOME, model or service is discovered.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HERMES = "427d605d4985c2d49a795493ef6c5d66413057d1"
ACCOUNT, CHAT = "wxid-file-return-fixture", "wxid-file-return-alice"
SERVICE_ENV = "CF_FILEBRIDGE_HOST_RETURN_PROBE"
MODEL_KEY = "synthetic-loopback-model-key"
# Same basename exercised by the separate protocol candidate's native Qt fixture.
PDF_FILENAME = "中文 空格 (1) %23 #.pdf"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_received_pdf(sent, expected):
    """Assert the actual Gateway outbound JSON, not only its Artifact row."""
    assert sent["filename"] == PDF_FILENAME, "outbound PDF filename changed"
    assert base64.b64decode(sent["data"], validate=True) == expected


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True)


def source_inventory(root, revision, relative, *, allow_dirty=False):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("an immutable source commit is required")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == revision, "source checkout differs from requested commit"
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", *relative],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    dirty = bool(status.strip())
    assert allow_dirty or not dirty, "source files differ from immutable commit"
    files = {}
    for item in relative:
        path = root / item
        paths = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for entry in paths:
            assert entry.is_file() and not entry.is_symlink()
            files[entry.relative_to(root).as_posix()] = sha(entry.read_bytes())
    return {
        "commit": revision,
        "worktree_dirty": dirty,
        "files_sha256": files,
        "tree_sha256": sha(json.dumps(files, sort_keys=True).encode()),
    }


def isolated_host_environment(environment, home):
    # Official427's positive isolation marker prevents ancestry discovery and
    # strengthens SessionDB's live-database protection; never enable its bypass.
    result = dict(environment)
    result["HERMES_HOME"] = str(home)
    result["HERMES_TEST_ISOLATION"] = "1"
    result.pop("HERMES_STATE_DB_GUARD_BYPASS", None)
    return result


def ordinary_windows():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    token, elevated = wintypes.HANDLE(), wintypes.DWORD()
    size = wintypes.DWORD()
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    api.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    assert api.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token))
    try:
        assert api.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size))
        assert not elevated.value, "this component test must use an ordinary token"
    finally:
        kernel.CloseHandle(token)
    return True


RETURN_PLUGIN = """from cf_agent_gateway.hermes.return_bridge.plugin import setup
from cf_file_return_host import event
def register(ctx):
    setup(ctx, observer=event)
    def observe(**kw):
        if kw.get("tool_name") in ("filebrowser_read_inbound", "cf_return_current_chat"):
            event("official_tool", {key: kw.get(key) for key in
                  ("session_id", "task_id", "turn_id", "tool_name", "result")})
    ctx.register_hook("post_tool_call", observe)
"""
_HOST_PAYLOAD = {}
_EVENT_LOCK = threading.Lock()


def event(kind, payload):
    value = {"kind": kind, **payload}
    encoded = json.dumps(value, default=str)
    for name in ("service_key", "host_key"):
        assert _HOST_PAYLOAD.get(name, "not-a-secret") not in encoded
    with (
        _EVENT_LOCK,
        (Path(_HOST_PAYLOAD["case"]) / "events.jsonl").open("a", encoding="utf-8") as output,
    ):
        output.write(encoded + "\n")


def install_host_audit(case, sources, worker, support):
    """Narrow test I/O guard, with byte-host normalization used by httpcore.

    The subprocess allowance is the existing FileBridge worker/parser contract.
    Optional global housekeeping is denied, never faked as successful.
    """
    import errno

    roots = (case, *sources, Path(sys.prefix), Path(sys.base_prefix))
    metadata = {
        "/proc/1/cgroup",
        "/proc/self/mountinfo",
        "/proc/stat",
        "/proc/version",
        "/etc/os-release",
        "/usr/lib/os-release",
        "/etc/mime.types",
        "/etc/localtime",
        "/etc/ssl/certs/ca-certificates.crt",
        f"/proc/{os.getpid()}/stat",
    }
    violations = []
    optional_metadata_denials = []
    spawn_ticket = threading.local()
    io_support = support._local_module("cf_probe_io_support", "hermes_probe_support.py")

    def within(path, allowed):
        candidate = Path(os.fsdecode(path)).resolve()
        return any(candidate.is_relative_to(root.resolve()) for root in allowed)

    def guard(name, args):
        denied = False
        if name == "import" and args[0] == "hermes_constants_scratch":
            raise PermissionError("isolated global housekeeping unavailable")
        if name == "open" and not isinstance(args[0], int):
            path, _mode, flags = args
            if Path(os.fsdecode(path)).resolve() == (sources[0].parent / "manifest.json").resolve():
                # The official environment detector probes above its source checkout.
                # Keep it unavailable; do not read a machine/package manifest.
                optional_metadata_denials.append("source_parent_manifest")
                raise FileNotFoundError(errno.ENOENT, "isolated optional manifest unavailable")
            write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if os.name == "nt" and os.path.normcase(os.path.abspath(path)) == os.path.normcase(
                os.path.abspath(f"/proc/{os.getpid()}/stat")
            ):
                raise FileNotFoundError(errno.ENOENT, "Linux self-stat unavailable")
            denied = not io_support.audit_path_within(
                path, (case,) if write else roots, allow_extended=not write
            )
            if not write and (
                os.fsdecode(path) in metadata or Path(path).resolve() == worker.resolve()
            ):
                denied = False
            if (
                not write
                and flags & getattr(os, "O_DIRECTORY", 0)
                and Path(path).resolve() in case.parents
            ):
                denied = False
            if os.path.normcase(os.fsdecode(path)) == os.path.normcase(os.devnull):
                denied = False
            if Path(os.fsdecode(path)).name == ".env" and not within(path, (case,)):
                denied = True
        elif name in {"socket.bind", "socket.connect"}:
            address = args[1]
            denied = not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}
        elif name == "socket.getaddrinfo":
            host = args[0].decode("ascii") if isinstance(args[0], bytes) else args[0]
            denied = host not in {"localhost", "127.0.0.1", "::1", None}
        elif name == "sqlite3.connect":
            value = args[0]
            ledger = case / "return-work/.return-claims.sqlite3"
            exact_ledger = value == ledger.as_uri() + "?mode=rw"
            denied = not io_support.sqlite_access_allowed(
                ledger.as_uri() + "?mode=ro" if exact_ledger else value, case
            )
        elif name == "subprocess.Popen":
            executable, command, cwd, env = args
            spawn_ticket.value = None
            expected = subprocess.list2cmdline([str(worker)]) if os.name == "nt" else [str(worker)]
            native = command == expected and executable in (None, str(worker))
            parser = support.content_parser_launch_allowed(case, executable, command, cwd, env)
            denied = not (native or parser)
            if not denied and isinstance(command, (list, tuple)) and isinstance(env, dict):
                # CPython 3.14 may implement this exact vetted Popen via posix_spawn.
                # One same-thread ticket binds the low-level audit to that launch.
                spawn_ticket.value = (str(executable or command[0]), tuple(command), dict(env))
        elif name == "os.posix_spawn":
            executable, command, env = args
            ticket = getattr(spawn_ticket, "value", None)
            spawn_ticket.value = None
            denied = ticket is None or ticket != (str(executable), tuple(command), env)
        elif name in {"os.system", "os.exec"}:
            denied = True
        if denied:
            violations.append(
                {
                    "event": name,
                    "path": os.fsdecode(args[0]) if name in {"open", "sqlite3.connect"} else None,
                    "frames": [
                        {"file": Path(frame.filename).name, "line": frame.lineno}
                        for frame in traceback.extract_stack(limit=20)
                    ],
                }
            )
            raise PermissionError("isolated operation rejected")

    sys.addaudithook(guard)
    return violations, optional_metadata_denials


def host_record(case, name, generation=0):
    assert type(generation) is int and generation in (0, 1)
    suffix = "" if generation == 0 else f"-{generation}"
    stem, extension = name.rsplit(".", 1)
    return case / f"{stem}{suffix}.{extension}"


async def host():
    global _HOST_PAYLOAD
    _HOST_PAYLOAD = json.loads(sys.stdin.readline())
    case, source, fb = (Path(_HOST_PAYLOAD[key]) for key in ("case", "hermes", "filebridge"))
    generation = _HOST_PAYLOAD.get("generation", 0)
    assert platform.python_version() == "3.14.7"
    ordinary = ordinary_windows()
    original = load("cf_return_original_probe", REPO / "tests/hermes_return_probe/probe.py")
    original.verify_source(source, HERMES)
    sys.path[:0] = [str(source), str(REPO / "src")]
    sys.modules["cf_file_return_host"] = sys.modules[__name__]
    support = load(
        "cf_file_return_fb_joint",
        fb / "integrations/hermes-filebridge/tests/test_gateway_hermes_joint.py",
    )
    support.bootstrap_system_metadata()
    optional_git_denials = []

    def deny_optional_git(event_name, arguments):
        if event_name == "subprocess.Popen":
            executable, command, cwd, _environment = arguments
            if cwd == str(source) and (
                executable == "git"
                or isinstance(command, (list, tuple))
                and command[:1] == ["git"]
                or isinstance(command, str)
                and command.startswith("git ")
            ):
                optional_git_denials.append(True)
                # The official optional version probe catches OSError. Deny the
                # launch rather than permitting another subprocess under the guard.
                raise FileNotFoundError("isolated optional git unavailable")

    sys.addaudithook(deny_optional_git)
    violations, optional_metadata_denials = install_host_audit(
        case, (source, REPO, fb), Path(_HOST_PAYLOAD["worker"]), support
    )
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    token = set_secret_scope({"OPENAI_API_KEY": MODEL_KEY, "CUSTOM_API_KEY": MODEL_KEY})
    os.environ[SERVICE_ENV] = _HOST_PAYLOAD["host_key"]
    discover_plugins()
    manager = get_plugin_manager()
    enabled = {name for name, item in manager._plugins.items() if item.enabled}
    assert enabled == {"cf-artifact-return", "cf-filebridge"}
    assert all(manager._plugins[name].error is None for name in enabled)
    host_adapters = [
        getattr(callback, "__self__", None)
        for callback in manager._middleware.get("tool_execution", [])
    ]
    assert sum(type(owner).__name__ == "HostAdapter" for owner in host_adapters) == 1, (
        "FileBridge host wiring absent"
    )
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "key": _HOST_PAYLOAD["service_key"],
                "model_name": "hermes-agent",
            },
        )
    )
    try:
        assert await adapter.connect()
        import httpx

        from cf_agent_gateway.hermes.tls import verified_ssl_context

        origin = _HOST_PAYLOAD["origin"]
        async with httpx.AsyncClient(
            verify=verified_ssl_context(str(case / "certificates/ca.pem")),
            trust_env=False,
            follow_redirects=False,
            timeout=2,
        ) as client:
            for _ in range(100):
                try:
                    response = await client.get(
                        origin + "/v1/models",
                        headers={"Authorization": "Bearer " + _HOST_PAYLOAD["service_key"]},
                    )
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("official TLS listener not ready")
        save(
            host_record(case, "ready.json", generation),
            {
                "origin": origin,
                "ordinary_windows_token": ordinary,
                "plugins": sorted(enabled),
                "python": platform.python_version(),
                "pid": os.getpid(),
                "generation": generation,
            },
        )
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await adapter.disconnect()
        manager.unload()
        reset_secret_scope(token)
        save(
            host_record(case, "host-ended.json", generation),
            {
                "closed": True,
                "pid": os.getpid(),
                "generation": generation,
                "audit_denials": len(violations),
                "optional_metadata_reads_denied": len(optional_metadata_denials),
                "ordinary_windows_token": ordinary,
                "denials": violations,
            },
        )
    assert not violations


def unused_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


@contextmanager
def official_host(args, case, model, origin, certificates, keys, *, generation=0):
    original = load("cf_return_original_env", REPO / "tests/hermes_return_probe/probe.py")
    fb_plugin = args.filebridge_source / "integrations/hermes-filebridge/plugin"
    home = case / "profile"
    assert generation in (0, 1)
    plugin = home / "plugins/cf-artifact-return"
    if generation == 0:
        shutil.copytree(
            fb_plugin,
            home / "plugins/cf-filebridge",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        plugin.mkdir()
        (plugin / "plugin.yaml").write_text(
            'name: cf-artifact-return\nversion: "1.0.0"\nkind: standalone\n'
        )
        (plugin / "__init__.py").write_text(RETURN_PLUGIN)
        port = unused_port()
    else:
        ended = json.loads(host_record(case, "host-ended.json", 0).read_text())
        assert ended["closed"] and ended["audit_denials"] == 0
        assert (plugin / "__init__.py").read_text() == RETURN_PLUGIN
        for source in fb_plugin.rglob("*.py"):
            assert sha(source.read_bytes()) == sha(
                (home / "plugins/cf-filebridge" / source.relative_to(fb_plugin)).read_bytes()
            )
        old_config = json.loads((home / "config.yaml").read_text())
        port = old_config["plugins"]["entries"]["cf-artifact-return"]["settings"]["tls_port"]
    config = {
        "model": model.config,
        "models_dev": {"url": f"http://127.0.0.1:{model.server_port}/models-dev.json"},
        "plugins": {
            "enabled": ["cf-artifact-return", "cf-filebridge"],
            "entries": {
                "cf-artifact-return": {
                    "settings": {
                        "enabled": True,
                        "gateway_origin": origin,
                        "gateway_ca_file": str(certificates[0]),
                        "work_root": str(case / "return-work"),
                        "profile_reference": "default",
                        "profile_revision": 1,
                        "tls_host": "127.0.0.1",
                        "tls_port": port,
                        "tls_cert_file": str(certificates[1]),
                        "tls_key_file": str(certificates[2]),
                    }
                },
                "cf-filebridge": {
                    "settings": {
                        "inbound_enabled": True,
                        "inbound_host_enabled": True,
                        "inbound_content_enabled": True,
                        "inbound_content_formats": ["pdf", "png"],
                        "inbound_host": {
                            "gateway_origin": origin,
                            "service_token_env": SERVICE_ENV,
                            "profile_reference": "default",
                            "profile_revision": 1,
                            "work_root": str(case / "inbound-work"),
                            "client_path": str(args.worker),
                            "client_sha256": sha(args.worker.read_bytes()),
                            "ca_file": str(certificates[0]),
                            "ca_sha256": sha(certificates[0].read_bytes()),
                            "consumer_tools": ["filebrowser_read_inbound"],
                        },
                    }
                },
            },
        },
        "platform_toolsets": {
            "api_server": [
                "cf_artifact_return",
                "cf_filebridge_inbound",
                "cf_filebridge_inbound_content",
                "vision",
            ]
        },
        "security": {"allow_lazy_installs": False},
        "agent": {"max_iterations": 14, "environment_probe": False},
        "memory": {"enabled": False},
        "skills": {"enabled": False},
        "compression": {"enabled": False},
        "fallback_models": [],
        "fallback_provider_models": [],
        "checkpoints": {"enabled": False},
    }
    if generation == 0:
        save(home / "config.yaml", config)
    else:
        assert old_config == config, "restart must reuse the exact isolated HOME configuration"
    environment = isolated_host_environment(original.isolated_environment(case), home)
    # Native child gets only a new test service key via anonymous stdin. No model,
    # Gateway or production configuration is discovered from inherited HOME.
    payload = {
        "case": str(case),
        "hermes": str(args.hermes_source),
        "filebridge": str(args.filebridge_source),
        "worker": str(args.worker),
        "service_key": keys["hermes"],
        "host_key": keys["host"],
        "origin": f"https://localhost:{port}",
        "generation": generation,
    }
    ready_path = host_record(case, "ready.json", generation)
    assert not ready_path.exists()
    with (
        host_record(case, "host.stdout", generation).open("xb") as stdout,
        host_record(case, "host.stderr", generation).open("xb") as stderr,
    ):
        child = subprocess.Popen(
            [str(args.hermes_python), "-I", "-B", str(Path(__file__).resolve()), "--host"],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=case,
            env=environment,
        )
        try:
            child.stdin.write(json.dumps(payload).encode() + b"\n")
            child.stdin.flush()
            deadline = time.monotonic() + 40
            while not ready_path.exists():
                assert child.poll() is None, (
                    "isolated official host failed; inspect private evidence"
                )
                assert time.monotonic() < deadline, "official host readiness timeout"
                time.sleep(0.05)
            ready = json.loads(ready_path.read_text())
            assert (os.name == "nt" or ready["pid"] == child.pid) and ready[
                "generation"
            ] == generation
            yield payload["origin"]
        finally:
            if child.poll() is None:
                try:
                    child.stdin.write(b"stop\n")
                    child.stdin.flush()
                    child.wait(timeout=35)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
            assert child.returncode == 0, "owned isolated host failed shutdown"


async def run_linux(args, case, model, certificates, keys):
    import httpx
    import test_artifact_return_integration as delivery_peer
    import uvicorn
    from sqlalchemy import select
    from sqlalchemy import text as sql_text

    from cf_agent_gateway.access import AccessPolicyService, RiskLevel
    from cf_agent_gateway.adapters.wechat import NormalizedWechatMessage
    from cf_agent_gateway.adapters.wechat.inbound_media import parse_inbound_media
    from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult
    from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
    from cf_agent_gateway.artifact import ArtifactRepository
    from cf_agent_gateway.artifact.models import Artifact
    from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
    from cf_agent_gateway.config import (
        ArtifactSettings,
        DatabaseSettings,
        InboundMediaSettings,
        LoggingSettings,
        Settings,
        WorkerSettings,
    )
    from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryReceipt
    from cf_agent_gateway.gateway.app import create_app
    from cf_agent_gateway.hermes import HermesClient
    from cf_agent_gateway.identity.service import IdentityService
    from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings
    from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
    from cf_agent_gateway.inbound.models import InboundMediaJob
    from cf_agent_gateway.inbound.worker import InboundMediaWorker
    from cf_agent_gateway.ingestion import MessageAdmissionService
    from cf_agent_gateway.message.models import Attachment, Message
    from cf_agent_gateway.response.models import ResponseRecord
    from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
    from cf_agent_gateway.task.model import HermesDispatchRecord
    from cf_agent_gateway.workspace.models import AIThread

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    gateway_origin = f"https://localhost:{listener.getsockname()[1]}"
    settings = Settings(
        database=DatabaseSettings(f"sqlite:///{(case / 'gateway.db').as_posix()}"),
        logging=LoggingSettings("WARNING"),
        artifact=ArtifactSettings(storage_root=str(case / "artifacts")),
        inbound_media=InboundMediaSettings(
            enabled=True,
            followup_enabled=True,
            staging_root=str(case / "staging"),
            public_base_url=gateway_origin,
        ),
        host_binding=HostBindingSettings(
            enabled=True,
            dedicated_endpoint_confirmed=True,
            host_id="isolated-official427",
            profile_reference="default",
            service_token_env=SERVICE_ENV,
            encryption_key_env="CF_FILE_RETURN_GRANT",
            runtime_model=model.config["default"],
            runtime_provider="custom",
            runtime_fallback_disabled_confirmed=True,
        ),
        artifact_return=ArtifactReturnSettings(
            enabled=True,
            host_contract_confirmed=True,
            public_base_url=gateway_origin,
            profile_reference="default",
            signing_key_env="CF_FILE_RETURN_SIGNING",
        ),
        worker=WorkerSettings(enabled=True, concurrency=1, retry_limit=0, lease_seconds=60),
    )
    app = create_app(settings)
    requests = []

    async def observed(scope, receive, send):
        async def record(message):
            if scope["type"] == "http" and message["type"] == "http.response.start":
                requests.append(
                    {"method": scope["method"], "path": scope["path"], "status": message["status"]}
                )
            await send(message)

        await app(scope, receive, record)

    server = uvicorn.Server(
        uvicorn.Config(
            observed,
            log_level="error",
            access_log=False,
            loop="asyncio",
            ssl_certfile=str(certificates[1]),
            ssl_keyfile=str(certificates[2]),
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                assert not serving.done()
                await asyncio.sleep(0.02)
        factory = app.state.database_session_factory
        with factory() as session:
            identities = IdentityService(session)
            identity = identities.create_identity(employee_id="isolated-file-return")
            identities.create_mapping(
                platform="wechat",
                account_id=ACCOUNT,
                sender_id=CHAT,
                enterprise_identity_id=identity.id,
            )
            access = AccessPolicyService(session)
            access.upsert_user_policy(enterprise_identity_id=identity.id, enabled=True)
            access.upsert_gateway_policy(enabled=True, allowed_risk_levels={RiskLevel.NORMAL})
        index = 0

        def admit(label, kind="text", *, attachment_id=None, action=None):
            nonlocal index
            index += 1
            message = NormalizedWechatMessage.model_validate(
                {
                    "source_account_id": ACCOUNT,
                    "source_message_id": str(index),
                    "source_local_id": str(index),
                    "source_server_id": str(index),
                    "source_message_id_is_fallback": False,
                    "event_id": "isolated:" + str(index),
                    "conversation_id": CHAT,
                    "conversation_type": "private",
                    "conversation_name": "synthetic",
                    "sender_type": "human",
                    "sender_id": CHAT,
                    "sender_name": "synthetic",
                    "message_type": "text"
                    if kind == "text"
                    else "image"
                    if kind == "png"
                    else "file",
                    "raw_type": 1 if kind == "text" else 3 if kind == "png" else 49,
                    "content": (
                        f"/attachment {attachment_id} {action}\n"
                        if attachment_id is not None
                        else ""
                    )
                    + "file-return-case:"
                    + label
                    + " Read the original and return unchanged bytes to the current chat.",
                    "timestamp": datetime.now(UTC),
                    "is_mentioned": None,
                    "is_self": False,
                    "reply": None,
                }
            )
            with factory() as session:
                result = MessageAdmissionService(
                    session, inbound_media=settings.inbound_media
                ).process(message)
                assert result.dispatch_record_id
                return result

        fixtures = load(
            "cf_file_return_fixtures",
            args.filebridge_source
            / "integrations/hermes-filebridge/tests/inbound_content_fixtures.py",
        )
        import struct
        import zlib

        def chunk(name, data):
            return (
                struct.pack(">I", len(data))
                + name
                + data
                + struct.pack(">I", zlib.crc32(name + data))
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0"))
            + chunk(b"IEND", b"")
        )
        content = {"pdf": fixtures.pdf_fixture(), "png": png}

        class MediaFetch:
            def fetch(self, source):
                kind = "png" if source.raw_type == 3 else "pdf"
                return BoundMediaResult(
                    source.fingerprint,
                    parse_inbound_media(
                        {
                            "type": "image" if kind == "png" else "file",
                            "filename": PDF_FILENAME if kind == "pdf" else "original.png",
                            "data": base64.b64encode(content[kind]).decode(),
                        }
                    ),
                )

        intake = InboundMediaWorker(factory, MediaFetch(), InboundMediaStaging(case / "staging"))
        receiver = delivery_peer.WechatReceiver(ACCOUNT)
        return_context_observations = []

        class ObservedHermesClient(HermesClient):
            def chat(self, content, **options):
                # Observe one boolean without changing the real TLS request or
                # recording authorization/session/body content in the summary.
                return_context_observations.append(
                    options.get("artifact_return_context") is not None
                )
                return super().chat(content, **options)

        def row_snapshot(row):
            return {column.name: getattr(row, column.name) for column in row.__table__.columns}

        with factory() as session:
            assert (
                session.execute(sql_text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20261018_01"
            )
        original_pdf = None
        with official_host(args, case, model, gateway_origin, certificates, keys) as origin:  # noqa: SIM117
            with ObservedHermesClient(
                origin,
                keys["hermes"],
                model.config["default"],
                ca_file=str(certificates[0]),
                timeout=httpx.Timeout(90, connect=5, read=90, write=5, pool=5),
            ) as client:

                def dispatcher():
                    return build_dispatch_worker(
                        settings, session_factory=factory, hermes_client=client, sender_factory=None
                    )

                async def execute(item):
                    result = await asyncio.to_thread(dispatcher().run_once)
                    assert result is not None and result.status.value == "success", (
                        "real dispatch failed"
                    )
                    with factory() as session:
                        row = session.get(HermesDispatchRecord, item.dispatch_record_id)
                        assert row.attempt_count == 1

                async def deliver():
                    with delivery_peer.protocol_peer(receiver) as peer:
                        while (
                            result := await asyncio.to_thread(
                                delivery_peer.deliver, settings, factory, peer
                            )
                        ) is not None:
                            assert result.status.value == "delivered"

                instruction = admit("instruction")
                await execute(instruction)
                await deliver()
                settings = replace(
                    settings,
                    host_binding=replace(
                        settings.host_binding, fresh_session_thread_ids=(instruction.ai_thread_id,)
                    ),
                )
                app.state.settings = settings
                reports = []
                for kind in ("pdf", "png"):
                    item = admit(kind, kind)
                    assert await asyncio.to_thread(intake.run_once) == "ready"
                    await execute(item)
                    await deliver()
                    with factory() as session:
                        job = session.scalar(
                            select(InboundMediaJob).where(
                                InboundMediaJob.message_id == item.message_id
                            )
                        )
                        binding = session.scalar(
                            select(InboundHostBinding).where(
                                InboundHostBinding.dispatch_id == item.dispatch_record_id
                            )
                        )
                        response = session.scalar(
                            select(ResponseRecord).where(
                                ResponseRecord.message_id == item.message_id
                            )
                        )
                        delivery = session.scalar(
                            select(DeliveryOutboxRecord).where(
                                DeliveryOutboxRecord.response_id == response.response_id
                            )
                        )
                        artifacts = list(session.scalars(select(Artifact)))
                        assert len(artifacts) == len(reports) + 1
                        assert binding.execution_policy == "fresh_runtime_v1" and binding.closed_at
                        assert binding.grant_ciphertext is None and job.read_token_hash is None
                        assert (
                            session.get(AIThread, item.ai_thread_id).hermes_thread_id
                            == binding.session_id
                        )
                        assert delivery.status.value == "delivered" and delivery.attempt_count == 1
                        expected = content[kind]
                        artifact = next(
                            value for value in artifacts if value.sha256 == sha(expected)
                        )
                        assert (
                            ArtifactRepository(session, settings.artifact.storage_root).read(
                                artifact.artifact_id
                            )
                            == expected
                        )
                        media_kind = "file" if kind == "pdf" else "image"
                        sent = next(
                            value[media_kind] for value in receiver.requests if media_kind in value
                        )
                        assert base64.b64decode(sent["data"], validate=True) == expected
                        if kind == "pdf":
                            assert artifact.filename == PDF_FILENAME
                            verify_received_pdf(sent, expected)
                        assert all(value["chatId"] == CHAT for value in receiver.requests)
                        reports.append(
                            {
                                "kind": kind,
                                "bytes": len(expected),
                                "sha256": sha(expected),
                                "dispatch_attempts": 1,
                                "delivery_attempts": 1,
                                "artifact_ready": True,
                                "receiver_bytes_equal": True,
                                "receiver_filename_equal": True if kind == "pdf" else None,
                                "binding_closed": True,
                            }
                        )
                    if kind == "pdf":
                        with factory() as session:
                            original_job = session.scalar(
                                select(InboundMediaJob).where(
                                    InboundMediaJob.message_id == item.message_id
                                )
                            )
                            original_binding = session.scalar(
                                select(InboundHostBinding).where(
                                    InboundHostBinding.dispatch_id == item.dispatch_record_id
                                )
                            )
                            original_pdf = {
                                "job": row_snapshot(original_job),
                                "binding": row_snapshot(original_binding),
                                "artifact_id": artifact.artifact_id,
                                "response_id": response.response_id,
                                "delivery_id": delivery.id,
                                "records": [
                                    (
                                        Attachment,
                                        original_job.attachment_id,
                                        row_snapshot(
                                            session.get(Attachment, original_job.attachment_id)
                                        ),
                                    ),
                                    (
                                        Message,
                                        item.message_id,
                                        row_snapshot(session.get(Message, item.message_id)),
                                    ),
                                    (
                                        HermesDispatchRecord,
                                        item.dispatch_record_id,
                                        row_snapshot(
                                            session.get(
                                                HermesDispatchRecord, item.dispatch_record_id
                                            )
                                        ),
                                    ),
                                    (
                                        Artifact,
                                        artifact.artifact_id,
                                        row_snapshot(session.get(Artifact, artifact.artifact_id)),
                                    ),
                                    (
                                        ResponseRecord,
                                        response.response_id,
                                        row_snapshot(
                                            session.get(ResponseRecord, response.response_id)
                                        ),
                                    ),
                                    (
                                        DeliveryOutboxRecord,
                                        delivery.id,
                                        row_snapshot(
                                            session.get(DeliveryOutboxRecord, delivery.id)
                                        ),
                                    ),
                                ],
                            }
                        with factory() as session:
                            before_tip = session.get(AIThread, item.ai_thread_id).hermes_thread_id
                        followup = admit("followup")
                        assert followup.ai_thread_id == item.ai_thread_id
                        await execute(followup)
                        await deliver()
                        with factory() as session:
                            assert (
                                session.get(AIThread, item.ai_thread_id).hermes_thread_id
                                == before_tip
                            )
                            assert len(list(session.scalars(select(Artifact)))) == 1
                # An old file_ref is never sufficient in a subsequent official
                # request, even though the model retained it from actual output.
                replay = admit("replay")
                await execute(replay)
                await deliver()
                with factory() as session:
                    assert len(list(session.scalars(select(Artifact)))) == 2
                    assert len(list(session.scalars(select(DeliveryReceipt)))) >= 4
                reuse_reports = []
                reused_tasks = {original_pdf["binding"]["task_id"]}
                reused_sessions = {original_pdf["binding"]["session_id"]}

                async def reuse(label, action):
                    assert original_pdf is not None
                    prior_artifacts = None
                    with factory() as session:
                        prior_artifacts = set(session.scalars(select(Artifact.artifact_id)))
                        prior_tip = session.get(AIThread, instruction.ai_thread_id).hermes_thread_id
                    item = admit(
                        label, attachment_id=original_pdf["job"]["attachment_id"], action=action
                    )
                    assert item.message_id != original_pdf["job"]["message_id"]
                    with factory() as session:
                        job = session.scalar(
                            select(InboundMediaJob).where(
                                InboundMediaJob.message_id == item.message_id
                            )
                        )
                        assert job.source_job_id == original_pdf["job"]["id"]
                        assert job.attachment_id == original_pdf["job"]["attachment_id"]
                        assert job.followup_action == action and job.state == "ready"
                        assert job.read_token_hash is None
                    before_requests = len(receiver.requests)
                    before_contexts = len(return_context_observations)
                    await execute(item)
                    await deliver()
                    assert return_context_observations[before_contexts:] == [
                        action == "read-return"
                    ]
                    with factory() as session:
                        old_job = session.get(InboundMediaJob, original_pdf["job"]["id"])
                        old_binding = session.get(InboundHostBinding, original_pdf["binding"]["id"])
                        assert row_snapshot(old_job) == original_pdf["job"]
                        assert row_snapshot(old_binding) == original_pdf["binding"]
                        for row_type, row_id, before in original_pdf["records"]:
                            assert row_snapshot(session.get(row_type, row_id)) == before
                        assert session.get(Artifact, original_pdf["artifact_id"]).sha256 == sha(
                            content["pdf"]
                        )
                        assert (
                            session.get(ResponseRecord, original_pdf["response_id"]).status.value
                            == "delivered"
                        )
                        assert (
                            session.get(
                                DeliveryOutboxRecord, original_pdf["delivery_id"]
                            ).status.value
                            == "delivered"
                        )
                        binding = session.scalar(
                            select(InboundHostBinding).where(
                                InboundHostBinding.dispatch_id == item.dispatch_record_id
                            )
                        )
                        assert binding.session_id != original_pdf["binding"]["session_id"]
                        assert binding.parent_session_id == prior_tip
                        assert binding.task_id not in reused_tasks
                        assert binding.session_id not in reused_sessions
                        reused_tasks.add(binding.task_id)
                        reused_sessions.add(binding.session_id)
                        assert binding.closed_at and binding.grant_ciphertext is None
                        response = session.scalar(
                            select(ResponseRecord).where(
                                ResponseRecord.message_id == item.message_id
                            )
                        )
                        delivery = session.scalar(
                            select(DeliveryOutboxRecord).where(
                                DeliveryOutboxRecord.response_id == response.response_id
                            )
                        )
                        assert delivery.status.value == "delivered" and delivery.attempt_count == 1
                        new_artifacts = (
                            set(session.scalars(select(Artifact.artifact_id))) - prior_artifacts
                        )
                        assert len(new_artifacts) == (1 if action == "read-return" else 0)
                        if action == "read-return":
                            artifact_id = new_artifacts.pop()
                            assert (
                                ArtifactRepository(session, settings.artifact.storage_root).read(
                                    artifact_id
                                )
                                == content["pdf"]
                            )
                            emitted = [
                                request["file"]
                                for request in receiver.requests[before_requests:]
                                if "file" in request
                            ]
                            assert len(emitted) == 1
                            verify_received_pdf(emitted[0], content["pdf"])
                        else:
                            assert not any(
                                "file" in request or "image" in request
                                for request in receiver.requests[before_requests:]
                            )
                    reuse_reports.append(
                        {
                            "operation": label,
                            "action": action,
                            "new_dispatch": True,
                            "same_attachment": True,
                            "new_task_and_session": True,
                            "original_completed_job_and_lease_unchanged": True,
                            "new_artifact": action == "read-return",
                            "receiver_filename_equal": True if action == "read-return" else None,
                            "delivered": True,
                            "return_context_present": action == "read-return",
                        }
                    )

                await reuse("reuse-pdf", "read-return")

        # A real process shutdown/start on the same isolated HOME, not merely
        # another HTTP client. Gateway remains the same process; replace only
        # its SQLAlchemy session factory to prove reads from durable state.
        from cf_agent_gateway.database import create_database_session_factory

        old_factory = factory
        factory = create_database_session_factory(app.state.database_engine)
        assert factory is not old_factory
        app.state.database_session_factory = factory
        with (
            official_host(
                args, case, model, gateway_origin, certificates, keys, generation=1
            ) as origin,
            ObservedHermesClient(
                origin,
                keys["hermes"],
                model.config["default"],
                ca_file=str(certificates[0]),
                timeout=httpx.Timeout(90, connect=5, read=90, write=5, pool=5),
            ) as client,
        ):
            await reuse("reuse-pdf-after-restart", "read-return")
            await reuse("reuse-read", "read")
        first_ready = json.loads(host_record(case, "ready.json", 0).read_text())
        second_ready = json.loads(host_record(case, "ready.json", 1).read_text())
        assert first_ready["pid"] != second_ready["pid"]
        assert json.loads(host_record(case, "host-ended.json", 1).read_text())["closed"]
        assert model.planner.read_only_verified
        repeated = [
            value
            for (label, _aid), value in model.planner.prepared.items()
            if label in {"pdf", "reuse-pdf", "reuse-pdf-after-restart"}
        ]
        assert len(repeated) == 3 and len({value["file_ref"] for value in repeated}) == 3
        assert not model.errors
        assert model.planner.pdf_text_seen and model.planner.images
        assert any(
            base64.b64decode(value.split(",", 1)[1]) == content["png"]
            for value in model.planner.images
        )
        assert model.planner.repeated_descriptors_equal
        assert set(model.planner.schema_hashes) == {
            "filebrowser_read_inbound",
            "cf_return_current_chat",
        }
        assert (
            sum(row["status"] == 200 and row["path"].endswith("/content") for row in requests) == 5
        )
        return {
            "ok": True,
            "chain": "linux-real-gateway-official-hermes-filebridge",
            "hermes_commit": HERMES,
            "cases": reports,
            "existing_attachment_followups": reuse_reports,
            "hermes_process_restarted_with_same_home": True,
            "gateway_session_factory_recreated": True,
            "gateway_process_restarted": False,
            "schema_head": "20261018_01",
            "read_without_return_context": True,
            "schema_sha256": model.planner.schema_hashes,
            "normal_followup_text_same_managed_tip": True,
            "pdf_content_in_model_http": True,
            "png_bytes_in_model_http": True,
            "repeat_preparation_same_descriptor": True,
            "expired_session_reference_rejected": True,
            "unrelated_attachment_rejected": True,
            "gateway_is_real": True,
            "wechat_is_substitute": True,
            "model_is_deterministic_loopback": True,
        }
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 15)
        listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--hermes-python", type=Path)
    parser.add_argument("--filebridge-source", type=Path)
    parser.add_argument("--filebridge-commit")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-dirty-source", action="store_true")
    args = parser.parse_args()
    if args.host:
        try:
            asyncio.run(host())
        except BaseException as error:
            case = Path(_HOST_PAYLOAD["case"])
            save(
                host_record(case, "host-failure.json", _HOST_PAYLOAD.get("generation", 0)),
                {
                    "class": type(error).__name__,
                    "frames": [
                        {"file": Path(f.filename).name, "line": f.lineno}
                        for f in traceback.extract_tb(error.__traceback__)
                    ],
                },
            )
            raise SystemExit(1) from None
        return
    if not all(
        (
            args.hermes_source,
            args.hermes_python,
            args.filebridge_source,
            args.filebridge_commit,
            args.worker,
            args.output,
        )
    ):
        parser.error("explicit source/interpreter/worker/output arguments are required")
    if sys.platform != "linux":
        parser.error("full Gateway chain requires Linux; use the separate Windows component probe")
    for name in ("hermes_source", "filebridge_source", "worker", "output"):
        setattr(args, name, getattr(args, name).resolve())
    args.hermes_python = args.hermes_python.absolute()  # preserve venv symlink
    original = load("cf_file_return_verifier", REPO / "tests/hermes_return_probe/probe.py")
    original.verify_source(args.hermes_source, HERMES)
    proof = source_inventory(
        args.filebridge_source,
        args.filebridge_commit,
        (
            "integrations/hermes-filebridge/plugin",
            "integrations/hermes-filebridge/tests/test_gateway_hermes_joint.py",
            "integrations/hermes-filebridge/tests/hermes_probe_support.py",
            "integrations/hermes-filebridge/tests/inbound_content_fixtures.py",
        ),
        allow_dirty=args.allow_dirty_source,
    )
    case = args.output
    case.mkdir(mode=0o700)
    for name in (
        "profile",
        "staging",
        "return-work",
        "inbound-work",
        "empty-bundled",
        "tmp",
        "user",
        "certificates",
    ):
        (case / name).mkdir(mode=0o700)
    save(
        case / "source-proof.json",
        {
            "filebridge": proof,
            "worker_sha256": sha(args.worker.read_bytes()),
            "gateway_files": source_inventory(
                REPO,
                subprocess.run(
                    ["git", "-C", str(REPO), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                ("src/cf_agent_gateway",),
                allow_dirty=args.allow_dirty_source,
            ),
        },
    )
    environment = original.isolated_environment(case)
    environment["HERMES_HOME"] = str(case / "profile")
    os.environ.clear()
    os.environ.update(environment)
    keys = {name: secrets.token_hex(32) for name in ("host", "hermes", "signing", "api")}
    keys["grant"] = base64.b64encode(secrets.token_bytes(32)).decode()
    os.environ.update(
        {
            SERVICE_ENV: keys["host"],
            "CF_FILE_RETURN_GRANT": keys["grant"],
            "CF_FILE_RETURN_SIGNING": keys["signing"],
            "CF_GATEWAY_API_TOKEN": keys["api"],
        }
    )
    sys.path[:0] = [str(REPO / "tests"), str(REPO / "src")]
    from test_hermes_tls import _test_certificates

    model_module = load("cf_file_return_model", HERE / "model.py")
    model = model_module.ModelServer()
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    report = None
    try:
        report = asyncio.run(
            run_linux(args, case, model, _test_certificates(case / "certificates"), keys)
        )
    except BaseException as error:
        report = {
            "ok": False,
            "class": type(error).__name__,
            "model_errors": model.errors,
            "frames": [
                {"file": Path(f.filename).name, "line": f.lineno}
                for f in traceback.extract_tb(error.__traceback__)
            ],
        }
    finally:
        model.planner.lease_release.set()
        model.shutdown()
        model.server_close()
        model_thread.join(5)
        # CI exports only this allowlisted summary, never model requests or DB.
        protected = [value.encode() for value in keys.values()]
        leak_paths = [
            path.name
            for path in case.rglob("*")
            if path.is_file()
            and path.name not in {"key.pem"}
            and any(value in path.read_bytes() for value in protected)
        ]
        report["credential_leak_file_count"] = len(leak_paths)
        report["ok"] = report["ok"] and not leak_paths
        save(case / "safe-summary.json", report)
    print(json.dumps(report))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
