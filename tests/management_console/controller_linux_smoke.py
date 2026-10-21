"""Real Linux UDS/Docker controller against one disposable, networkless CI container."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def command(argv):
    return subprocess.check_output(argv, text=True, timeout=30).strip()


def main():
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This real Docker smoke is restricted to the disposable Linux CI runner.")
    task = re.sub("[^0-9]", "", os.environ["GITHUB_RUN_ID"])
    if not task:
        raise SystemExit("missing CI identity")
    name = "cf-console-control-smoke-" + task
    runtime = Path(tempfile.mkdtemp(prefix="cf-control-", dir=os.environ["RUNNER_TEMP"]))
    config = runtime / "config.json"
    address = runtime / "socket/controller.sock"
    controller = Path(__file__).resolve().parents[2] / "deploy/wechat-console/control.py"
    process = None
    report = {"synthetic_data": True, "real_linux_uds": True, "real_docker": True}
    try:
        command(
            [
                "docker",
                "create",
                "--name",
                name,
                "--label",
                "cf.synthetic-project=" + name,
                "--network",
                "none",
                "python:3.12-slim",
                "python",
                "-c",
                "import time; time.sleep(120)",
            ]
        )
        target = json.loads(command(["docker", "inspect", name]))[0]
        config.write_text(
            json.dumps(
                {
                    "socket": str(address),
                    "allowed_uid": os.getuid(),
                    "allowed_gid": os.getgid(),
                    "wechat_container": name,
                    "wechat_image_id": target["Image"],
                    "workers": {"poll": name},
                }
            )
        )
        subprocess.run(["sudo", "chown", "root:root", str(config)], check=True)
        subprocess.run(["sudo", "chmod", "600", str(config)], check=True)
        with (runtime / "controller.log").open("w") as log:
            process = subprocess.Popen(
                ["sudo", sys.executable, str(controller), "--config", str(config)],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("owned controller exited")
                if address.exists():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("owned controller socket timeout")
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(address)),
                base_url="http://controller",
                trust_env=False,
                timeout=10,
            ) as client:
                assert client.get("/status").json()["wechat_service"] == "created"
                assert (
                    json.loads(command(["docker", "inspect", name]))[0]["State"]["Status"]
                    == "created"
                )
                assert client.post("/dispatch/start", json={}).status_code == 404
                assert (
                    client.post("/wechat/start", json={"command": "not-accepted"}).status_code
                    == 400
                )
                assert client.post("/wechat/start", json={}).json()["changed"] is True
                assert client.post("/wechat/start", json={}).json()["changed"] is False
                assert client.post("/wechat/app/start", json={}).json()["state"] == "unsupported"
                assert client.get("/status").json()["wechat_service"] == "running"
                report.update(
                    status_read_no_start=True,
                    start_real_container=True,
                    repeat_preserved=True,
                    no_generic_actions=True,
                    no_arbitrary_arguments=True,
                    unverified_app_start_refused=True,
                )
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        probe = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
        if probe.returncode == 0:
            target = json.loads(probe.stdout)[0]
            assert target["Config"]["Labels"]["cf.synthetic-project"] == name
            subprocess.run(["docker", "rm", "--force", target["Id"]], check=True, timeout=30)
        report["owned_container_removed"] = (
            subprocess.run(["docker", "inspect", name], capture_output=True).returncode != 0
        )
        output = Path(os.environ["RUNNER_TEMP"]) / "console-control-result.json"
        output.write_text(json.dumps(report, indent=2))
    assert all(v is True for v in report.values())
    print(json.dumps(report))


if __name__ == "__main__":
    main()
