"""Run A in a disposable Debian 13 container with its own rootful Docker daemon.

Only external WeChat/Hermes and the four systemd observations are simulated.
The production installers create every deployment directory, credential,
network, database, image and business authorization. Never run on an existing host.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = Path("/acceptance")
EVIDENCE = Path("/evidence")
GATEWAY = Path("/opt/cf-agent-gateway")
WECHAT = Path("/opt/cf-agent-wechat")
TOKEN = Path("/srv/storage/cf-agent-wechat/secrets/auth-token")
STATE = Path("/var/lib/cf-agent-gateway-install")
MANAGER = "deployoperator"
WECHAT_SHA = "67cbbb04ce15703428ce165ac38effac19f4b701"
GATEWAY_SHA = os.environ.get("GATEWAY_COMMIT", "")
COMMON = ["--manager", MANAGER, "--gateway-commit", GATEWAY_SHA, "--wechat-commit", WECHAT_SHA]
COMPOSE = [
    "docker",
    "--context",
    "default",
    "compose",
    "--project-directory",
    str(GATEWAY),
    "--env-file",
    str(GATEWAY / ".env"),
    "--file",
    str(GATEWAY / "docker-compose.prod.yml"),
]
CHECKS: list[str] = []


def private_values() -> list[str]:
    values = []
    for path in (
        TOKEN,
        Path("/root/a-manager-password"),
        Path("/root/acceptance-inputs/hermes-key"),
        Path("/var/lib/cf-agent-gateway-postgres/postgres-password"),
        Path("/var/lib/cf-agent-gateway-postgres/app-password"),
    ):
        if path.is_file():
            values.append(path.read_text().strip())
    generated = STATE / "secrets.json"
    if generated.is_file():
        values.extend(
            value for value in json.loads(generated.read_text()).values() if isinstance(value, str)
        )
    runtime_file = GATEWAY / "runtime.env"
    if runtime_file.is_file():
        values.extend(literal_env(runtime_file).values())
    return sorted((value for value in values if value), key=len, reverse=True)


def redact_output(output: str) -> str:
    for value in private_values():
        output = output.replace(value, "[redacted A credential]")
    return output


def preserve_runtime_evidence() -> None:
    if (GATEWAY / ".env").is_file():
        try:
            logs = subprocess.run(
                [*COMPOSE, "logs", "--no-color"], capture_output=True, text=True, timeout=30
            )
            (EVIDENCE / "runtime-final.log").write_text(redact_output(logs.stdout + logs.stderr))
        except subprocess.TimeoutExpired:
            (EVIDENCE / "runtime-final.log").write_text("runtime log collection timed out\n")
    # Scan on both success and failure, including management output and earlier
    # stage logs. Only sanitized A evidence leaves the disposable root filesystem.
    for path in EVIDENCE.iterdir():
        if path.is_file():
            original = path.read_text(errors="replace")
            sanitized = redact_output(original)
            if sanitized != original:
                path.write_text(sanitized)
            assert not any(value in sanitized for value in private_values())


def run(
    name: str, command: list[str], *, succeeds: bool = True, timeout: int = 1200
) -> subprocess.CompletedProcess[str]:
    print(f"A: {name}", flush=True)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    (EVIDENCE / f"{name}.log").write_text(redact_output(result.stdout + result.stderr))
    if (result.returncode == 0) != succeeds:
        raise AssertionError(f"{name}: unexpected exit {result.returncode}; see stage log")
    return result


def stage(name: str, *, succeeds: bool = True, extra: list[str] | None = None) -> None:
    run(
        f"install-{name}-{len(CHECKS)}",
        ["bash", str(SOURCE / "deploy/install-clean-device.sh"), name, *COMMON, *(extra or [])],
        succeeds=succeeds,
    )
    CHECKS.append(f"{name}: {'passed' if succeeds else 'failed safely as expected'}")


def manager(name: str, command: list[str], *, succeeds: bool = True, tty: bool = False) -> str:
    # A account provisioning supplies the password privately. Real sudo policy
    # evaluates the named non-root manager; no Controller or Docker call is mocked.
    authorized = subprocess.run(
        ["sudo", "-H", "-u", MANAGER, "--", "sudo", "-S", "-p", "", "-v"],
        input=Path("/root/a-manager-password").read_text(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if authorized.returncode:
        raise AssertionError("A manager sudo authentication failed; credentials not logged")
    if tty:
        command = ["script", "--quiet", "--return", "--command", shlex.join(command), "/dev/null"]
    return run(name, ["sudo", "-H", "-u", MANAGER, "--", *command], succeeds=succeeds).stdout


def literal_env(path: Path) -> dict[str, str]:
    return dict(
        (line.split("=", 1)[0], shlex.split(line.split("=", 1)[1])[0])
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def request(url: str, token: str, body: object | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers), timeout=10
        ) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def wait(predicate, description: str, timeout: int = 120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise AssertionError(f"timed out: {description}")


def snapshot() -> dict[str, str]:
    paths = [
        GATEWAY / ".env",
        GATEWAY / "runtime.env",
        GATEWAY / "config/production.local.yaml",
        TOKEN,
        WECHAT / "docker/.env",
    ]
    paths.extend(
        path
        for path in Path("/var/lib/cf-agent-gateway-postgres").rglob("*")
        if path.is_file()
        and (
            path.suffix in {".json", ".yaml"}
            or path.name in {".env", "postgres-password", "app-password"}
        )
    )
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def inspect_service(service: str) -> dict:
    identifier = subprocess.check_output([*COMPOSE, "ps", "-a", "-q", service], text=True).strip()
    assert identifier, f"missing {service}"
    return json.loads(subprocess.check_output(["docker", "inspect", identifier], text=True))[0]


def assert_management_isolation() -> None:
    management = {
        "uid": int(subprocess.check_output(["id", "-u", MANAGER], text=True)),
        "gid": int(subprocess.check_output(["id", "-g", MANAGER], text=True)),
        "groups": [
            int(value)
            for value in subprocess.check_output(["id", "-G", MANAGER], text=True).split()
        ],
    }
    wechat_env = dict(
        line.split("=", 1)
        for line in (WECHAT / "docker/.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    runtime_root = Path(wechat_env["CF_AGENT_WECHAT_RUNTIME_ROOT"])
    assert runtime_root == Path("/srv/storage/cf-agent-wechat/runtime")
    wechat_identity = {
        "uid": int(wechat_env["CF_AGENT_WECHAT_RUNTIME_UID"]),
        "gid": int(wechat_env["CF_AGENT_WECHAT_RUNTIME_GID"]),
    }
    database_ids = subprocess.check_output(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=cf-agent-gateway-db"],
        text=True,
    ).split()
    assert len(database_ids) == 1

    def process_identity(identifier: str) -> dict:
        status = subprocess.check_output(
            ["docker", "exec", identifier, "cat", "/proc/1/status"], text=True
        )
        values = {
            key: [int(item) for item in value.split()]
            for line in status.splitlines()
            if (key := line.partition(":")[0]) in ("Uid", "Gid", "Groups")
            for value in (line.partition(":")[2],)
        }
        return {"uid": values["Uid"][1], "gid": values["Gid"][1], "groups": values["Groups"]}

    processes = {
        "gateway": process_identity(inspect_service("gateway")["Id"]),
        "postgres": process_identity(database_ids[0]),
        "synthetic_wechat_supervisor": process_identity(wechat_env["AGENT_WECHAT_CONTAINER_NAME"]),
    }
    assert (processes["gateway"]["uid"], processes["gateway"]["gid"]) == (10001, 10001)
    assert processes["postgres"]["uid"] != 0 and processes["postgres"]["gid"] != 0
    manager_intent = json.loads((STATE / "manager-identity.json").read_text())
    assert manager_intent == {
        "manager": MANAGER,
        "uid": management["uid"],
        "gid": management["gid"],
    }
    postgres_intent = json.loads((STATE / "postgres-service-identity.json").read_text())
    assert postgres_intent == {
        "uid": processes["postgres"]["uid"],
        "gid": processes["postgres"]["gid"],
    }
    service_ids = {0, 10001, wechat_identity["uid"], wechat_identity["gid"]}
    service_ids.update((processes["postgres"]["uid"], processes["postgres"]["gid"]))
    assert management["uid"] not in service_ids and management["gid"] not in service_ids
    assert not service_ids.intersection(management["groups"])
    protected = {}
    for path in (GATEWAY, runtime_root, runtime_root / "data"):
        metadata = path.stat()
        protected[str(path)] = {
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "mode": oct(stat.S_IMODE(metadata.st_mode)),
        }
        if path != GATEWAY:
            assert (metadata.st_uid, metadata.st_gid) == (
                wechat_identity["uid"],
                wechat_identity["gid"],
            )
    # runuser drops to the actual installed account and supplementary groups;
    # this child never invokes sudo and never emits protected file contents.
    script = """
