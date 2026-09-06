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
    identity = inputs_dir / "identity.json"
    identity.write_text(
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
                "wechat": {
                    "account_id": "wxid_clean_device_gateway",
                    "sender_id": "wxid_clean_device_operator",
                    "conversation_id": "wxid_clean_device_operator",
                },
            }
        )
    )
    identity.chmod(0o600)
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
                "initial_identity_file": str(identity),
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
    stage("configure", extra=["--inputs", str(inputs)])
    initial = snapshot()
    stage("migrate", succeeds=False)
    assert snapshot() == initial
    stage("database")
    stage("initialize", succeeds=False)
    stage("migrate")
    stage("initialize")
    stage("start")
    assert_gate(False)
    for service in ("gateway", "dispatch-worker"):
        item = inspect_service(service)
        assert item["Config"]["User"] == "10001:10001"
        assert "cf-internal" in item["NetworkSettings"]["Networks"]
        assert item["State"]["Health"]["Status"] == "healthy"
    CHECKS.append(
        "real PostgreSQL empty migration + V2 identity + Gateway/Dispatch with closed gate"
    )
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

    manager("wechat-dry-run", ["bash", str(WECHAT / "scripts/start-qr-login.sh"), "--dry-run"])
    manager(
        "wechat-synthetic-fresh-qr", ["bash", str(WECHAT / "scripts/start-qr-login.sh")], tty=True
    )
    assert_gate(True)
    wechat_token = TOKEN.read_text().strip()
    runtime = literal_env(GATEWAY / "runtime.env")
    port = literal_env(GATEWAY / ".env").get("CF_GATEWAY_PORT", "8080")
    gateway_url = f"http://127.0.0.1:{port}"
    admin = runtime["CF_AGENT_GATEWAY_ADMIN_TOKEN"]
    assert request(gateway_url + "/admin/messages", "invalid-credential")[0] == 401
    assert request(gateway_url + "/admin/messages", runtime["CF_GATEWAY_API_TOKEN"])[0] == 401
    # Bootstrap mode=latest intentionally ignores history. Inject only after a
    # successful empty poll, just as a newly sent user message arrives after QR.
    wait(
        lambda: request("http://127.0.0.1:6174/__test/state", wechat_token)[1]["polls"] >= 2,
        "empty initial polling checkpoint",
    )
    marker = "clean-device-A-" + secrets.token_hex(12)
    request("http://127.0.0.1:6174/__test/control", wechat_token, {"message": marker})
    deliveries = wait(
        lambda: request(gateway_url + "/admin/deliveries", admin)[1]["items"],
        "text delivery outbox",
    )
    wait(
        lambda: (
            request(gateway_url + "/admin/deliveries", admin)[1]["items"][0]["status"]
            == "delivered"
        ),
        "text delivery completion",
    )
    time.sleep(9)  # At least two subsequent production polling intervals replay the same message.
    messages = request(gateway_url + "/admin/messages", admin)[1]
    assert messages["total"] == 1
    message = messages["items"][0]
    assert message["content"] == marker and message["identity_id"] and message["ai_thread_id"]
    assert message["dispatch_status"] == "success" and message["response_id"]
    deliveries = request(gateway_url + "/admin/deliveries", admin)[1]["items"]
    assert len(deliveries) == 1 and deliveries[0]["status"] == "delivered"
    assert deliveries[0]["message_id"] == message["id"]
    thread = request(gateway_url + "/admin/threads/" + message["ai_thread_id"], admin)[1]
    assert thread["agent_profile_id"] and thread["hermes_thread_id"]
    assert thread["delivery_summary"]["delivered"] == 1
    external = request("http://127.0.0.1:6174/__test/state", wechat_token)[1]
    assert external["deliveries"] == [
        {"chatId": "wxid_clean_device_operator", "text": f"Synthetic reply: {marker}"}
    ]
    calls = request("http://127.0.0.1:18765/__test/state", key_file.read_text())[1]["calls"]
    marker_calls = [call for call in calls if call["request"]["messages"][0]["content"] == marker]
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
                    "gateway_messages": messages["total"],
                    "gateway_delivery_records": len(deliveries),
                    "synthetic_wechat_text_deliveries": len(external["deliveries"]),
                    "synthetic_hermes_model_executions_for_marker": len(marker_calls),
                },
            },
            indent=2,
        )
    )
    CHECKS.append(
        "unique text: real admission/profile/authorization/dispatch/response/delivery; dedup"
    )

    before = snapshot()
    conflict = inputs_dir / "conflict.json"
    conflicting = json.loads(inputs.read_text())
    conflicting["hermes_model"] = "deliberately-conflicting-A-input"
    conflict.write_text(json.dumps(conflicting))
    conflict.chmod(0o600)
    stage("configure", succeeds=False, extra=["--inputs", str(conflict)])
    assert before == snapshot()
    stage("configure", extra=["--inputs", str(inputs)])
    stage("database")
    stage("migrate")
    stage("initialize")
    assert before == snapshot()
    assert request(gateway_url + "/admin/messages", admin)[1]["total"] == 1
    CHECKS.append(
        "repeat configuration/database/migration/identity retains credentials/config/data"
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
        lambda: request(gateway_url + "/admin/messages", admin)[1]["total"] == 1,
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
