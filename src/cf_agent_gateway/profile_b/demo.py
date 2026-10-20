"""Foreground supervisor for disposable loopback services; Ctrl+C stops owned children."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from cf_agent_gateway.profile_b.demo_peers import ModelServer


def isolated_environment(home: Path, tools: Path, os_home: Path):
    os_home.mkdir(parents=True, exist_ok=True)
    env = {
        k: os.environ[k]
        for k in (
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "LD_LIBRARY_PATH",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "PATH",
            "TEMP",
            "TMP",
        )
        if k in os.environ
    }
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_RUNTIME_DIR": str(tools),
            "HOME": str(os_home),
            "USERPROFILE": str(os_home),
            "LOCALAPPDATA": str(os_home / "AppData/Local"),
            "APPDATA": str(os_home / "AppData/Roaming"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "HERMES_DISABLE_TELEMETRY": "1",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    return env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--node-only", action="store_true")
    parser.add_argument("--web-port", type=int, default=18760)
    parser.add_argument("--model-port", type=int, default=18761)
    parser.add_argument("--node-port", type=int, default=18762)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    stop_file = runtime / "stop-requested"
    stop_file.unlink(missing_ok=True)
    home = runtime / "hermes-home"
    home.mkdir(exist_ok=True)
    secrets_path = runtime / "demo-secrets.json"
    if secrets_path.exists():
        keys = json.loads(secrets_path.read_text(encoding="utf-8"))
    else:
        keys = {k: secrets.token_urlsafe(36) for k in ("admin", "gateway", "hermes")}
        secrets_path.write_text(json.dumps(keys), encoding="utf-8")
    model = ModelServer(args.model_port)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    initial = {
        "model": model.config,
        "models_dev": {"url": f"http://127.0.0.1:{args.model_port}/models-dev.json"},
        "agent": {"max_turns": 5},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
    }
    config = home / "config.yaml"
    if not config.exists():
        config.write_text(json.dumps(initial), encoding="utf-8")
    if not (home / ".env").exists():
        (home / ".env").write_text("# Isolated synthetic credentials only\n", encoding="utf-8")
    connections = {
        "isolated-hermes": {
            "url": f"http://127.0.0.1:{args.node_port}",
            "token": keys["hermes"],
            "initial_config": initial,
        },
        "unreachable-test-node": {
            "url": "http://127.0.0.1:18769",
            "token": secrets.token_urlsafe(36),
        },
    }
    (runtime / "connections.json").write_text(json.dumps(connections), encoding="utf-8")
    env = isolated_environment(home, runtime / "hermes-tools", runtime / "os-home")
    env["HERMES_DASHBOARD_SESSION_TOKEN"] = keys["hermes"]
    log = (runtime / "hermes.log").open("a", encoding="utf-8")
    children = []
    try:
        child = subprocess.Popen(
            [
                str(args.hermes_python.absolute()),
                "-B",
                "-m",
                "hermes_cli.main",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.node_port),
                "--isolated",
            ],
            cwd=args.hermes_source.resolve(),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        children.append(child)
        client = httpx.Client(trust_env=False, timeout=2)
        for _ in range(120):
            if child.poll() is not None:
                raise RuntimeError("official Hermes exited; inspect isolated hermes.log")
            try:
                response = client.get(
                    connections["isolated-hermes"]["url"] + "/api/profiles",
                    headers={"Authorization": "Bearer " + keys["hermes"]},
                )
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("official Hermes readiness timeout")
        client.close()
        if not args.node_only:
            web_env = dict(env)
            web_env.update(
                {
                    "CF_PROFILE_B_RUNTIME": str(runtime),
                    "CF_PROFILE_B_WEB_PORT": str(args.web_port),
                    "CF_AGENT_GATEWAY_ADMIN_TOKEN": keys["admin"],
                    "CF_GATEWAY_API_TOKEN": keys["gateway"],
                }
            )
            web_log = (runtime / "gateway.log").open("a", encoding="utf-8")
            web = subprocess.Popen(
                [sys.executable, "-m", "cf_agent_gateway.profile_b.app"],
                env=web_env,
                stdout=web_log,
                stderr=subprocess.STDOUT,
            )
            children.append(web)
            with httpx.Client(trust_env=False, timeout=2) as web_client:
                for _ in range(90):
                    if web.poll() is not None:
                        raise RuntimeError("candidate Gateway exited; inspect isolated gateway.log")
                    try:
                        ready = web_client.get(
                            f"http://127.0.0.1:{args.web_port}/admin/profile-b/state",
                            headers={"Authorization": "Bearer " + keys["admin"]},
                        )
                        if ready.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("candidate Gateway readiness timeout")
        (runtime / "owned-processes.json").write_text(
            json.dumps(
                {
                    "supervisor": os.getpid(),
                    "children": [{"pid": p.pid, "args": p.args} for p in children],
                    "ports": {
                        "web": args.web_port,
                        "model": args.model_port,
                        "hermes": args.node_port,
                    },
                }
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "ready": True,
                    "node_only": args.node_only,
                    "web": f"http://127.0.0.1:{args.web_port}/manage",
                    "test_login_file": str(secrets_path),
                    "stop": "Ctrl+C in this foreground supervisor",
                }
            ),
            flush=True,
        )
        while not stop_file.exists() and all(p.poll() is None for p in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)
        (runtime / "model-evidence.json").write_text(
            json.dumps(model.calls, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (runtime / "stopped.json").write_text(
            json.dumps(
                {
                    "supervisor": os.getpid(),
                    "children_stopped": all(p.poll() is not None for p in children),
                }
            ),
            encoding="utf-8",
        )
        log.close()


if __name__ == "__main__":
    main()
