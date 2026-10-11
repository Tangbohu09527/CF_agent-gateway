"""Pinned official CLI/listener probe. Synthetic profiles, GET /v1/models only.

Explicit integration driver, never executed during pytest collection. Windows
uses the actual official detached gateway start/stop management implementation.
Linux uses official gateway run/stop (no system service is installed). Every
child inherits a restrictive audit hook; no external connection or real profile
read/write is permitted. No model request, attachment, or WeChat request exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

COMMIT = "0f4a98f87c17007b81500239d0bd5b9574027b73"
BLOBS = {
    "gateway/config.py": "f1cfcf914cad78f2085cd468eed1cd6d401271d1",
    "gateway/config_env.py": "9f171ca2ee21fdc1dc46683bdab5c552775a58d3",
    "gateway/config_loader.py": "e367209fa55cca8286e700843d7d3319d5479c75",
    "gateway/platforms/api_server.py": "5bd5e91fa2296f3c150a87845c047a6a69933c71",
    "gateway/run.py": "6c9556d10f248c3104488a9b183034b8094c27e0",
    "hermes_cli/env_loader.py": "398b108bf4f4d528fc4aa4f41e33d1b8e6e1c794",
    "hermes_cli/gateway.py": "82723f9afc633859b063f2376f91340a1f51d75f",
    "hermes_cli/gateway_windows.py": "83f607a3762914c63e42b043a1def10eca4365fc",
    "hermes_cli/main.py": "3007cd9c4d5e3bf56cfa424a79d7d51649c852cb",
    "hermes_cli/plugins.py": "bba82a3b4b0a394f69fbb2a54cc5a94f2a1303e5",
    "agent/secret_scope.py": "ca7f9357c1312171a3b0c28d8f053b874ed469b3",
    "agent/ssl_verify.py": "251513708d564725dc1a8f347fbe50e640d1441e",
}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def verify(source):
    assert (
        subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        == COMMIT
    )
    assert not subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True
    ).strip()
    for name, expected in BLOBS.items():
        raw = (source / name).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        assert actual == expected, f"Pinned official source mismatch: {name}"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def audit_bootstrap():
    # sitecustomize normally logs exceptions then continues; this must fail closed.
    return (
        "import os, sys\ntry:\n    sys.path.insert(0, "
        + repr(str(Path(__file__).parent.resolve()))
        + ")\n    from probe import install_audit\n    install_audit()\n"
        + "except BaseException:\n    os._exit(97)\n"
    )


def environment(case, source, python, repository):
    env = {
        k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT") if k in os.environ
    }
    env.update(
        {
            "HERMES_HOME": str(case / "home"),
            "HOME": str(case / "user"),
            "USERPROFILE": str(case / "user"),
            "APPDATA": str(case / "user/AppData"),
            "LOCALAPPDATA": str(case / "user/LocalAppData"),
            "TEMP": str(case / "tmp"),
            "TMP": str(case / "tmp"),
            "TMPDIR": str(case / "tmp"),
            "XDG_CONFIG_HOME": str(case / "user/config"),
            "HERMES_BUNDLED_PLUGINS": str(case / "empty-bundled"),
            "HERMES_ENABLE_PROJECT_PLUGINS": "false",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_GATEWAY_INSTALL_START_ON_LOGIN": "false",
            "HERMES_NONINTERACTIVE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(case / "audit"), str(source), str(repository / "src"))
            ),
            "PATH": os.pathsep.join(
                (str(python.parent), str(Path(env.get("SYSTEMROOT", "/")) / "System32"))
            ),
            "CF_LISTENER_PROBE_CASE": str(case),
            "CF_LISTENER_PROBE_SOURCE": str(source),
            "CF_LISTENER_PROBE_REPOSITORY": str(repository),
        }
    )
    return env


def install_audit():
    """Used by sitecustomize in both the management CLI and detached child."""
    case = Path(os.environ["CF_LISTENER_PROBE_CASE"]).resolve()
    source = Path(os.environ["CF_LISTENER_PROBE_SOURCE"]).resolve()
    repository = Path(os.environ["CF_LISTENER_PROBE_REPOSITORY"]).resolve()
    readable = [
        case,
        source,
        repository / "src",
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path(__file__).parent.resolve(),
    ]
    # Windows Python DLL imports/locale and POSIX OS release are read-only runtime metadata.
    if os.name == "nt":
        readable.append(Path(os.environ["SYSTEMROOT"]).resolve())
    os_metadata = {
        Path("/etc/os-release"),
        Path("/usr/lib/os-release"),
        Path("/etc/localtime"),
        Path("/dev/null"),
    }
    # The audit hook outlives this function and must keep its stream until process exit.
    events = open(  # noqa: SIM115
        case / ("audit-" + str(os.getpid()) + ".jsonl"), "a", encoding="utf-8"
    )

    def record(kind, **values):
        events.write(json.dumps({"event": kind, "pid": os.getpid(), **values}) + "\n")
        events.flush()

    def inside(path, roots):
        return any(path == root or root in path.parents for root in roots)

    def audit(event, args):
        if event == "open" and not isinstance(args[0], int):
            path = Path(args[0]).resolve()
            if os.fsdecode(args[0]).lower() == os.devnull.lower():
                return
            flags = args[2] or 0
            mode = args[1] or ""
            writing = any(x in mode for x in "wax+") or bool(flags & (os.O_WRONLY | os.O_RDWR))
            if writing and not inside(path, [case]):
                raise PermissionError("Probe refuses writes outside the synthetic case")
            if not writing and not inside(path, readable) and path not in os_metadata:
                raise PermissionError("Probe refuses reads outside approved source/runtime/case")
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.replace"}:
            targets = args[:2] if event in {"os.rename", "os.replace"} else args[:1]
            for target in targets:
                if isinstance(target, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(target)).resolve()
                    if not inside(path, [case]):
                        raise PermissionError("Probe refuses filesystem mutation outside case")
        if event == "socket.connect":
            destination = args[1]
            # Runtime never needs outbound networking. The parent does the readonly HTTP probes.
            if isinstance(destination, tuple):
                caller = sys._getframe(1)
                fallback = getattr(socket, "_fallback_socketpair", None)
                if (
                    fallback is not None
                    and caller.f_code is fallback.__code__
                    and destination[0] in {"127.0.0.1", "::1"}
                ):
                    return  # CPython Windows asyncio wakeup pair, no HTTP/model transport.
                record("outbound_connection_blocked")
                raise PermissionError("Probe runtime refuses every outbound network connection")
        if event == "socket.bind" and isinstance(args[1], tuple):
            host, bound_port = args[1][:2]
            allowed = json.loads((case / "ports.json").read_text())
            if host not in {"127.0.0.1", "0.0.0.0", "::1"} or bound_port not in (*allowed, 0):
                raise PermissionError("Probe refuses unrelated listener")
            record("socket_bind", host=host, port=bound_port)
        if event == "subprocess.Popen":
            argv = args[1]
            if isinstance(argv, str):
                if os.name != "nt":
                    raise PermissionError("Probe refuses shell command strings")
                # Windows Popen audits the quoted CreateProcess command even for list argv.
                import ctypes
                from ctypes import wintypes

                count = ctypes.c_int()
                split = ctypes.windll.shell32.CommandLineToArgvW
                split.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
                split.restype = ctypes.POINTER(wintypes.LPWSTR)
                pointer = split(argv, ctypes.byref(count))
                try:
                    argv = [pointer[i] for i in range(count.value)]
                finally:
                    free = ctypes.windll.kernel32.LocalFree
                    free.argtypes = [wintypes.HLOCAL]
                    free.restype = wintypes.HLOCAL
                    free(pointer)
            words = [str(x) for x in argv]
            executable = Path(words[0]).name.lower()
            official_child = executable in {
                "python",
                "python.exe",
                "python3",
                "python3.14",
            } and words[1:] == ["-m", "hermes_cli.main", "gateway", "run"]
            # Read-only discovery of the synthetic home-hashed task; never install/reconcile it.
            task_query = executable in {"schtasks", "schtasks.exe"} and "/Query" in words
            if not (official_child or task_query):
                raise PermissionError("Probe refuses non-management subprocess")
            if official_child:
                child_env = args[3]
                assert child_env["HERMES_HOME"] == str(case / "home")
                assert str(case / "audit") in child_env["PYTHONPATH"]
                record("official_management_spawn")
        if event == "os.system":
            raise PermissionError("Probe refuses shell commands")

    sys.addaudithook(audit)
    record("audit_active", python=sys.version.split()[0])


def cli(python, env, case, action, label):
    with (case / f"{label}.log").open("wb") as output:
        result = subprocess.run(
            [str(python), "-B", "-m", "hermes_cli.main", "gateway", action],
            env=env,
            cwd=case,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=100,
        )
    assert result.returncode == 0, (
        f"Official management {action} failed; inspect private {label}.log"
    )


def identity(case):
    import psutil

    raw = json.loads((case / "home/gateway.pid").read_text(encoding="utf-8-sig"))
    pid = raw["pid"] if isinstance(raw, dict) else raw
    process = psutil.Process(pid)
    assert str(case / "home") == process.environ()["HERMES_HOME"]
    return {"pid": pid, "created": process.create_time(), "executable": process.exe()}


def listeners(who):
    import psutil

    process = psutil.Process(who["pid"])
    assert process.create_time() == who["created"] and process.exe() == who["executable"]
    return sorted(
        (item.laddr.ip, item.laddr.port)
        for item in process.net_connections("inet")
        if item.status == psutil.CONN_LISTEN
    )


def wait_for(check, timeout=45):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, KeyError, AssertionError) as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError("Official listener readiness timed out") from last


def run(source, python, output):
    import httpx
    import psutil

    repository = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repository / "src"), str(repository / "tests")]
    from test_hermes_tls import _test_certificates

    from cf_agent_gateway.hermes.return_bridge.enablement import apply_plan, plan_hermes

    verify(source)
    case = output.resolve()
    case.mkdir()  # Evidence is append-only across attempts: never reuse a previous case.
    for name in ("home", "user", "tmp", "empty-bundled", "work", "certificates", "audit"):
        (case / name).mkdir()
    native_port, tls_port = port(), port()
    assert native_port != tls_port
    write_json(case / "ports.json", [native_port, tls_port])
    ca, cert, keyfile = _test_certificates(case / "certificates")
    (case / "untrusted-certificates").mkdir()
    wrong_ca = _test_certificates(case / "untrusted-certificates")[0]
    service_key = secrets.token_hex(32)
    untouched = secrets.token_hex(32)
    env_file = case / "home/.env"
    original = (
        "\ufeffAPI_SERVER_HOST=0.0.0.0\r\n"
        f"API_SERVER_PORT={native_port}\r\n"
        f"API_SERVER_KEY={service_key}\r\n# preserve comment and synthetic credential\r\n"
        f"UNRELATED_PROBE_SECRET='{untouched}'\r\n"
    ).encode()
    env_file.write_bytes(original)
    settings = {
        "gateway_origin": "https://localhost:1",
        "gateway_ca_file": str(ca),
        "work_root": str(case / "work"),
        "profile_reference": "synthetic-listener-probe",
        "profile_revision": 1,
        "tls_host": "127.0.0.1",
        "tls_port": tls_port,
        "tls_cert_file": str(cert),
        "tls_key_file": str(keyfile),
    }
    config = {
        "platforms": {"api_server": {"enabled": True, "host": "127.0.0.1", "port": native_port}},
        "platform_toolsets": {"api_server": []},
        "gateway": {"standalone": True, "multiplex_profiles": False},
        "agent": {"gateway_startup_warmup_timeout": 0, "environment_probe": False},
        "model": {
            "provider": "custom",
            "default": "synthetic-never-called",
            "context_length": 131072,
            "base_url": "http://127.0.0.1:1/v1",
        },
        "memory": {"enabled": False},
        "skills": {"enabled": False},
        "compression": {"enabled": False},
        "checkpoints": {"enabled": False},
    }
    config_path = case / "home/config.yaml"
    write_json(config_path, config)
    plugin = case / "home/plugins/cf-artifact-return"
    plan = plan_hermes(config_path, plugin, settings)
    apply_plan(plan, case / "journal", expected_plan_sha256=plan.summary()["plan_sha256"])
    # Reproduce the original deployment with YAML fixed but the old persistent env untouched.
    # This intentionally restores ONLY our own synthetic .env, never a production/journal file.
    repaired = env_file.read_bytes()
    env_file.write_bytes(original)
    (case / "audit/sitecustomize.py").write_text(audit_bootstrap(), encoding="utf-8")
    env = environment(case, source, python, repository)
    report = {
        "source_commit": COMMIT,
        "source_blobs": BLOBS,
        "model_calls": 0,
        "management": "official Windows gateway start/stop"
        if os.name == "nt"
        else "official foreground gateway run/stop",
        "cycles": [],
    }
    foreground = None

    def start(label):
        nonlocal foreground
        if os.name == "nt":
            cli(python, env, case, "start", label + "-start")
        else:
            log = (case / f"{label}-start.log").open("wb")
            foreground = subprocess.Popen(
                [str(python), "-B", "-m", "hermes_cli.main", "gateway", "run"],
                env=env,
                cwd=case,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            log.close()
        return wait_for(lambda: identity(case))

    def stop(who, label):
        # Reconfirm full process identity and home before invoking the original manager.
        assert identity(case) == who
        cli(python, env, case, "stop", label + "-stop")
        wait_for(lambda: not psutil.pid_exists(who["pid"]))
        if foreground is not None:
            foreground.wait(timeout=10)

    request_observations = []

    def get_models(host, tls, supplied_key, *, ca_file=None):
        context = ssl.create_default_context(cafile=str(ca_file or ca)) if tls else True
        with httpx.Client(verify=context, trust_env=False, timeout=3) as client:
            response = client.get(
                f"{'https' if tls else 'http'}://{host}:"
                f"{tls_port if tls else native_port}/v1/models",
                headers={"Authorization": "Bearer " + supplied_key},
            )
        request_observations.append(
            {"method": "GET", "path": "/v1/models", "tls": tls, "status": response.status_code}
        )
        return response

    def rejects_certificate(host, ca_file):
        try:
            get_models(host, True, service_key, ca_file=ca_file)
        except httpx.ConnectError as exc:
            assert "CERTIFICATE_VERIFY_FAILED" in str(exc), "Unrelated connection failure"
        else:
            raise AssertionError("TLS certificate verification was bypassed")

    who = None
    try:
        who = start("bad-env")
        seen = wait_for(lambda: listeners(who))
        assert ("0.0.0.0", native_port) in seen
        assert all(p != tls_port for _, p in seen)
        assert get_models("127.0.0.1", False, service_key).status_code == 200
        logs = "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in (case / "home/logs").glob("*.log")
        )
        assert "official plaintext listener must be loopback" in logs
        report["bad_env"] = {
            "yaml_host": "127.0.0.1",
            "effective_bind": "0.0.0.0",
            "http_authenticated": True,
            "tls_listener_absent": True,
            "plugin_refusal_observed": True,
            "listeners": seen,
        }
        stop(who, "bad-env")
        who = None
        # Re-plan after the managed stop; a fresh reviewed journal performs the persistent fix.
        fix_plan = plan_hermes(config_path, plugin, settings)
        apply_plan(
            fix_plan, case / "fix-journal", expected_plan_sha256=fix_plan.summary()["plan_sha256"]
        )
        assert env_file.read_bytes() == repaired
        assert (
            original.replace(b"API_SERVER_HOST=0.0.0.0", b"API_SERVER_HOST=127.0.0.1") == repaired
        )
        report["persistent_env_sha256"] = hashlib.sha256(repaired).hexdigest()
        for iteration in range(2):
            label = f"fixed-{iteration + 1}"
            who = start(label)
            seen = wait_for(
                lambda who=who: (
                    found if ("127.0.0.1", tls_port) in (found := listeners(who)) else None
                )
            )
            assert ("127.0.0.1", native_port) in seen
            assert all(host in {"127.0.0.1", "::1"} for host, _ in seen)
            assert get_models("127.0.0.1", False, service_key).status_code == 200
            assert get_models("localhost", True, service_key).status_code == 200
            assert get_models("localhost", True, "wrong-synthetic-key").status_code == 401
            rejects_certificate("127.0.0.1", ca)
            rejects_certificate("localhost", wrong_ca)
            report["cycles"].append(
                {
                    "pid": who["pid"],
                    "created": who["created"],
                    "listeners": seen,
                    "tls_ca_hostname_auth_verified": True,
                    "wrong_auth_rejected": True,
                    "wrong_hostname_rejected": True,
                    "wrong_ca_rejected": True,
                }
            )
            stop(who, label)
            who = None
        assert report["cycles"][0]["pid"] != report["cycles"][1]["pid"]
        assert env_file.read_bytes() == repaired
        audit_records = [
            json.loads(line)
            for path in case.glob("audit-*.jsonl")
            for line in path.read_text().splitlines()
        ]
        assert all(
            cycle["pid"] in {e["pid"] for e in audit_records if e["event"] == "audit_active"}
            for cycle in report["cycles"]
        )
        if os.name == "nt":
            assert len([e for e in audit_records if e["event"] == "official_management_spawn"]) == 3
        assert not any(e["event"] == "outbound_connection_blocked" for e in audit_records)
        report["runtime_outbound_connection_attempts"] = 0
        report["readonly_requests"] = request_observations
        assert all(
            item["method"] == "GET" and item["path"] == "/v1/models"
            for item in request_observations
        )
        scanned = [
            *case.glob("*.log"),
            *case.glob("audit-*.jsonl"),
            *(case / "home/logs").rglob("*.log"),
        ]
        for path in scanned:
            assert service_key.encode() not in path.read_bytes()
            assert untouched.encode() not in path.read_bytes()
        report["logs_credential_scan"] = {"files": len(scanned), "matches": 0}
        report["all_runtime_children_audited"] = True
        assert all(e["python"] == "3.14.7" for e in audit_records if e["event"] == "audit_active")
        report["python"] = "3.14.7"
        report["persistent_restart_verified"] = True
        report["all_probe_processes_stopped"] = True
        verify(source)
        report["ok"] = True
        public = json.dumps(report).encode()
        assert service_key.encode() not in public and untouched.encode() not in public
        write_json(case / "report.json", report)
        print(json.dumps(report))
    finally:
        if who is None:
            with suppress(OSError, ValueError, KeyError, AssertionError, psutil.NoSuchProcess):
                who = identity(case)
        if who is not None:
            stop(who, "failure-cleanup")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", required=True, type=Path)
    parser.add_argument("--hermes-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.hermes_source.resolve(), args.hermes_python.resolve(), args.output)
