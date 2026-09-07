"""B-only post-install authorization persistence; synthetic auth and intake, no QR/model.

Runs inside the already installed disposable Debian VM, after its empty-business
reboot passed. This creates no base installation assets and uses formal business
commands and the existing authenticated message intake API. Never used by the
installer or on an existing user host.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path("/opt/cf-agent-gateway")
STATE = Path("/var/lib/cf-agent-gateway-install")
REPORT = STATE / "business-reboot.json"
TOKEN = Path("/srv/storage/cf-agent-wechat/secrets/auth-token")
COMPOSE = [
    "docker",
    "compose",
    "--project-directory",
    str(ROOT),
    "--env-file",
    str(ROOT / ".env"),
    "--file",
    str(ROOT / "docker-compose.prod.yml"),
]


def run(arguments: list[str], *, data: str | None = None, timeout: int = 120) -> str:
    process = subprocess.run(arguments, input=data, capture_output=True, text=True, timeout=timeout)
    if process.returncode:
        raise RuntimeError("B_business_command_failed:" + Path(arguments[0]).name)
    return process.stdout.strip()


def business(*arguments: str) -> dict:
    return json.loads(
        run(
            [
                *COMPOSE,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "migration",
                "python",
                "-m",
                "cf_agent_gateway.business_access",
                *arguments,
            ]
        )
    )


def authentication() -> dict:
    return json.loads(
        run(
            [
                *COMPOSE,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "worker",
                "python",
                "-m",
                "cf_agent_gateway.adapters.wechat.diagnose",
            ]
        )
    )


def write_report(report: dict) -> None:
    descriptor = os.open(REPORT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def read_report() -> dict:
    info = REPORT.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o600)
    ):
        raise RuntimeError("B_business_report_not_protected")
    return json.loads(REPORT.read_text())


def prepare() -> dict:
    if REPORT.exists() or REPORT.is_symlink():
        raise RuntimeError("B_business_report_already_exists")
    empty = business("status")
    if empty["business_state"] != "awaiting_configuration" or any(
        empty["counts"][key] for key in ("identities", "enabled_user_policies", "active_profiles")
    ):
        raise RuntimeError("B_requires_empty_business_before_explicit_onboarding")
    prior_boot = json.loads((STATE / "boot-acceptance.json").read_text())
    if prior_boot["result"] != "passed":
        raise RuntimeError("B_empty_business_reboot_must_pass_first")
    if run(["docker", "ps", "-q", "--filter", "name=^cf-agent-wechat$"]):
        raise RuntimeError("B_never_replaces_a_running_real_wechat")
    image = json.loads((STATE / "gateway-image.json").read_text())["image_id"]
    account = "wxid_b_" + secrets.token_hex(8)
    server = r"""
