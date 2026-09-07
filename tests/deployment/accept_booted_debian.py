"""B-only boot/reboot acceptance on an explicitly disposable Debian 13 VM/host.

This never performs real QR or model requests (those belong to C). It records
checks around the official installation and one operator-controlled host reboot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
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


def wait_boot_services(timeout: float = 240) -> None:
    """Observe systemd boot jobs without starting or repairing any service."""
    deadline = time.monotonic() + timeout
    pending = {"docker.service", "cf-agent-gateway-core.service"}
    while pending:
        for unit in sorted(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SystemExit("B timed out waiting for boot service: " + unit)
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=min(10, remaining),
            )
            state = result.stdout.strip()
            if state == "active" and result.returncode == 0:
                pending.remove(unit)
            elif state in {"failed", "unknown", "not-found"}:
                raise SystemExit("B boot service failed or missing: " + unit)
        if pending:
            time.sleep(min(2, max(0, deadline - time.monotonic())))


def require_interactive_terminal() -> None:
    if not all(os.isatty(descriptor) for descriptor in (0, 1, 2)):
        raise SystemExit(
            "B acceptance requires a real interactive TTY for manager sudo authentication"
        )


def authenticate_manager(manager: str) -> None:
    require_interactive_terminal()
    print("B: authenticate the management account through sudo on this terminal.", flush=True)
    # Inherit all stdio and the controlling TTY. Never read/capture/log a password,
    # use sudo -S, create a session, or alter sudo's standard tty timestamp policy.
    result = subprocess.run(["sudo", "-H", "-u", manager, "--", "sudo", "-v"], timeout=300)
    if result.returncode:
        raise SystemExit("B requires successful interactive manager sudo authentication")


# One outer sudo session owns both validation and the captured management stage.
# Debian's default use_pty may otherwise create a different terminal/session on
# each sudo invocation, invalidating an earlier per-TTY credential timestamp.
MANAGER_STAGE = """
import json
import subprocess
import sys
validation = subprocess.run(["sudo", "-v"], timeout=300)
if validation.returncode:
    raise SystemExit("B manager sudo authentication failed")
stage = subprocess.run(sys.argv[2:], capture_output=True, text=True, timeout=1800)
if sys.argv[1] == "closed-gate":
    try:
        payload = json.loads(stage.stdout)
    except (ValueError, TypeError):
        raise SystemExit("B Controller status did not return valid JSON")
    if stage.returncode != 1 or not isinstance(payload, dict) or payload.get("ready") is not False:
        raise SystemExit("B Poll/Delivery gate must remain closed after host reboot")
elif stage.returncode:
    raise SystemExit("B management stage failed; inspect the official stage diagnostic locally")
"""


def run_manager_stage(
    manager: str,
    command: list[str],
    *,
    controller_status: bool = False,
) -> None:
    require_interactive_terminal()
    print("B: validate manager sudo and execute this stage in one terminal session.", flush=True)
    result = subprocess.run(
        [
            "sudo",
            "-H",
            "-u",
            manager,
            "--",
            "python3",
            "-c",
            MANAGER_STAGE,
            "closed-gate" if controller_status else "stage",
            *command,
        ],
        timeout=2100,
    )
    if result.returncode:
        raise SystemExit("B management authentication or stage failed")


def report_location() -> None:
    for path in (STATE, *STATE.parents):
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_uid, info.st_gid) != (0, 0)
            or info.st_mode & 0o022
            or (path == STATE and stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise SystemExit(
                "B report requires a protected root:root 0700 installation state directory"
            )


def report_status():
    info = REPORT.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid) != (0, 0)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise SystemExit("B report must be a root:root 0600 single-link regular file")
    return info


def read_report() -> dict:
    report_location()
    expected = report_status()
    descriptor = os.open(REPORT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise SystemExit("B report changed while opening")
        return json.load(stream)


def write_report(payload: dict, *, update: bool = False) -> None:
    report_location()
    if REPORT.exists() or REPORT.is_symlink():
        report_status()
        if not update:
            raise SystemExit("B report already exists; existing evidence retained")
    elif update:
        raise SystemExit("B report disappeared; no replacement evidence created")
    descriptor, temporary = tempfile.mkstemp(prefix=".boot-acceptance-", dir=STATE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, REPORT)
        directory_fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        # The only cleanup target is the exact temporary file created above.
        Path(temporary).unlink(missing_ok=True)


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
        require_interactive_terminal()
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
        authenticate_manager(args.manager)
        stage("controller")
        public = Path("/usr/local/libexec/cf-agent-wechat-prepare")
        prefix = ["bash"]

        def manager_run(command: list[str]) -> None:
            run_manager_stage(args.manager, command)

        manager_run(
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
        manager_run(
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
        manager_run([*prefix, str(wechat / "bootstrap-cfserver.sh")])
        stage("build", ["--inputs", args.inputs])
        stage("configure", ["--inputs", args.inputs])
        for name in (
            "database",
            "migrate",
            "start",
            "diagnose",
            "boot-service",
        ):
            stage(name)
        write_report(
            {
                "before_boot_id": boot_id,
                "files": files(),
                "manager": args.manager,
                "gateway_commit": args.gateway_commit,
                "wechat_commit": args.wechat_commit,
                "layer": "B",
                "result": "awaiting operator reboot",
                "C": "pending separately approved real WeChat/Windows AI/Hermes acceptance",
            }
        )
        print("B preparation passed. Reboot this disposable host manually, then run after-reboot.")
        return
    require_interactive_terminal()
    previous = read_report()
    if previous["manager"] != args.manager or previous["before_boot_id"] == boot_id:
        raise SystemExit("recorded manager must match and a real host reboot must have occurred")
    if files() != previous["files"]:
        raise SystemExit("configuration or credentials changed across reboot")
    wait_boot_services()
    common = [
        "--manager",
        args.manager,
        "--gateway-commit",
        previous["gateway_commit"],
        "--wechat-commit",
        previous["wechat_commit"],
    ]
    run(["bash", str(ROOT / "deploy/install-clean-device.sh"), "diagnose", *common])
    run_manager_stage(
        args.manager,
        ["sudo", "-n", "--", str(ROOT / "deploy/wechat-runtime-control"), "status"],
        controller_status=True,
    )
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
                "database migration and current business configuration overview",
                "Poll/Delivery closed",
                "WeChat waits for fresh QR",
            ],
        }
    )
    write_report(previous, update=True)
    print("B booted-host installation and reboot passed; C remains pending.")


if __name__ == "__main__":
    main()
