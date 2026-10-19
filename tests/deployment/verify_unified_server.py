"""Read only the disposable suite environment; publish bounded sanitized facts."""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import io
import json
import os
import random
import re
import secrets
import ssl
import stat
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

STATE = Path("/var/lib/cf-suite-install")
RESULT = Path("/evidence/result.json")
PRIVATE = Path("/root/suite-acceptance.private.json")


def run(argv, **kwargs):
    return subprocess.run(
        argv, capture_output=True, check=True, text=True, timeout=180, **kwargs
    ).stdout.strip()


def save(value):
    RESULT.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def journal():
    return json.loads((STATE / "journal.json").read_bytes())


def check(condition, label):
    if not condition:
        raise RuntimeError(label)


def credentials_snapshot():
    paths = [
        Path("/opt/cf-agent-gateway/runtime.env"),
        Path("/opt/cf-agent-gateway/config/production.local.yaml"),
        Path("/opt/cf-agent-wechat/docker/.env"),
        Path("/srv/storage/cf-agent-wechat/secrets/auth-token"),
        Path("/etc/cf-filebrowser-enterprise/config.yaml"),
        Path("/etc/cf-filebrowser-enterprise/deployment.json"),
    ]
    paths.extend(path for path in (STATE / "secrets").iterdir() if path.is_file())
    paths.extend(
        path for path in Path("/etc/cf-filebrowser-enterprise/secrets").iterdir() if path.is_file()
    )
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def file_client():
    spec = importlib.util.spec_from_file_location(
        "shared_host_api", "/opt/cf-filebrowser-enterprise/scripts/tests/shared-host-api.py"
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api, api.Client("https://suite.test:18443", "/root/suite-inputs/ca.crt")


def public_filebrowser_build(receipt):
    records = [
        json.loads(path.read_bytes()) for path in STATE.glob("filebrowser-build-*/image.json")
    ]
    selected = [
        item
        for item in records
        if item.get("image_id") == receipt["image_id"]
        and item.get("source_sha") == receipt["source_sha"]
    ]
    check(bool(selected), "fixed-source-build-receipt-missing")
    record = selected[-1]
    expected_bases = {
        "FFMPEG_IMAGE": "gtstef/ffmpeg:8.1-decode",
        "GO_IMAGE": "golang:alpine",
        "NODE_IMAGE": "node:jod-slim",
        "RUNTIME_IMAGE": "alpine:latest",
    }
    check(set(record["bases"]) == set(expected_bases), "unexpected-build-base-fields")
    for key, reference in expected_bases.items():
        base = record["bases"][key]
        check(set(base) == {"requested", "digest", "image_id"}, "unexpected-build-base-receipt")
        check(base["requested"] == reference, "unexpected-build-base-reference")
        check(
            re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", base["digest"])
            and re.fullmatch(r"sha256:[0-9a-f]{64}", base["image_id"]),
            "invalid-public-base-digest",
        )
    check(
        set(record["inputs"])
        == {
            "backend/go.mod",
            "backend/go.sum",
            "frontend/package.json",
            "frontend/package-lock.json",
            "_docker/Dockerfile",
            "deploy/shared-host/compose.build.yaml",
        }
        and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in record["inputs"].values()),
        "unexpected-build-input-receipt",
    )
    check(record["platform"] == "linux/amd64", "unexpected-build-platform")
    return {key: record[key] for key in ("source_sha", "image_id", "platform", "bases", "inputs")}


def request(client, method, endpoint, expected=(200,), **kwargs):
    reply = client.request(method, endpoint, **kwargs)
    check(reply.status in expected, "unexpected-api-status-" + str(reply.status))
    return reply


