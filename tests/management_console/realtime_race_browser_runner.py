"""Own one loopback synthetic candidate and run only the snapshot/live race test."""

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from sqlalchemy import func, select

from cf_agent_gateway.config import DatabaseSettings, ServerSettings, Settings
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.management_console.app import install_console
from cf_agent_gateway.management_console.demo import seed_archive
from cf_agent_gateway.management_console.models import DirectorySource
from cf_agent_gateway.management_console.runtime import build_config
from cf_agent_gateway.management_console.service import ConsoleService
from cf_agent_gateway.task.model.models import HermesDispatchRecord

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("CF_RACE_APP_PORT", "18990"))
PEER_PORT = int(os.environ.get("CF_RACE_PEER_PORT", "18991"))


def serve(runtime):
    config, peer = build_config(runtime, "demo", PEER_PORT, True)
    peer.controls_enabled = True
    peer.chats = [chat for chat in peer.chats if chat["username"] == "wxid_demo_0000"]
    os.environ.setdefault("CF_AGENT_GATEWAY_ADMIN_TOKEN", secrets.token_urlsafe(48))
    os.environ.setdefault("CF_GATEWAY_API_TOKEN", secrets.token_urlsafe(48))
    app = install_console(
        create_app(
            Settings(
                server=ServerSettings(host="127.0.0.1", port=PORT),
                database=DatabaseSettings(url="sqlite:///" + (runtime / "gateway.db").as_posix()),
            )
        ),
        config,
    )
    parent = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with parent(app):
            with app.state.database_session_factory() as session:
                if not session.get(DirectorySource, "demo-primary"):
                    session.add(
                        DirectorySource(
                            id="demo-primary", label="合成竞态测试", status="not_synced"
                        )
                    )
                session.commit()
                seed_archive(session)
                ConsoleService(session, config).sync("demo-primary")
            app.state.console_live.start()
            try:
                yield
            finally:
                app.state.console_live.stop()

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, access_log=False))
    stop_file = runtime / "stop-requested"
    stop_file.unlink(missing_ok=True)
    finished = threading.Event()

    def watch():
        while not finished.wait(0.3):
            if stop_file.exists():
                server.should_exit = True
                return

    peer.thread.start()
    threading.Thread(target=watch, daemon=True).start()
    try:
        server.run()
    finally:
        finished.set()
        peer.shutdown()
        peer.server_close()
        peer.thread.join(5)
        with app.state.database_session_factory() as session:
            summary = {
                "synthetic_data": True,
                "dispatch_records": session.scalar(
                    select(func.count()).select_from(HermesDispatchRecord)
                ),
                "delivery_records": session.scalar(
                    select(func.count()).select_from(DeliveryOutboxRecord)
                ),
                "native_side_effect_calls": [
                    call
                    for call in peer.calls
                    if call["method"] != "GET" and not call["path"].startswith("/_synthetic/")
                ],
                "gateway_stopped": True,
            }
        (runtime / "realtime-race-evidence/peer-summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )


def main():
    runtime = Path(os.environ["CF_CONSOLE_RUNTIME"]).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "realtime-race-evidence").mkdir(exist_ok=True)
    if "--serve" in sys.argv:
        serve(runtime)
        return
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "CF_CONSOLE_ORIGIN": f"http://127.0.0.1:{PORT}",
    }
    result = None
    with (runtime / "candidate.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, __file__, "--serve"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("race_candidate_exited")
                    try:
                        if client.get(env["CF_CONSOLE_ORIGIN"] + "/console").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("race_candidate_readiness_timeout")
            result = subprocess.run(
                [
                    os.environ.get("CF_NODE_EXECUTABLE", "node"),
                    str(ROOT / "tests/management_console/realtime-race-browser.cjs"),
                ],
                cwd=ROOT,
                env=env,
                timeout=100,
                check=False,
            )
        finally:
            (runtime / "stop-requested").touch()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=8)
    summary = json.loads(
        (runtime / "realtime-race-evidence/peer-summary.json").read_text(encoding="utf-8")
    )
    assert summary["dispatch_records"] == summary["delivery_records"] == 0
    assert summary["native_side_effect_calls"] == []
    print(json.dumps(summary))
    raise SystemExit(result.returncode if result else 1)


if __name__ == "__main__":
    main()