import json, os, pathlib, sys
checks = []
for index, value in enumerate(sys.argv[1:]):
    try:
        if index < 3:
            os.chdir(value)
        else:
            with pathlib.Path(value).open('rb') as stream:
                stream.read(1)
    except PermissionError:
        checks.append({'path': value, 'access_denied': True})
    else:
        raise AssertionError('management account accessed a protected service path')
print(json.dumps(checks))
"""
    denied = run(
        "manager-without-sudo-private-paths",
        [
            "runuser",
            "-u",
            MANAGER,
            "--",
            "python3",
            "-c",
            script,
            str(GATEWAY),
            str(runtime_root),
            str(runtime_root / "data"),
            str(GATEWAY / "runtime.env"),
            str(TOKEN),
        ],
    )
    (EVIDENCE / "management-service-identities.json").write_text(
        json.dumps(
            {
                "manager": management,
                "wechat_configured_runtime_directory_owner": wechat_identity,
                "actual_container_pid1_identities": processes,
                "protected_directories": protected,
                "unprivileged_access_checks": json.loads(denied.stdout),
                "boundary": (
                    "WeChat application/process is synthetic; its supervisor is recorded "
                    "separately from configured runtime ownership"
                ),
            },
            indent=2,
        )
        + "\n"
    )
    CHECKS.append(
        "actual manager and service identities separated; no unprivileged private-path access"
    )


def assert_gate(expected: bool) -> None:
    status = json.loads(
        manager(
            f"controller-status-{len(CHECKS)}",
            ["sudo", "-n", "--", str(GATEWAY / "deploy/wechat-runtime-control"), "status"],
            succeeds=expected,
        )
    )
    assert status["ready"] is expected
    for service in ("worker", "delivery-worker"):
        identifiers = subprocess.check_output(
            [*COMPOSE, "ps", "-a", "-q", service], text=True
        ).strip()
        if identifiers:
            assert inspect_service(service)["State"]["Running"] is expected
        else:
            assert not expected
    CHECKS.append(f"real Controller combination gate={expected}")


def hermes_probe(expected: str) -> None:
    # Uses actual installed configuration and the real client from inside Gateway.
    script = """
