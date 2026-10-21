"""Own all synthetic node-browser processes; never use real credentials or services."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from test_console_nodes import Peer


def main():
    root = Path(__file__).resolve().parents[2]
    runtime = Path(os.environ["CF_CONSOLE_RUNTIME"]).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    env.setdefault("CF_CONSOLE_ORIGIN", "http://127.0.0.1:18870")
    with Peer() as peer, (runtime / "nodes-candidate.log").open("w", encoding="utf-8") as log:
        env["CF_NODE_HERMES_ORIGIN"] = peer.url
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cf_agent_gateway.management_console.runtime",
                "serve",
                "--runtime",
                str(runtime),
                "--mode",
                "demo",
                "--host",
                "127.0.0.1",
                "--port",
                "18870",
                "--peer-port",
                "18871",
                "--browser-test",
            ],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                for _ in range(80):
                    if process.poll() is not None:
                        raise RuntimeError("node_browser_candidate_exited")
                    try:
                        if client.get(env["CF_CONSOLE_ORIGIN"] + "/console").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.25)
                else:
                    raise RuntimeError("node_browser_readiness_timeout")
            subprocess.run(
                [
                    os.environ.get("CF_NODE_EXECUTABLE", "node"),
                    str(root / "tests/management_console/nodes-browser.cjs"),
                ],
                check=True,
                cwd=root,
                env=env,
                timeout=180,
            )
            summary = {
                "synthetic_data": True,
                "model_calls": sum("completions" in p or "prompt" in p for _, p in peer.calls),
                "profile_creates": sum(m == "POST" and p == "/api/profiles" for m, p in peer.calls),
                "profile_count": len(peer.profiles),
                "gateway_stopped": False,
            }
            assert summary["model_calls"] == 0
            expected_creates = 0 if env.get("CF_NODE_SCREENSHOTS_ONLY") == "1" else 1
            assert summary["profile_creates"] == summary["profile_count"] == expected_creates
        finally:
            (runtime / "stop-requested").touch()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=8)
        summary["gateway_stopped"] = process.poll() is not None
        (runtime / "nodes-browser-evidence/peer-summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
