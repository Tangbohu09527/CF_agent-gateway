"""Run the actual page and native evidence with bounded owned processes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--hermes-python", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "supervisor.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cf_agent_gateway.profile_b.demo",
                "--runtime",
                str(runtime),
                "--hermes-source",
                str(args.hermes_source.resolve()),
                "--hermes-python",
                str(args.hermes_python.resolve()),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                for _ in range(180):
                    if process.poll() is not None:
                        raise RuntimeError("supervisor exited")
                    try:
                        if client.get("http://127.0.0.1:18760/manage").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("candidate readiness timeout")
            env = {**os.environ, "CF_PROFILE_B_RUNTIME": str(runtime)}
            subprocess.run(
                ["node", "tests/profile_b/browser.cjs"], env=env, check=True, timeout=300
            )
            subprocess.run(
                [sys.executable, "tests/profile_b/verify_native.py", "--runtime", str(runtime)],
                check=True,
                timeout=300,
            )
        finally:
            (runtime / "stop-requested").write_text("stop", encoding="utf-8")
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=15)
            if sys.exc_info()[0] is not None:
                secrets_file = runtime / "demo-secrets.json"
                keys = (
                    json.loads(secrets_file.read_text(encoding="utf-8"))
                    if secrets_file.exists()
                    else {}
                )
                diagnostics = {}
                for name in ("supervisor.log", "gateway.log", "hermes.log"):
                    file = runtime / name
                    if file.exists():
                        text = "\n".join(
                            file.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
                        )
                        for secret in keys.values():
                            text = text.replace(secret, "[REDACTED_TEST_CREDENTIAL]")
                        diagnostics[name] = text
                (runtime / "diagnostics.json").write_text(
                    json.dumps(diagnostics, indent=2), encoding="utf-8"
                )
                print(json.dumps(diagnostics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