def acceptance():
    check(
        stat.S_IMODE(
            Path("/opt/cf-filebrowser-enterprise/scripts/container-entrypoint.sh").stat().st_mode
        )
        == 0o755,
        "reviewed-entrypoint-not-executable-by-service",
    )
    file_service_ids = run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=cf-filebrowser"]
    ).split()
    check(len(file_service_ids) == 1, "expected-one-file-service")
    check(
        run(["docker", "inspect", "--format", "{{.Config.User}}", file_service_ids[0]])
        == "10001:10001",
        "ordinary-file-service-identity",
    )
    api, client = file_client()
    password = (STATE / "secrets/filebrowser-admin").read_text().strip()

    def login(user, secret):
        return (
            request(
                client,
                "POST",
                "/api/auth/login",
                query={"username": user},
                headers={"X-Password": urllib.parse.quote(secret, safe="")},
            )
            .body.decode()
            .strip()
        )

    admin = login("admin", password)
    request(
        client,
        "POST",
        "/api/resources",
        token=admin,
        query={"source": api.SOURCE, "path": "/unified-acceptance", "isDir": "true"},
        body=b"",
    )
    account = {
        "username": "unified_scoped_worker",
        "password": secrets.token_urlsafe(32),
        "permissions": api.permissions(admin=False, share=False, modify=False, delete=False),
        "loginMethod": "password",
        "locale": "en",
        "scopes": [{"name": api.SOURCE, "scope": "/unified-acceptance"}],
    }
    request(
        client,
        "POST",
        "/api/users",
        token=admin,
        expected=(201,),
        headers={"X-Password": urllib.parse.quote(password, safe="")},
        data={"which": [], "data": account},
    )
    session = login(account["username"], account["password"])
    token = request(
        client,
        "POST",
        "/api/auth/token",
        token=session,
        query={
            "name": "unified-runtime",
            "days": "1",
            "permissions": "api,browse,download,create",
            "minimal": "false",
        },
    ).json()["token"]
    who = request(client, "GET", "/api/users", token=session, query={"id": "self"}).json()
    check(who["permissions"]["admin"] is False, "ordinary-business-user-required")
    # About 18 MB, 12 deterministic non-business members. No real materials or model key.
    generator = random.Random(20261020)
    members = {f"商品测试/主图 ({i:02d}).bin": generator.randbytes(1_513_910) for i in range(12)}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    payload = output.getvalue()
    check(18_000_000 <= len(payload) <= 19_000_000, "required-transfer-scale")
    request(
        client,
        "POST",
        "/api/resources",
        token=token,
        query={"source": api.SOURCE, "path": "/图包 (1).zip"},
        body=payload,
    )

    def downloaded():
        return request(
            client,
            "GET",
            "/api/resources/download",
            token=token,
            query={"source": api.SOURCE, "file": "/图包 (1).zip"},
        ).body

    returned = downloaded()
    check(returned == payload, "tls-file-service-transfer-bytes")
    with zipfile.ZipFile(io.BytesIO(returned)) as archive:
        check(set(archive.namelist()) == set(members), "zip-member-list")
        for name, value in members.items():
            check(
                hashlib.sha256(archive.read(name)).digest() == hashlib.sha256(value).digest(),
                "zip-member-bytes",
            )
    request(
        client,
        "GET",
        "/api/resources",
        token=token,
        expected=api.DENIED,
        query={"source": api.SOURCE, "path": "/../not-authorized"},
    )
    request(
        client,
        "GET",
        "/api/resources",
        token="wrong-token",
        expected=api.DENIED,
        query={"source": api.SOURCE, "path": "/"},
    )
    # Revoke an actual disposable Linux management password, not a business token.
    management_secret = secrets.token_urlsafe(32)
    run(["chpasswd"], input="deployoperator:" + management_secret + "\n")
    run(["usermod", "--lock", "deployoperator"])
    check(
        run(["passwd", "--status", "deployoperator"]).split()[1] == "L",
        "management-account-not-locked",
    )
    denied = subprocess.run(
        ["runuser", "-u", "deployoperator", "--", "sudo", "-n", "true"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    check(denied.returncode != 0, "management-sudo-still-authorized")
    check(downloaded() == payload, "business-credential-depends-on-management")
    # Recreate a real service container with the same image/mounts; do not call
    # this a database restart, OS reboot, backup restore, or native Hermes E2E.
    compose = [
        "docker",
        "compose",
        "--project-name",
        "cf-filebrowser",
        "--env-file",
        "/etc/cf-filebrowser-enterprise/.env",
        "-f",
        "/opt/cf-filebrowser-enterprise/deploy/shared-host/compose.yaml",
        "-f",
        "/opt/cf-filebrowser-enterprise/deploy/shared-host/compose.lan.yaml",
        "--profile",
        "approved",
    ]
    run(
        [
            *compose,
            "up",
            "--detach",
            "--force-recreate",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            "filebrowser-enterprise",
        ]
    )
    check(downloaded() == payload, "file-lost-on-service-recreation")
    context = ssl.create_default_context(cafile="/root/suite-inputs/ca.crt")
    connection = http.client.HTTPSConnection("suite.test", 18444, context=context, timeout=20)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        check(response.status == 200, "gateway-verified-https-health")
        response.read()
    finally:
        connection.close()
    check(
        not Path("/srv/storage/cf-agent-wechat/runtime").exists(),
        "installer-created-wechat-runtime",
    )
    for service in ("worker", "delivery-worker"):
        check(
            not run(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=cf-agent-gateway",
                    "--filter",
                    "label=com.docker.compose.service=" + service,
                ]
            ),
            "installer-opened-gates",
        )
    db = run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=cf-agent-gateway-db"]
    )
    zones = run(
        [
            "docker",
            "exec",
            db,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-Atc",
            "SHOW TimeZone; SHOW log_timezone;",
        ]
    ).splitlines()
    check(zones == ["UTC", "Asia/Shanghai"], "sql-vs-log-timezones")
    private = {
        "token": token,
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "snapshot": credentials_snapshot(),
    }
    PRIVATE.write_text(json.dumps(private))
    result = json.loads(RESULT.read_bytes())
    public_receipts = {}
    for name, target in {
        "filebrowser": STATE / "filebrowser-image.json",
        "https": STATE / "https-image.json",
        "gateway": Path("/var/lib/cf-agent-gateway-install/gateway-image.json"),
        "python": Path("/var/lib/cf-agent-gateway-install/python-image.json"),
        "postgres": Path("/var/lib/cf-agent-gateway-install/postgres-image.json"),
    }.items():
        # These fixed receipts contain only source hashes and image references,
        # never environment files or credential records.
        data = json.loads(target.read_bytes())
        allowed = {
            "source_sha",
            "reference",
            "image_id",
            "requested_reference",
            "digest_reference",
            "gateway_commit",
            "base_digest",
        }
        check(set(data) <= allowed, "unexpected-private-image-receipt-field")
        public_receipts[name] = data
    result.update(
        image_receipts=public_receipts,
        filebrowser_build=public_filebrowser_build(public_receipts["filebrowser"]),
        status="acceptance-passed-awaiting-repeat",
        bytes=len(payload),
        members=12,
        filebrowser_runtime_user="10001:10001",
        reviewed_entrypoint_mode="0755",
        sha256=private["file_sha256"],
        true_components=["Gateway", "PostgreSQL", "FileBrowser", "HTTPS", "WeChat base installer"],
        passed=[
            "same-input failure resume",
            "verified HTTPS upload/download",
            "whole ZIP and 12 member hashes",
            "ordinary scoped business credential",
            "management password locked and sudo negative",
            "business credential still works",
            "real same-image service recreation retains file",
            "bad token and path rejected",
            "Poll/Delivery closed",
            "no WeChat runtime/login",
            "SQL UTC and log Asia/Shanghai",
        ],
        not_tested=[
            "booted systemd",
            "host reboot",
            "real Hermes model",
            "real WeChat",
            "configuration-only trusted task return",
            "published production image combination",
        ],
    )
    save(result)


