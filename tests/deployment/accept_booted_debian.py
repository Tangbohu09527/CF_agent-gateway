"""B-only boot/reboot acceptance on an explicitly disposable Debian 13 VM/host.

This never performs real QR or model requests (those belong to C). It records
checks around the official installation and one operator-controlled host reboot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path("/opt/cf-agent-gateway")
STATE = Path("/var/lib/cf-agent-gateway-install")
REPORT = STATE / "boot-acceptance.json"
SOURCE = Path(__file__).resolve().parents[2]


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        raise SystemExit(
            "B acceptance command failed: "
            + Path(command[0]).name
            + "; inspect the official stage diagnostic locally"
        )
    return result.stdout.strip()


def files() -> dict[str, str]:
    selected = [
        ROOT / ".env",
        ROOT / "runtime.env",
        ROOT / "config/production.local.yaml",
        Path("/srv/storage/cf-agent-wechat/secrets/auth-token"),
    ]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in selected}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("before-reboot", "after-reboot"))
    parser.add_argument("--manager", required=True)
    parser.add_argument("--gateway-commit")
    parser.add_argument("--wechat-commit")
    parser.add_argument("--inputs")
    parser.add_argument("--wechat-image")
    parser.add_argument("--wechat-runtime-uid")
    parser.add_argument("--wechat-runtime-gid")
    args = parser.parse_args()
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise SystemExit("B requires initial root installation on a booted Debian 13 host/VM")
    virtualization = subprocess.run(
        ["systemd-detect-virt", "--container"], capture_output=True, text=True
    ).stdout.strip()
    if Path("/.dockerenv").exists() or virtualization not in ("none", ""):
        raise SystemExit("A container cannot establish B boot/reboot acceptance")
    if any(name.startswith("CF_BOOTSTRAP_") for name in os.environ):
        raise SystemExit("B forbids Bootstrap test overrides")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if args.phase == "before-reboot":
        required = (
            args.gateway_commit,
            args.wechat_commit,
            args.inputs,
            args.wechat_image,
            args.wechat_runtime_uid,
            args.wechat_runtime_gid,
        )
        if not all(required):
            parser.error(
                "before-reboot requires fixed commits, inputs, immutable image and UID/GID"
            )
        if any(
            path.exists()
            for path in (
                ROOT,
                Path("/opt/cf-agent-wechat"),
                STATE,
                Path("/srv/storage/cf-agent-wechat"),
            )
        ):
            raise SystemExit("B requires a clean disposable host; existing assets retained")
        common = [
            "--manager",
            args.manager,
            "--gateway-commit",
            args.gateway_commit,
            "--wechat-commit",
            args.wechat_commit,
        ]
        entry = ["bash", str(SOURCE / "deploy/install-clean-device.sh")]

        def stage(name: str, extra: list[str] | None = None) -> None:
            run([*entry, name, *common, *(extra or [])])
            print("B official stage passed: " + name, flush=True)

        stage("system")
        # A root provisioned manager must already have interactive/passwordless
        # sudo access for lifecycle steps; this script never changes sudo policy.
        run(["sudo", "-H", "-u", args.manager, "--", "sudo", "-n", "--", "true"])
        stage("controller")
        public = Path("/usr/local/libexec/cf-agent-wechat-prepare")
        prefix = ["sudo", "-H", "-u", args.manager, "--", "bash"]
        run(
            [
                *prefix,
                str(public),
                "checkout",
                "--manager",
                args.manager,
                "--commit",
                args.wechat_commit,
            ]
        )
        wechat = Path("/opt/cf-agent-wechat/scripts")
        run(
            [
                *prefix,
                str(wechat / "prepare-clean-host.sh"),
                "configure",
                "--manager",
                args.manager,
                "--commit",
                args.wechat_commit,
                "--image",
                args.wechat_image,
                "--runtime-uid",
                args.wechat_runtime_uid,
                "--runtime-gid",
                args.wechat_runtime_gid,
            ]
        )
        run([*prefix, str(wechat / "bootstrap-cfserver.sh")])
        stage("build", ["--inputs", args.inputs])
        stage("configure", ["--inputs", args.inputs])
        for name in (
            "database",
            "migrate",
            "initialize",
            "start",
            "diagnose",
            "boot-service",
        ):
            stage(name)
        REPORT.write_text(
            json.dumps(
                {
                    "before_boot_id": boot_id,
                    "files": files(),
                    "manager": args.manager,
                    "gateway_commit": args.gateway_commit,
                    "wechat_commit": args.wechat_commit,
                    "layer": "B",
                    "result": "awaiting operator reboot",
                    "C": "pending separately approved real WeChat/Windows AI/Hermes acceptance",
                },
                indent=2,
            )
        )
        REPORT.chmod(0o600)
        print("B preparation passed. Reboot this disposable host manually, then run after-reboot.")
        return
    previous = json.loads(REPORT.read_text())
    if previous["manager"] != args.manager or previous["before_boot_id"] == boot_id:
        raise SystemExit("recorded manager must match and a real host reboot must have occurred")
    if files() != previous["files"]:
        raise SystemExit("configuration or credentials changed across reboot")
    for unit in ("docker.service", "cf-agent-gateway-core.service"):
        if run(["systemctl", "is-active", unit]) != "active":
            raise SystemExit("not active after reboot: " + unit)
    common = [
        "--manager",
        args.manager,
        "--gateway-commit",
        previous["gateway_commit"],
        "--wechat-commit",
        previous["wechat_commit"],
    ]
    run(["bash", str(ROOT / "deploy/install-clean-device.sh"), "diagnose", *common])
    result = subprocess.run(
        [
            "sudo",
            "-H",
            "-u",
            args.manager,
            "--",
            "sudo",
            "-n",
            "--",
            str(ROOT / "deploy/wechat-runtime-control"),
            "status",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 1 or json.loads(result.stdout).get("ready") is not False:
        raise SystemExit("Poll/Delivery gate must remain closed after host reboot")
    running = run(["docker", "ps", "-q", "--filter", "name=^cf-agent-wechat$"])
    if running:
        raise SystemExit("WeChat must await fresh QR after reboot")
    previous.update(
        {
            "after_boot_id": boot_id,
            "result": "passed",
            "after_checks": [
                "configuration/credential persistence",
                "Docker and Gateway/Dispatch boot service",
                "database migration and identity check",
                "Poll/Delivery closed",
                "WeChat waits for fresh QR",
            ],
        }
    )
    REPORT.write_text(json.dumps(previous, indent=2))
    print("B booted-host installation and reboot passed; C remains pending.")


if __name__ == "__main__":
    main()