import os, socket
from urllib.parse import urlsplit
from cf_agent_gateway.config import load_settings
from cf_agent_gateway.hermes.client import HermesClient
from cf_agent_gateway.hermes.errors import HermesAPIError, HermesTimeoutError, HermesTransportError
s = load_settings(os.environ['CF_GATEWAY_CONFIG'])
expected = __import__('sys').argv[1]
endpoint = urlsplit(s.hermes.base_url)
if expected == 'success':
    with socket.create_connection((endpoint.hostname, endpoint.port), timeout=3): pass
try:
    key = os.environ[s.hermes.api_key_env]
    if expected == 'authentication': key = 'intentionally-invalid-A-key'
    with HermesClient(s.hermes.base_url, key, s.hermes.model,
                      timeout=1) as client:
        result = client.chat('synthetic-protocol-probe', hermes_thread_id='a-probe-session',
            idempotency_key='a-probe-idempotency', profile_reference='profiles/clean-device/1',
            profile_revision=1, thread_id='a-probe-thread', session_metadata={'layer':'A'})
except HermesAPIError as error:
    assert expected == 'authentication' and error.status_code == 401
except HermesTimeoutError:
    assert expected == 'timeout'
except HermesTransportError:
    assert expected in {'unavailable', 'unreachable'}
else:
    assert expected == 'success'
    assert result.hermes_thread_id == 'a-probe-session'
    assert 'Synthetic reply: synthetic-protocol-probe' in result.assistant_content
print('synthetic Hermes real-container client: ' + expected)
"""
    run(f"hermes-{expected}", [*COMPOSE, "exec", "-T", "gateway", "python", "-c", script, expected])
    CHECKS.append(f"real Gateway container to external synthetic Hermes: {expected}")


def application_json(
    name: str,
    module: str,
    arguments: list[str] | None = None,
    *,
    input_file: Path | None = None,
    succeeds: bool = True,
) -> dict:
    needs_wechat_token = module.endswith(".wechat.diagnose") or (arguments or [])[:1] == ["approve"]
    if input_file is None and not needs_wechat_token:
        command = [*COMPOSE, "exec", "-T", "gateway", "python", "-m", module]
    else:
        command = [*COMPOSE, "run", "--rm", "--no-deps", "-T"]
        if input_file is not None:
            command.extend(["--volume", f"{input_file}:/run/approved-business.json:ro"])
        command.extend(["worker" if needs_wechat_token else "migration", "python", "-m", module])
    return json.loads(run(name, [*command, *(arguments or [])], succeeds=succeeds).stdout)


def account_diagnosis(name: str, expected: str) -> dict:
    result = application_json(
        name,
        "cf_agent_gateway.adapters.wechat.diagnose",
        succeeds=expected in {"authenticated", "not_logged_in"},
    )
    assert result["status"] == expected
    assert result["authenticated"] is (expected == "authenticated")
    assert ("account_id" in result) is (expected == "authenticated")
    return result


def synthetic_fresh_qr(wechat_token: str) -> dict:
    command = ["bash", str(WECHAT / "scripts/start-qr-login.sh")]
    # The unchanged lifecycle owns runtime creation and Gate opening. Only the
    # external WebSocket scan event is held until the test observes not_logged_in.
    process = subprocess.Popen(
        [
            "sudo",
            "-H",
            "-u",
            MANAGER,
            "--",
            "script",
            "--quiet",
            "--return",
            "--command",
            shlex.join(command),
            "/dev/null",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait(
            lambda: request("http://127.0.0.1:6174/__test/state", wechat_token)[0] == 200,
            "real fresh-QR runtime before simulated scan completion",
        )
        before = account_diagnosis("wechat-before-scan", "not_logged_in")
        assert_gate(False)
        assert (
            request("http://127.0.0.1:6174/__test/control", wechat_token, {"allow_login": True})[0]
            == 200
        )
        output, _ = process.communicate(timeout=360)
        (EVIDENCE / "wechat-synthetic-fresh-qr.log").write_text(redact_output(output))
        assert process.returncode == 0, "formal fresh-QR lifecycle failed; inspect stage log"
        after = account_diagnosis("wechat-after-scan", "authenticated")
        (EVIDENCE / "login-discovery.json").write_text(
            json.dumps({"synthetic": True, "before": before, "after": after}, indent=2)
        )
        return after
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            (EVIDENCE / "wechat-synthetic-fresh-qr.log").write_text(redact_output(output))


def checkpoint_evidence(name: str) -> list[dict]:
    # Observe the actual persisted model; this performs no initialization or writes.
    script = """