def repeat():
    private = json.loads(PRIVATE.read_bytes())
    _, client = file_client()
    check(
        credentials_snapshot() == private["snapshot"],
        "repeat-changed-credentials-config-or-database-metadata",
    )
    body = request(
        client,
        "GET",
        "/api/resources/download",
        token=private["token"],
        query={"source": "enterprise-files", "file": "/图包 (1).zip"},
    ).body
    check(hashlib.sha256(body).hexdigest() == private["file_sha256"], "repeat-lost-file")
    value = json.loads(RESULT.read_bytes())
    value["status"] = "passed"
    value["passed"].append("second complete entry preserves credentials, config, and file bytes")
    value["completed_stages"] = journal()["completed"]
    save(value)


def failure():
    value = (
        json.loads(RESULT.read_bytes())
        if RESULT.exists()
        else {"scope": "isolated-unified-Debian-container"}
    )
    if value.get("status") == "passed":
        return
    value["status"] = "failed"
    if (STATE / "journal.json").exists():
        value["stages"] = [
            {key: event[key] for key in ("stage", "result")} for event in journal()["events"]
        ]
    # Only isolated generated secrets exist here. Redact them before exporting
    # the last component error excerpt; never copy complete private logs.
    hidden = []
    for path in [
        *(STATE / "secrets").glob("*"),
        *Path("/etc/cf-filebrowser-enterprise/secrets").glob("*"),
        Path("/srv/storage/cf-agent-wechat/secrets/auth-token"),
    ]:
        if path.is_file():
            hidden.append(path.read_text().strip())
    generated = Path("/var/lib/cf-agent-gateway-install/secrets.json")
    if generated.exists():
        hidden.extend(json.loads(generated.read_bytes()).values())

    def redact(text):
        for secret in hidden:
            if secret:
                text = text.replace(secret, "[redacted generated test secret]")
        return re.sub(
            r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", "[redacted PEM]", text, flags=re.S
        )

    # Read-only, fixed-project diagnostics. Never export container configuration,
    # environment or complete application/daemon logs (they can contain secrets).
    try:
        identifiers = run(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=cf-filebrowser"]
        ).split()
        states = []
        for identifier in identifiers[:4]:
            state = json.loads(
                run(["docker", "inspect", "--format", "{{json .State}}", identifier])
            )
            states.append(
                {
                    "status": state.get("Status"),
                    "exit_code": state.get("ExitCode"),
                    "oom_killed": state.get("OOMKilled"),
                    "error": redact(str(state.get("Error", "")))[:3000],
                    "health": state.get("Health", {}).get("Status"),
                }
            )
            lines = subprocess.run(
                ["docker", "logs", "--tail", "30", identifier],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=True,
                text=True,
                timeout=15,
            ).stdout.splitlines()
            states[-1]["startup_error_lines"] = [
                redact(line)[:1000]
                for line in lines
                if re.search(r"Permission denied|exec .*failed|exec .*not found", line, re.I)
            ][:4]
        value["filebrowser_container_states"] = states
        https_identifiers = run(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=cf-suite-https"]
        ).split()
        https_states = []
        for identifier in https_identifiers[:2]:
            state = json.loads(
                run(["docker", "inspect", "--format", "{{json .State}}", identifier])
            )
            health = state.get("Health", {})
            lines = subprocess.run(
                ["docker", "logs", "--tail", "30", identifier],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=True,
                text=True,
                timeout=15,
            ).stdout.splitlines()
            https_states.append(
                {
                    "status": state.get("Status"),
                    "exit_code": state.get("ExitCode"),
                    "error": redact(str(state.get("Error", "")))[:1000],
                    "health": health.get("Status"),
                    "health_checks": [
                        {
                            "exit_code": item.get("ExitCode"),
                            "output": redact(str(item.get("Output", "")))[:2000],
                        }
                        for item in health.get("Log", [])[-3:]
                    ],
                    "startup_error_lines": [
                        redact(line)[:1000]
                        for line in lines
                        if re.search(r"emerg|crit|Permission denied|Read-only|failed", line, re.I)
                    ][:6],
                }
            )
        value["https_container_states"] = https_states
        daemon = Path("/root/dockerd.private.log")
        if daemon.exists():
            errors = [
                line
                for line in daemon.read_text(errors="replace").splitlines()
                if re.search(r"level=(error|warning)\b", line)
            ]
            value["daemon_error_excerpt"] = redact("\n".join(errors[-12:]))[-6000:]
    except Exception as error:
        value["diagnostic_failure"] = type(error).__name__
    logs = sorted(STATE.glob("command-*.private.log"), key=lambda p: p.stat().st_mtime)
    if logs:
        excerpt = "\n".join(logs[-1].read_text(errors="replace").splitlines()[-70:])[-12000:]
        value["last_component_error_excerpt"] = redact(excerpt)
    save(value)