import hmac, json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
token = Path('/run/test-wechat-token').read_text().strip()
account = sys.argv[1]
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path != '/api/status/auth':
            self.send_response(404); self.end_headers(); return
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
            self.send_response(401); self.end_headers(); return
        if self.headers.get('X-Session-Id') != 'default':
            self.send_response(400); self.end_headers(); return
        payload = json.dumps({'status': 'logged_in', 'loggedInUser': account}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers(); self.wfile.write(payload)
HTTPServer(('0.0.0.0', 6174), Handler).serve_forever()
"""
    container = run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            "cf-b-synthetic-auth-" + secrets.token_hex(6),
            "--network",
            "cf-internal",
            "--network-alias",
            "cf-agent-wechat",
            "--restart",
            "no",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "none",
            "--volume",
            str(TOKEN) + ":/run/test-wechat-token:ro",
            "--entrypoint",
            "python",
            image,
            "-c",
            server,
            account,
        ]
    )
    if len(container) != 64 or any(c not in "0123456789abcdef" for c in container):
        raise RuntimeError("B_synthetic_container_id_invalid")
    approval_path: Path | None = None
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                auth = authentication()
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
                continue
            if auth.get("account_id") != account or auth.get("authenticated") is not True:
                raise RuntimeError("B_synthetic_auth_not_discovered")
            break
        event = {
            "event_id": "B-authorization-" + secrets.token_hex(12),
            "source": "wechat",
            "source_account_id": auth["account_id"],
            "source_message_id": "1",
            "conversation_id": "wxid_b_approved_employee",
            "conversation_type": "private",
            "is_self": False,
            "sender_type": "human",
            "sender_id": "wxid_b_approved_employee",
            "message_type": "text",
            "content": "B synthetic archive; no Poll admission claimed",
            "timestamp": "2026-09-06T00:00:00Z",
        }
        intake = """
import json, os, sys, httpx
response = httpx.post('http://127.0.0.1:8080/internal/messages',
    headers={'Authorization': 'Bearer ' + os.environ['CF_GATEWAY_API_TOKEN']},
    json=json.load(sys.stdin), timeout=10)
response.raise_for_status()
print(json.dumps(response.json()))
"""
        message_id = json.loads(
            run(
                [*COMPOSE, "exec", "-T", "gateway", "python", "-c", intake],
                data=json.dumps(event),
            )
        )["id"]
        # /internal/messages is an archive API, not the Poll admission path.
        # B proves current configuration and archive persistence; A separately
        # proves real Poll admission/refusal and no automatic replay.
        denied = business("status", "--message-id", str(message_id))
        if (
            denied["permitted"] is not False
            or denied["historical_admission"] is not None
            or denied["ai_thread_id"] is not None
        ):
            raise RuntimeError("B_unapproved_archive_or_configuration_unexpected")
        approval = {
            "version": 1,
            "profile": {
                "profile_key": "booted-vm",
                "revision": 1,
                "provider": "hermes",
                "external_profile_ref": "profiles/booted-vm/1",
                "model": "synthetic-B-no-call",
            },
            "identity": {"employee_id": "booted-vm-operator", "display_name": "B operator"},
        }
        fd, name = tempfile.mkstemp(prefix=".B-approved-business-", dir=STATE)
        approval_path = Path(name)
        with os.fdopen(fd, "w") as stream:
            os.fchown(stream.fileno(), 0, 10001)
            os.fchmod(stream.fileno(), 0o640)
            json.dump(approval, stream)
        approved = json.loads(
            run(
                [
                    *COMPOSE,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--volume",
                    str(approval_path) + ":/run/approved-business.json:ro",
                    "worker",
                    "python",
                    "-m",
                    "cf_agent_gateway.business_access",
                    "approve",
                    "--message-id",
                    str(message_id),
                    "--input",
                    "/run/approved-business.json",
                ]
            )
        )
        current = business("status", "--message-id", str(message_id))
        if approved.get("replayed_messages") != 0 or current["permitted"] is not True:
            raise RuntimeError("B_formal_business_onboarding_did_not_pass")
        if current["historical_admission"] != denied["historical_admission"]:
            raise RuntimeError("B_approval_mutated_archived_observation")
        report = {
            "scope": "B post-install business persistence; synthetic auth and internal intake",
            "before_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "empty_business": empty,
            "message_id": message_id,
            "before_status": current,
            "before_overview": business("status"),
            "synthetic_account_discovered_from_auth": auth["account_id"],
            "archived_observation_unprocessed": True,
        }
        write_report(report)
        return report
    finally:
        run(["docker", "rm", "--force", container])
        if approval_path is not None:
            approval_path.unlink()


def check() -> dict:
    previous = read_report()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if previous["before_boot_id"] == boot:
        raise RuntimeError("B_authorized_state_requires_a_second_real_reboot")
    current = business("status", "--message-id", str(previous["message_id"]))
    overview = business("status")
    if current != previous["before_status"] or overview != previous["before_overview"]:
        raise RuntimeError("B_authorization_changed_across_reboot")
    if (
        current["permitted"] is not True
        or current["historical_admission"] is not None
        or current["ai_thread_id"] is not None
    ):
        raise RuntimeError("B_permission_or_archived_observation_changed")
    return {
        **previous,
        "after_boot_id": boot,
        "result": "passed",
        "authorization_persisted": True,
        "archived_observation_unprocessed": True,
        "after_status": current,
        "after_overview": overview,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "check"))
    args = parser.parse_args()
    if (
        os.geteuid() != 0
        or Path("/.dockerenv").exists()
        or not Path("/run/systemd/system").is_dir()
    ):
        raise SystemExit("B helper requires its already-installed booted disposable VM")
    print(json.dumps(prepare() if args.phase == "prepare" else check(), sort_keys=True))


if __name__ == "__main__":
    main()
