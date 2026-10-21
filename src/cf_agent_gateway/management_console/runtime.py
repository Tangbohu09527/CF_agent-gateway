"""Foreground local candidate or standalone container; all state under explicit runtime."""

import argparse
import getpass
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from cf_agent_gateway.config import DatabaseSettings, ServerSettings, Settings
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.management_console.app import install_console
from cf_agent_gateway.management_console.auth import password_record
from cf_agent_gateway.management_console.config import ConsoleConfig
from cf_agent_gateway.management_console.demo import DemoPeer, seed_archive
from cf_agent_gateway.management_console.models import DirectorySource
from cf_agent_gateway.management_console.service import ConsoleService
from cf_agent_gateway.management_console.upstream import SourceConnection
from cf_agent_gateway.profile_b.hermes import Connection


def build_config(runtime, mode, peer_port, browser_test=False):
    mode_file = runtime / "console-mode.json"
    if mode_file.exists() and json.loads(mode_file.read_text(encoding="utf-8"))["mode"] != mode:
        raise SystemExit("Use a separate runtime/database for demo and real read-only sources.")
    if not mode_file.exists():
        mode_file.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    admin_file = runtime / "admin.json"
    if not admin_file.exists():
        if not browser_test:
            raise SystemExit("Run init-admin first; no default administrator password.")
        username, password = "console-admin", secrets.token_urlsafe(24)
        admin_file.write_text(json.dumps(password_record(username, password)), encoding="utf-8")
        (runtime / "browser-login.json").write_text(
            json.dumps({"username": username, "password": password}), encoding="utf-8"
        )
    admin = json.loads(admin_file.read_text(encoding="utf-8"))
    peer = None
    if mode == "demo":
        token = secrets.token_urlsafe(36)
        peer = DemoPeer(peer_port, token)
        sources = {
            "demo-primary": SourceConnection(
                "演示 Bot · 合成数据", f"http://127.0.0.1:{peer_port}/", token
            )
        }
        connections = {
            "isolated-hermes": Connection(
                url="http://127.0.0.1:18772", token=secrets.token_urlsafe(36)
            )
        }
    else:
        raw = json.loads((runtime / "sources.json").read_text(encoding="utf-8"))
        sources = {
            key: SourceConnection(
                label=value["label"],
                url=value["url"],
                token=os.environ[value["token_env"]],
                archive_url=os.environ.get(value.get("archive_url_env", "")),
            )
            for key, value in raw.items()
        }
        connections = {}
    connection_file = runtime / "hermes-connections.json"
    if connection_file.exists():
        connections = {
            key: Connection(**value)
            for key, value in json.loads(connection_file.read_text(encoding="utf-8")).items()
        }
    return ConsoleConfig(runtime, mode, sources, admin, connections), peer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init-admin", "serve"])
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--mode", choices=["demo", "read_only"], default="demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18770)
    parser.add_argument("--peer-port", type=int, default=18771)
    parser.add_argument("--browser-test", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    if args.action == "init-admin":
        if (runtime / "admin.json").exists():
            raise SystemExit("Administrator exists; preserve current login configuration.")
        username = input("管理员用户名: ").strip()
        password = getpass.getpass("密码（至少12字符，遮蔽输入）: ")
        if password != getpass.getpass("再次输入密码: "):
            raise SystemExit("Passwords do not match")
        (runtime / "admin.json").write_text(
            json.dumps(password_record(username, password)), encoding="utf-8"
        )
        print("Administrator initialized. No password was printed or logged.")
        return
    if args.browser_test and (args.mode != "demo" or args.host != "127.0.0.1"):
        raise SystemExit("Browser-test credentials are restricted to loopback synthetic demo.")
    config, peer = build_config(runtime, args.mode, args.peer_port, args.browser_test)
    os.environ.setdefault("CF_AGENT_GATEWAY_ADMIN_TOKEN", secrets.token_urlsafe(48))
    os.environ.setdefault("CF_GATEWAY_API_TOKEN", secrets.token_urlsafe(48))
    stop_file = runtime / "stop-requested"
    stop_file.unlink(missing_ok=True)
    settings = Settings(
        server=ServerSettings(host=args.host, port=args.port),
        database=DatabaseSettings(url="sqlite:///" + (runtime / "gateway.db").as_posix()),
    )
    app = install_console(create_app(settings), config)

    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def candidate_lifespan(app):
        async with parent_lifespan(app):
            with app.state.database_session_factory() as session:
                for key, value in config.sources.items():
                    if not session.get(DirectorySource, key):
                        session.add(
                            DirectorySource(
                                id=key,
                                label=value.label,
                                status="not_synced",
                                coverage="尚未同步，不代表没有好友。",
                                sync_details={},
                            )
                        )
                session.commit()
                if peer:
                    seed_archive(session)
                    ConsoleService(session, config).sync("demo-primary")
            yield

    app.router.lifespan_context = candidate_lifespan

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, access_log=False))
    finished = threading.Event()

    def stop_watcher():
        while not finished.wait(0.3):
            if stop_file.exists():
                server.should_exit = True
                return

    watcher = threading.Thread(target=stop_watcher, daemon=True)
    try:
        if peer:
            peer.thread.start()
        (runtime / "owned-processes.json").write_text(
            json.dumps(
                {"supervisor": os.getpid(), "children": [], "port": args.port, "mode": args.mode}
            ),
            encoding="utf-8",
        )
        watcher.start()
        server.run()
    finally:
        finished.set()
        if peer:
            peer.shutdown()
            peer.server_close()
            peer.thread.join(timeout=5)
            (runtime / "native-read-calls.json").write_text(
                json.dumps(peer.calls), encoding="utf-8"
            )
        for engine in config._archives.values():
            engine.dispose()
        (runtime / "stopped.json").write_text(
            json.dumps({"supervisor": os.getpid(), "children_stopped": True}), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