def main():
    os.umask(0o077)
    check(
        Path("/.dockerenv").is_file() and os.environ.get("GITHUB_ACTIONS") == "true",
        "disposable-container-only",
    )
    action = sys.argv[1]
    if action == "resume-precondition":
        check(
            Path("/root/cgroup-resource-smoke").read_text().strip() == "passed",
            "nested-cgroup-resource-controls-not-proved",
        )
        value = {
            "scope": "pristine-Debian13-container-independent-Docker-daemon",
            "systemd": "four-read-only-observation-substitute",
            "storage": "independent tmpfs test-disk; not persistent host disk/RAID",
            "cgroup": "private namespace; cpu/memory/pids resource-limited smoke passed",
            "status": "first-failure-observed",
        }
        if (STATE / "journal.json").exists():
            value["first_events"] = [
                {"stage": item["stage"], "result": item["result"]} for item in journal()["events"]
            ]
        save(value)
        check(os.environ["FIRST_RESULT"] != "0", "injected-port-conflict-was-not-rejected")
        check(
            journal()["events"][-1]["stage"] == "filebrowser-prepare",
            "failure-occurred-before-port-conflict-validation",
        )
        check("filebrowser-start" not in journal()["completed"], "continued-after-failure")
        logs = sorted(STATE.glob("command-*.private.log"), key=lambda p: p.stat().st_mtime)
        check(
            bool(logs) and "validator rejected phase port" in logs[-1].read_text(),
            "injected-port-conflict-was-not-the-actual-failure",
        )
    elif action == "acceptance":
        acceptance()
    elif action == "repeat":
        repeat()
    elif action == "failure":
        failure()
    else:
        raise RuntimeError("unknown-test-action")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        failure()
        raise