import json, os
from sqlalchemy import select
from sqlalchemy.orm import Session
from cf_agent_gateway.config import load_settings
from cf_agent_gateway.database import create_database_engine
from cf_agent_gateway.adapters.wechat.polling_models import WechatSyncCheckpoint
settings = load_settings(os.environ['CF_GATEWAY_CONFIG'])
engine = create_database_engine(settings.database.url)
with Session(engine) as session:
    rows = session.scalars(select(WechatSyncCheckpoint).order_by(WechatSyncCheckpoint.id))
    print(json.dumps([{'id': row.id, 'account_id': row.source_account_id,
        'conversation_id': row.conversation_id, 'last_local_id': row.last_local_id,
        'generation': row.regression_generation} for row in rows]))
engine.dispose()
"""
    return json.loads(run(name, [*COMPOSE, "exec", "-T", "gateway", "python", "-c", script]).stdout)


def main() -> None:
    assert os.geteuid() == 0 and len(GATEWAY_SHA) == 40
    assert Path("/.dockerenv").exists(), "A must run inside its disposable container"
    assert not any(path.exists() for path in (GATEWAY, WECHAT, TOKEN))
    assert (EVIDENCE / "empty-before-system.txt").read_text().strip() == "confirmed"
    assert not subprocess.check_output(["docker", "image", "ls", "-q"], text=True).strip()
    assert not subprocess.check_output(["docker", "volume", "ls", "-q"], text=True).strip()
    CHECKS.append("empty deployment paths, image store and volume store confirmed")
    stage("controller")
    metadata = GATEWAY.stat()
    assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (0, 0, 0o750)
    groups = subprocess.check_output(["id", "-nG", MANAGER], text=True).split()
    assert "root" not in groups and "docker" not in groups
    contract = json.loads(
        manager(
            "controller-contract-before-token",
            ["sudo", "-n", "--", str(GATEWAY / "deploy/wechat-runtime-control"), "contract"],
        )
    )
    assert contract["contract_version"] == 1 and not TOKEN.exists()
    CHECKS.append("real static Contract before Token/database/Gateway; root:root 0750")

    from sudo_tty_probe import probe

    probe(GATEWAY, EVIDENCE, MANAGER)
    CHECKS.append("real B helper authentication with default sudo tty/use_pty; A rule restored")

    entrypoint = Path("/usr/local/libexec/cf-agent-wechat-prepare")
    manager(
        "wechat-checkout",
        ["bash", str(entrypoint), "checkout", "--manager", MANAGER, "--commit", WECHAT_SHA],
    )
    run(
        "synthetic-registry",
        ["docker", "run", "-d", "--name", "a-registry", "--network", "host", "registry:2"],
    )
    wait(
        lambda: urllib.request.urlopen("http://127.0.0.1:5000/v2/", timeout=2).status == 200,
        "isolated test registry",
    )
    run(
        "synthetic-wechat-build",
        [
            "docker",
            "build",
            "-t",
            "localhost:5000/a-wechat:only-test",
            "-f",
            str(SOURCE / "tests/deployment/Dockerfile.synthetic-wechat"),
            str(SOURCE),
        ],
    )
    run("synthetic-wechat-push", ["docker", "push", "localhost:5000/a-wechat:only-test"])
    references = json.loads(
        subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                "localhost:5000/a-wechat:only-test",
            ],
            text=True,
        )
    )
    reference = next(value for value in references if value.startswith("localhost:5000/"))
    (EVIDENCE / "synthetic-wechat-image.txt").write_text(reference + "\n")
    manager(
        "wechat-configure",
        [
            "bash",
            str(WECHAT / "scripts/prepare-clean-host.sh"),
            "configure",
            "--manager",
            MANAGER,
            "--commit",
            WECHAT_SHA,
            "--image",
            reference,
            "--runtime-uid",
            "1000",
            "--runtime-gid",
            "1000",
        ],
    )
    manager(
        "wechat-bootstrap",
        [
            "env",
            "CF_BOOTSTRAP_TESTING=1",
            "CF_BOOTSTRAP_SYSTEMCTL_BIN=/usr/local/bin/cf-a-systemctl",
            "bash",
            str(WECHAT / "scripts/bootstrap-cfserver.sh"),
        ],
    )
    assert TOKEN.exists() and not Path("/srv/storage/cf-agent-wechat/runtime").exists()
    CHECKS.append(
        "official WeChat configure/bootstrap created Token, network, protected directories"
    )

    inputs_dir = Path("/root/acceptance-inputs")
    inputs_dir.mkdir(mode=0o700)
    key_file = inputs_dir / "hermes-key"
    key_file.write_text(secrets.token_urlsafe(32))
    key_file.chmod(0o600)
    bridge = json.loads(
        subprocess.check_output(["docker", "network", "inspect", "cf-internal"], text=True)
    )[0]["IPAM"]["Config"][0]["Gateway"]
    inputs = inputs_dir / "inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "version": 1,
                "hermes_url": f"http://{bridge}:18765",
                "hermes_model": "synthetic-A",
                "hermes_api_key_file": str(key_file),
                "database": {"mode": "managed"},
            }
        )
    )
    inputs.chmod(0o600)
    hermes = subprocess.Popen(
        [
            "python3",
            str(SOURCE / "tests/deployment/external_services.py"),
            "hermes",
            "--port",
            "18765",
            "--token-file",
            str(key_file),
        ]
    )
    stage("build")
    invalid_identity = inputs_dir / "invalid-identity.json"
    invalid_identity.write_text('{"version": 1}')
    invalid_identity.chmod(0o600)
    invalid_inputs = inputs_dir / "invalid-inputs.json"
    invalid_payload = json.loads(inputs.read_text())
    invalid_payload["initial_identity_file"] = str(invalid_identity)
    invalid_inputs.write_text(json.dumps(invalid_payload))
    invalid_inputs.chmod(0o600)
    token_before = TOKEN.read_bytes()
    stage("configure", succeeds=False, extra=["--inputs", str(invalid_inputs)])
    assert not (GATEWAY / "runtime.env").exists() and not (GATEWAY / ".env").exists()
    assert TOKEN.read_bytes() == token_before
    # An explicit JSON null is invalid input, not omission of the optional file.
    invalid_identity.write_text("null")
    stage("configure", succeeds=False, extra=["--inputs", str(invalid_inputs)])
    assert not (GATEWAY / "runtime.env").exists() and not (STATE / "inputs.json").exists()
    assert TOKEN.read_bytes() == token_before
    missing_inputs = inputs_dir / "missing-identity-inputs.json"
    invalid_payload["initial_identity_file"] = str(inputs_dir / "does-not-exist.json")
    missing_inputs.write_text(json.dumps(invalid_payload))
    missing_inputs.chmod(0o600)
    stage("configure", succeeds=False, extra=["--inputs", str(missing_inputs)])
    assert not (GATEWAY / "runtime.env").exists() and TOKEN.read_bytes() == token_before
    stage("configure", extra=["--inputs", str(inputs)])
    initial = snapshot()
    stage("migrate", succeeds=False)
    assert snapshot() == initial
    stage("database")
    stage("migrate")
    stage("start")
    assert_gate(False)
    for service in ("gateway", "dispatch-worker"):
        item = inspect_service(service)
        assert item["Config"]["User"] == "10001:10001"
        assert "cf-internal" in item["NetworkSettings"]["Networks"]
        assert item["State"]["Health"]["Status"] == "healthy"
    CHECKS.append(
        "real PostgreSQL empty migration + core start without any business identity or bot ID"
    )
    assert not (STATE / "initial-identity.json").exists()
    assert "initial_identity_file" not in json.loads(inputs.read_text())
    stage("diagnose")
    assert request("http://127.0.0.1:18765/__test/state", key_file.read_text())[1]["calls"] == []
    assert account_diagnosis("wechat-service-not-started", "service_unavailable")
    CHECKS.append("default diagnosis sends no model request; absent WeChat reported accurately")
    hermes_probe("success")
    gateway_ip = inspect_service("gateway")["NetworkSettings"]["Networks"]["cf-internal"][
        "IPAddress"
    ]
    rejection = [
        "-s",
        gateway_ip,
        "-p",
        "tcp",
        "--dport",
        "18765",
        "-j",
        "REJECT",
        "--reject-with",
        "icmp-host-unreachable",
    ]
    run("isolate-hermes-source", ["iptables", "-I", "INPUT", "1", *rejection])
    try:
        hermes_probe("unreachable")
    finally:
        run("restore-hermes-source", ["iptables", "-D", "INPUT", *rejection])
    hermes_probe("success")
    for mode, expected in (("reject_auth", "authentication"), ("timeout", "timeout")):
        assert (
            request("http://127.0.0.1:18765/__test/control", key_file.read_text(), {"mode": mode})[
                0
            ]
            == 200
        )
        hermes_probe(expected)
    hermes.terminate()
    hermes.wait(timeout=10)
    hermes_probe("unavailable")
    hermes = subprocess.Popen(
        [
            "python3",
            str(SOURCE / "tests/deployment/external_services.py"),
            "hermes",
            "--port",
            "18765",
            "--token-file",
            str(key_file),
        ]
    )
    wait(
        lambda: request("http://127.0.0.1:18765/__test/state", key_file.read_text())[0] == 200,
        "synthetic Hermes safe restart",
    )
    hermes_probe("success")

    wechat_token = TOKEN.read_text().strip()
    runtime = literal_env(GATEWAY / "runtime.env")
    port = literal_env(GATEWAY / ".env").get("CF_GATEWAY_PORT", "8080")
    gateway_url = f"http://127.0.0.1:{port}"
    admin = runtime["CF_AGENT_GATEWAY_ADMIN_TOKEN"]
    assert request(gateway_url + "/admin/messages", "invalid-credential")[0] == 401
    assert request(gateway_url + "/admin/messages", runtime["CF_GATEWAY_API_TOKEN"])[0] == 401

    def access(name: str, arguments: list[str], *, approval_file: Path | None = None) -> dict:
        return application_json(
            name, "cf_agent_gateway.business_access", arguments, input_file=approval_file
        )

    def archive() -> dict:
        return request(gateway_url + "/admin/messages?limit=100", admin)[1]

    def wechat_state() -> dict:
        return request("http://127.0.0.1:6174/__test/state", wechat_token)[1]

    def wechat_control(payload: dict) -> None:
        assert request("http://127.0.0.1:6174/__test/control", wechat_token, payload)[0] == 200

    def hermes_calls() -> list[dict]:
        return request("http://127.0.0.1:18765/__test/state", key_file.read_text())[1]["calls"]

    def settle_polls() -> None:
        state = wechat_state()
        key = json.dumps([state["account"], state["chats"][0]])
        previous = state["conversation_polls"].get(key, 0)
        wait(
            lambda: wechat_state()["conversation_polls"].get(key, 0) >= previous + 2,
            "two repeated production polls of the same conversation",
        )

    def message_with_marker(marker: str) -> dict | None:
        return next((item for item in archive()["items"] if item["content"] == marker), None)

    rejected_evidence = []

    def reject_new_message(
        label: str, *, local_id: int | None = None, conversation_id: str | None = None
    ) -> dict:
        previous_calls = len(hermes_calls())
        previous_deliveries = len(wechat_state()["deliveries"])
        marker = "clean-device-A-" + label + "-" + secrets.token_hex(8)
        payload = {"message": marker}
        if local_id is not None:
            payload["local_id"] = local_id
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        wechat_control(payload)
        message = wait(lambda: message_with_marker(marker), "persisted unapproved message")

        def completed_status():
            details = access("business-" + label, ["status", "--message-id", str(message["id"])])
            outcome = details.get("historical_admission")
            return details if outcome and outcome["state"] == "completed" else None

        details = wait(completed_status, "completed unapproved message admission")
        assert details["permitted"] is False
        assert details["historical_admission"]["admitted"] is False
        settle_polls()
        current = message_with_marker(marker)
        assert current["dispatch_status"] is None and current["response_id"] is None
        assert len(hermes_calls()) == previous_calls
        assert len(wechat_state()["deliveries"]) == previous_deliveries
        rejected_evidence.append({"label": label, "message": current, "status": details})
        return current

    empty_business = access("business-empty-core", ["status"])
    assert empty_business["counts"]["configured_identity_route_pairs"] == 0
    assert archive()["total"] == 0
    before_login_calls = len(hermes_calls())
    manager("wechat-dry-run", ["bash", str(WECHAT / "scripts/start-qr-login.sh"), "--dry-run"])
    discovered = synthetic_fresh_qr(wechat_token)
    account_id = discovered["account_id"]
    assert_gate(True)
    assert_management_isolation()
    assert len(hermes_calls()) == before_login_calls
    settle_polls()  # Establish the empty latest-history checkpoint after actual login.
    CHECKS.append(
        "core and real Gate start without identity; explicit auth discovers current account"
    )

    unapproved = reject_new_message("before-approval")
    assert unapproved["source_account_id"] == account_id
    discovery = access("business-discover", ["discover", "--limit", "50"])
    candidate = next(item for item in discovery["items"] if item["message_id"] == unapproved["id"])
    assert candidate["source_account_id"] == account_id and candidate["permitted"] is False
    assert candidate["sender_id"] == unapproved["sender_id"]
    CHECKS.append(
        "discovered unapproved account/sender creates no authorization, model call or reply"
    )

    # This operator approval file is first created after observed login/admission.
    # Bot, sender and conversation IDs are derived by the formal business command.
    approval = inputs_dir / "approved-business.json"
    approval.write_text(
        json.dumps(
            {
                "version": 1,
                "profile": {
                    "profile_key": "clean-device",
                    "revision": 1,
                    "provider": "hermes",
                    "external_profile_ref": "profiles/clean-device/1",
                    "model": "synthetic-A",
                },
                "identity": {"employee_id": "clean-device-operator", "display_name": "A operator"},
            }
        )
    )
    os.chown(approval, 0, 10001)
    approval.chmod(0o640)
    approved = access(
        "business-approve",
        [
            "approve",
            "--message-id",
            str(unapproved["id"]),
            "--input",
            "/run/approved-business.json",
        ],
        approval_file=approval,
    )
    assert approved["permitted"] is True and approved["replayed_messages"] == 0
    assert approved["source_account_id"] == account_id
    settle_polls()
    assert message_with_marker(unapproved["content"])["dispatch_status"] is None
    assert not any(
        call["request"]["messages"][0]["content"] == unapproved["content"]
        for call in hermes_calls()
    )
    assert wechat_state()["deliveries"] == []

    marker = "clean-device-A-approved-" + secrets.token_hex(12)
    wechat_control({"message": marker})
    message = wait(lambda: message_with_marker(marker), "approved message archive")
    wait(
        lambda: any(
            item["message_id"] == message["id"] and item["status"] == "delivered"
            for item in request(gateway_url + "/admin/deliveries", admin)[1]["items"]
        ),
        "approved text delivery completion",
    )
    settle_polls()
    message = message_with_marker(marker)
    assert message["identity_id"] and message["ai_thread_id"]
    assert message["dispatch_status"] == "success" and message["response_status"] == "delivered"
    deliveries = request(gateway_url + "/admin/deliveries", admin)[1]["items"]
    assert len(deliveries) == 1 and deliveries[0]["message_id"] == message["id"]
    thread = request(gateway_url + "/admin/threads/" + message["ai_thread_id"], admin)[1]
    assert thread["agent_profile_id"] and thread["hermes_thread_id"]
    assert thread["delivery_summary"]["delivered"] == 1
    external = wechat_state()
    assert external["deliveries"] == [
        {"chatId": message["conversation_id"], "text": f"Synthetic reply: {marker}"}
    ]
    marker_calls = [
        call for call in hermes_calls() if call["request"]["messages"][0]["content"] == marker
    ]
    assert len(marker_calls) == 1
    (EVIDENCE / "text-chain.json").write_text(
        json.dumps(
            {
                "marker": marker,
                "message": message,
                "thread": thread,
                "delivery": deliveries,
                "external": external,
                "synthetic_hermes_execution_records": marker_calls,
                "counts": {
                    "gateway_messages": sum(
                        item["content"] == marker for item in archive()["items"]
                    ),
                    "gateway_delivery_records": len(deliveries),
                    "synthetic_wechat_text_deliveries": len(external["deliveries"]),
                    "synthetic_hermes_model_executions_for_marker": len(marker_calls),
                },
                "previously_rejected_message_id": unapproved["id"],
                "previously_rejected_message_replayed": False,
            },
            indent=2,
        )
    )
    CHECKS.append("formal approval never replays rejection; only a new text executes/delivers once")

    unbound_conversation = message["conversation_id"] + "_unbound"
    wechat_control({"conversation_id": unbound_conversation})
    wait(
        lambda: (
            wechat_state()["conversation_polls"].get(
                json.dumps([account_id, unbound_conversation]), 0
            )
            >= 2
        ),
        "unbound conversation empty checkpoint",
    )
    unbound = reject_new_message("missing-route", conversation_id=unbound_conversation)
    assert unbound["sender_id"] == message["sender_id"]
    calls_before_route = len(hermes_calls())
    route_approval = access(
        "business-bind-observed-conversation",
        ["approve", "--message-id", str(unbound["id"]), "--input", "/run/approved-business.json"],
        approval_file=approval,
    )
    assert route_approval["permitted"] is True and route_approval["replayed_messages"] == 0
    assert route_approval["historical_admission"]["admitted"] is False
    settle_polls()
    assert message_with_marker(unbound["content"])["dispatch_status"] is None
    assert len(hermes_calls()) == calls_before_route and len(wechat_state()["deliveries"]) == 1
    (EVIDENCE / "unconfigured-route.json").write_text(
        json.dumps(
            {
                "rejected_message": unbound,
                "binding_approval": route_approval,
                "replayed_messages": 0,
                "synthetic_external_services": True,
            },
            indent=2,
        )
    )
    CHECKS.append("mapped sender without route is durably rejected; later binding never replays it")

    original_checkpoints = checkpoint_evidence("checkpoints-before-relogin")
    original_total = archive()["total"]
    original_calls = len(hermes_calls())
    account_states = []
    for label, update, expected in (
        ("logged-out", {"auth": "logged_out"}, "not_logged_in"),
        ("missing-account", {"auth": "logged_in", "account_present": False}, "invalid_account"),
        ("malformed-account", {"account": {"unexpected": "identifier"}}, "invalid_account"),
        ("wrong-token", {"account": account_id, "mode": "reject_auth"}, "token_error"),
    ):
        wechat_control(update)
        state = account_diagnosis("wechat-" + label, expected)
        # Let any already-started authenticated poll finish, then prove invalid
        # authentication cannot fetch new message batches on subsequent ticks.
        time.sleep(4)
        before_invalid_polls = wechat_state()["polls"]
        before_auth_requests = wechat_state()["auth_requests"]
        wait(
            lambda previous=before_auth_requests: wechat_state()["auth_requests"] >= previous + 2,
            "invalid auth observations",
        )
        assert wechat_state()["polls"] == before_invalid_polls
        assert len(hermes_calls()) == original_calls and len(wechat_state()["deliveries"]) == 1
        account_states.append({"case": label, "diagnosis": state, "polls_unchanged": True})
    wechat_control({"auth": "logged_in", "account": account_id, "mode": "normal"})
    account_diagnosis("wechat-same-account-relogin", "authenticated")
    settle_polls()
    assert checkpoint_evidence("checkpoints-after-relogin") == original_checkpoints
    assert archive()["total"] == original_total and len(hermes_calls()) == original_calls
    CHECKS.append(
        "missing/malformed/auth failures fail closed; same-account relogin retains checkpoint/dedup"
    )

    second_account = account_id + "_second"
    wechat_control({"account": second_account})
    second_discovery = account_diagnosis("wechat-second-account", "authenticated")
    assert second_discovery["account_id"] == second_account
    wait(
        lambda: wechat_state()["account_polls"].get(json.dumps(second_account), 0) >= 2,
        "new account empty checkpoint",
    )
    switched = reject_new_message("other-account", local_id=1)
    assert switched["source_account_id"] == second_account
    switched_status = access(
        "business-second-account", ["status", "--message-id", str(switched["id"])]
    )
    assert switched_status["enterprise_identity_id"] is None
    assert switched_status["ai_thread_id"] is None
    switched_checkpoints = checkpoint_evidence("checkpoints-two-accounts")
    old_rows = [row for row in switched_checkpoints if row["account_id"] == account_id]
    new_rows = [row for row in switched_checkpoints if row["account_id"] == second_account]
    assert old_rows == original_checkpoints and len(new_rows) == 1
    assert new_rows[0]["last_local_id"] == 1 and old_rows[0]["last_local_id"] > 1
    assert new_rows[0]["id"] != old_rows[0]["id"]
    assert message_with_marker(marker)["ai_thread_id"] == thread["id"]
    wechat_control({"account": account_id})
    account_diagnosis("wechat-return-original-account", "authenticated")
    settle_polls()
    assert len(hermes_calls()) == original_calls and len(wechat_state()["deliveries"]) == 1
    (EVIDENCE / "account-isolation.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "states": account_states,
                "original_checkpoints": original_checkpoints,
                "switched_checkpoints": switched_checkpoints,
                "second_account_business_status": switched_status,
                "old_thread_retained": thread["id"],
            },
            indent=2,
        )
    )
    CHECKS.append("account switch retains separate checkpoint, authorization and thread history")

    disabled = access("business-disable", ["disable", "--employee-id", "clean-device-operator"])
    assert disabled["enabled"] is False and disabled["replayed_messages"] == 0
    reject_new_message("after-disable")
    before = snapshot()
    business_before = access(
        "business-disabled-state", ["status", "--message-id", str(message["id"])]
    )
    assert business_before["permitted"] is False
    conflict = inputs_dir / "conflict.json"
    conflicting = json.loads(inputs.read_text())
    conflicting["hermes_model"] = "deliberately-conflicting-A-input"
    conflict.write_text(json.dumps(conflicting))
    conflict.chmod(0o600)
    stage("configure", succeeds=False, extra=["--inputs", str(conflict)])
    assert before == snapshot()
    total_before_rerun = archive()["total"]
    stage("configure", extra=["--inputs", str(inputs)])
    stage("database")
    stage("migrate")
    stage("initialize")
    stage("start")
    stage("diagnose")
    assert before == snapshot() and archive()["total"] == total_before_rerun
    assert (
        access("business-after-rerun", ["status", "--message-id", str(message["id"])])
        == business_before
    )
    assert len(hermes_calls()) == original_calls
    reject_new_message("after-rerun")
    expected_archive_total = archive()["total"]
    (EVIDENCE / "business-onboarding.json").write_text(
        json.dumps(
            {
                "empty_business": empty_business,
                "discovery": discovery,
                "approval": approved,
                "disabled": disabled,
                "disabled_status_retained_after_install": business_before,
                "rejected_messages": rejected_evidence,
                "archive_total": expected_archive_total,
                "business_model_executions": 1,
                "business_deliveries": 1,
                "synthetic_external_services": True,
            },
            indent=2,
        )
    )
    CHECKS.append(
        "formal disable rejects new text; base rerun retains authorization state, secrets and data"
    )
    manager("wechat-stop", ["bash", str(WECHAT / "scripts/stop-qr-runtime.sh")])
    assert_gate(False)
    dispatch_before = inspect_service("dispatch-worker")["Id"]
    assert inspect_service("dispatch-worker")["State"]["Running"]
    database_ids = subprocess.check_output(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=cf-agent-gateway-db"],
        text=True,
    ).split()
    assert len(database_ids) == 1
    database_inspect = json.loads(
        subprocess.check_output(["docker", "inspect", database_ids[0]], text=True)
    )[0]
    assert not database_inspect["HostConfig"]["PortBindings"]
    run("postgres-restart", ["docker", "restart", database_ids[0]])
    stage("database")
    stage("migrate")
    wait(
        lambda: (
            request(gateway_url + "/admin/messages", admin)[1]["total"] == expected_archive_total
        ),
        "PostgreSQL restart persistence",
    )
    assert inspect_service("dispatch-worker")["Id"] == dispatch_before
    assert before == snapshot()
    CHECKS.append(
        "independent unexposed PostgreSQL restart persists data; Controller leaves Dispatch"
    )
    run("runtime-logs", [*COMPOSE, "logs", "--no-color"])
    known_secrets = [wechat_token, key_file.read_text(), runtime["CF_GATEWAY_API_TOKEN"], admin]
    for path in EVIDENCE.rglob("*"):
        if path.is_file():
            content = path.read_text(errors="replace")
            assert not any(value in content for value in known_secrets), f"Secret in {path.name}"
    CHECKS.append("no generated API/Admin/Hermes/WeChat credential leaked to evidence")
    hermes.terminate()


if __name__ == "__main__":
    EVIDENCE.mkdir(exist_ok=True)
    result = "failed"
    try:
        main()
        result = "passed"
    finally:
        preserve_runtime_evidence()
        for name in (
            "gateway-image.json",
            "python-image.json",
            "postgres-image.json",
            "python-packages.txt",
            "source-versions.json",
            "manager-identity.json",
            "postgres-service-identity.json",
        ):
            record = STATE / name
            if record.is_file():
                (EVIDENCE / name).write_bytes(record.read_bytes())
        system_packages = Path("/var/lib/cf-agent-wechat-install/system-packages.txt")
        if system_packages.is_file():
            (EVIDENCE / "system-packages.txt").write_bytes(system_packages.read_bytes())
        (EVIDENCE / "A-result.json").write_text(
            json.dumps(
                {
                    "layer": "A",
                    "result": result,
                    "gateway_commit": GATEWAY_SHA,
                    "wechat_commit": WECHAT_SHA,
                    "checks": CHECKS,
                    "real": [
                        "Debian packages",
                        "isolated Docker",
                        "PostgreSQL",
                        "Gateway",
                        "Controller",
                        "WeChat management scripts",
                        "non-root manager permissions",
                    ],
                    "synthetic": [
                        "WeChat external HTTP/WebSocket/process",
                        "Hermes external HTTP",
                        "four systemd observations for Bootstrap in a non-booted container",
                    ],
                    "B": "not executed by A: requires booted Debian 13 host/VM and reboot",
                    "C": "not executed: requires approved real QR/Windows AI/Hermes acceptance",
                },
                indent=2,
            )
        )
