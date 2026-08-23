from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_COMPOSE = ROOT / "docker-compose.prod.yml"
E2E_COMPOSE = ROOT / "tests" / "container" / "docker-compose.e2e.yml"
E2E_CONFIG = ROOT / "tests" / "container" / "production.yaml"
APP_SERVICES = ("gateway", "worker", "dispatch-worker", "delivery-worker")
WORKER_SERVICES = ("worker", "dispatch-worker", "delivery-worker")
HEARTBEAT_FILES = (
    "wechat-worker-heartbeat.json",
    "dispatch-worker-heartbeat.json",
    "delivery-worker-heartbeat.json",
)


def _run(
    arguments: list[str],
    *,
    environment: dict[str, str],
    capture_output: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {shlex.join(arguments)}", flush=True)
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _container_id(compose: list[str], service: str, environment: dict[str, str]) -> str:
    result = _run(
        [*compose, "ps", "--all", "--quiet", service],
        environment=environment,
        capture_output=True,
    )
    identifiers = result.stdout.split()
    if len(identifiers) != 1:
        raise AssertionError(f"expected one {service} container, found {identifiers!r}")
    return identifiers[0]


def _inspect(container_id: str, environment: dict[str, str]) -> dict[str, Any]:
    result = _run(
        ["docker", "inspect", container_id],
        environment=environment,
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise AssertionError("docker inspect returned an unexpected payload")
    return payload[0]


def _wait_for_healthy(
    compose: list[str],
    service: str,
    environment: dict[str, str],
    *,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        inspected = _inspect(_container_id(compose, service, environment), environment)
        health = inspected["State"].get("Health", {}).get("Status")
        if inspected["State"]["Running"] and health == "healthy":
            return inspected
        time.sleep(1)
    raise AssertionError(f"{service} did not become healthy")


def _assert_application_containers(
    compose: list[str],
    environment: dict[str, str],
) -> None:
    for service in APP_SERVICES:
        inspected = _wait_for_healthy(compose, service, environment)
        assert inspected["Config"]["User"] == "10001:10001"
        assert inspected["HostConfig"]["ReadonlyRootfs"] is True
        assert inspected["HostConfig"]["CapDrop"] == ["ALL"]
        heartbeat_mount = next(
            mount
            for mount in inspected["Mounts"]
            if mount["Destination"] == "/run/cf-agent-gateway"
        )
        assert heartbeat_mount["RW"] is (service != "gateway")
        _run(
            [
                *compose,
                "exec",
                "--no-TTY",
                service,
                "python",
                "-c",
                "import os; assert (os.geteuid(), os.getegid()) == (10001, 10001)",
            ],
            environment=environment,
        )

    for service in ("heartbeat-init", "migration"):
        inspected = _inspect(_container_id(compose, service, environment), environment)
        assert inspected["State"]["Running"] is False
        assert inspected["State"]["ExitCode"] == 0
    initializer = _inspect(_container_id(compose, "heartbeat-init", environment), environment)
    assert initializer["Config"]["User"] == "0:0"
    assert initializer["HostConfig"]["ReadonlyRootfs"] is True
    assert initializer["HostConfig"]["NetworkMode"] == "none"
    cap_add = {
        capability.removeprefix("CAP_") for capability in initializer["HostConfig"]["CapAdd"]
    }
    assert cap_add == {"CHOWN", "FOWNER"}
    assert initializer["HostConfig"]["CapDrop"] == ["ALL"]
    initializer_environment = "\n".join(initializer["Config"]["Env"])
    for secret_name in (
        "CF_GATEWAY_API_TOKEN",
        "CF_AGENT_GATEWAY_ADMIN_TOKEN",
        "CF_AGENT_WECHAT_TOKEN",
        "HERMES_API_KEY",
        "CF_AGENT_GATEWAY_DATABASE_URL",
    ):
        assert secret_name not in initializer_environment


def _assert_postgresql_16(compose: list[str], environment: dict[str, str]) -> None:
    result = _run(
        [
            *compose,
            "exec",
            "--no-TTY",
            "postgres",
            "psql",
            "--username",
            "gateway",
            "--dbname",
            "gateway",
            "--tuples-only",
            "--no-align",
            "--command",
            "SHOW server_version_num",
        ],
        environment=environment,
        capture_output=True,
    )
    version = int(result.stdout.strip())
    assert 160000 <= version < 170000


def _assert_heartbeat_permissions(compose: list[str], environment: dict[str, str]) -> None:
    script = f"""
import json
import stat
from pathlib import Path

directory = Path('/run/cf-agent-gateway')
status = directory.stat()
assert (status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode)) == (10001, 10001, 0o750)
expected = {HEARTBEAT_FILES!r}
for name in expected:
    path = directory / name
    status = path.stat()
    assert (status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode)) == (10001, 10001, 0o600)
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['state'] in {{'starting', 'running'}}
assert not list(directory.glob('*.tmp'))
"""
    _run(
        [*compose, "exec", "--no-TTY", "worker", "python", "-c", script],
        environment=environment,
    )


def _heartbeat_payloads(
    compose: list[str],
    environment: dict[str, str],
) -> dict[str, dict[str, Any]]:
    script = f"""
import json
from pathlib import Path

directory = Path('/run/cf-agent-gateway')
print(json.dumps({{
    name: json.loads((directory / name).read_text(encoding='utf-8'))
    for name in {HEARTBEAT_FILES!r}
}}, sort_keys=True))
"""
    result = _run(
        [*compose, "exec", "--no-TTY", "gateway", "python", "-c", script],
        environment=environment,
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or set(payload) != set(HEARTBEAT_FILES):
        raise AssertionError(f"unexpected heartbeat payload set: {payload!r}")
    return payload


def _heartbeat_timestamp(payload: dict[str, Any]) -> datetime:
    value = payload.get("updated_at")
    if not isinstance(value, str):
        raise AssertionError(f"heartbeat has no updated_at: {payload!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssertionError(f"heartbeat timestamp is naive: {value!r}")
    return parsed.astimezone(UTC)


def _wait_for_heartbeat_advances(
    compose: list[str],
    environment: dict[str, str],
    previous: dict[str, dict[str, Any]],
    *,
    names: tuple[str, ...] = HEARTBEAT_FILES,
    timeout_seconds: float = 15,
) -> dict[str, dict[str, Any]]:
    previous_times = {name: _heartbeat_timestamp(previous[name]) for name in names}
    deadline = time.monotonic() + timeout_seconds
    last_payloads: dict[str, dict[str, Any]] | None = None
    while time.monotonic() < deadline:
        last_payloads = _heartbeat_payloads(compose, environment)
        if all(
            last_payloads[name].get("state") == "running"
            and _heartbeat_timestamp(last_payloads[name]) > previous_times[name]
            for name in names
        ):
            return last_payloads
        time.sleep(0.5)
    raise AssertionError(f"heartbeat files did not advance: {last_payloads!r}")


def _assert_gateway_is_read_only(compose: list[str], environment: dict[str, str]) -> None:
    script = """
from pathlib import Path

for target in (
    Path('/run/cf-agent-gateway/gateway-must-not-write'),
    Path('/app/gateway-must-not-write'),
):
    try:
        target.write_text('forbidden', encoding='utf-8')
    except OSError:
        continue
    raise AssertionError(f'gateway unexpectedly wrote {target}')
"""
    _run(
        [*compose, "exec", "--no-TTY", "gateway", "python", "-c", script],
        environment=environment,
    )


def _runtime_health(port: int, *, timeout_seconds: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/runtime",
                timeout=3,
            ) as response:
                last_payload = json.load(response)
        except (OSError, ValueError):
            time.sleep(1)
            continue
        components = last_payload["components"]
        if (
            components["database"]["status"] == "ok"
            and components["migration_schema"]["status"] == "ok"
            and all(
                components[name]["status"] == "ok"
                for name in ("wechat_worker", "dispatch_worker", "delivery_worker")
            )
            and components["wechat_auth"]["status"] == "logged_in"
        ):
            return last_payload
        time.sleep(1)
    raise AssertionError(f"runtime health did not converge: {last_payload!r}")


def _assert_runtime_health(port: int) -> None:
    payload = _runtime_health(port)
    assert payload["status"] == "healthy"
    assert payload["components"]["hermes"] == {
        "status": "ok",
        "configuration": "configured",
        "connectivity": "no_recent_observation",
    }
    for key in ("queued", "running", "failed", "uncertain", "dead"):
        assert payload["dispatch"][key] == 0
    for key in ("queued", "delivering", "failed", "uncertain"):
        assert payload["delivery"][key] == 0


def _http_status(
    url: str,
    *,
    bearer_token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {}
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def _assert_token_boundaries(port: int) -> None:
    message_url = f"http://127.0.0.1:{port}/messages/999999"
    admin_url = f"http://127.0.0.1:{port}/admin/messages?limit=1"

    assert _http_status(message_url)[0] == 401
    assert _http_status(message_url, bearer_token="invalid-e2e-value")[0] == 401
    assert _http_status(message_url, bearer_token="e2e-api-value")[0] == 404
    assert _http_status(admin_url)[0] == 401
    assert _http_status(admin_url, bearer_token="e2e-api-value")[0] == 401
    status, payload = _http_status(admin_url, bearer_token="e2e-admin-value")
    assert status == 200
    assert payload["items"] == []


def _wait_for_component_status(
    port: int,
    component_name: str,
    expected_status: str,
    *,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/runtime",
                timeout=3,
            ) as response:
                last_payload = json.load(response)
        except (OSError, ValueError):
            time.sleep(0.5)
            continue
        if last_payload["components"][component_name]["status"] == expected_status:
            return last_payload
        time.sleep(0.5)
    raise AssertionError(f"{component_name} did not reach {expected_status}: {last_payload!r}")


def _assert_stale_heartbeat_and_recovery(
    compose: list[str],
    environment: dict[str, str],
    port: int,
) -> None:
    before = _heartbeat_payloads(compose, environment)
    dispatch_name = "dispatch-worker-heartbeat.json"
    assert before[dispatch_name]["state"] == "running"
    _run([*compose, "pause", "dispatch-worker"], environment=environment)
    try:
        payload = _wait_for_component_status(
            port,
            "dispatch_worker",
            "stale_or_invalid",
        )
        assert payload["status"] == "degraded"
        frozen = _heartbeat_payloads(compose, environment)
        assert frozen[dispatch_name]["state"] == "running"
        assert _heartbeat_timestamp(frozen[dispatch_name]) == _heartbeat_timestamp(
            before[dispatch_name]
        )
    finally:
        _run(
            [*compose, "unpause", "dispatch-worker"],
            environment=environment,
            check=False,
        )
    _wait_for_heartbeat_advances(
        compose,
        environment,
        before,
        names=(dispatch_name,),
    )
    _wait_for_healthy(compose, "dispatch-worker", environment)
    assert _runtime_health(port)["components"]["dispatch_worker"]["status"] == "ok"


def _assert_restart_recovery(
    compose: list[str],
    environment: dict[str, str],
    port: int,
) -> None:
    dispatch_name = "dispatch-worker-heartbeat.json"
    before_heartbeat = _heartbeat_payloads(compose, environment)
    before = _inspect(_container_id(compose, "dispatch-worker", environment), environment)
    _run(
        [*compose, "restart", "--timeout", "20", "dispatch-worker"],
        environment=environment,
    )
    after = _wait_for_healthy(compose, "dispatch-worker", environment)
    assert after["State"]["StartedAt"] != before["State"]["StartedAt"]
    recovered = _wait_for_heartbeat_advances(
        compose,
        environment,
        before_heartbeat,
        names=(dispatch_name,),
    )
    assert _heartbeat_timestamp(recovered[dispatch_name]) >= _heartbeat_timestamp(
        {"updated_at": after["State"]["StartedAt"]}
    )
    assert _runtime_health(port)["components"]["dispatch_worker"]["status"] == "ok"


def _wait_for_clean_stop(
    compose: list[str],
    service: str,
    environment: dict[str, str],
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        inspected = _inspect(_container_id(compose, service, environment), environment)
        if not inspected["State"]["Running"]:
            assert inspected["State"]["ExitCode"] == 0
            assert inspected["State"]["OOMKilled"] is False
            return
        time.sleep(0.5)
    raise AssertionError(f"{service} did not stop cleanly")


def _assert_clean_shutdown(compose: list[str], environment: dict[str, str]) -> None:
    _run(
        [*compose, "stop", "--timeout", "20", *WORKER_SERVICES],
        environment=environment,
    )
    for service in WORKER_SERVICES:
        _wait_for_clean_stop(compose, service, environment)

    script = f"""
import json
from pathlib import Path

directory = Path('/run/cf-agent-gateway')
for name in {HEARTBEAT_FILES!r}:
    payload = json.loads((directory / name).read_text(encoding='utf-8'))
    assert payload['state'] == 'stopped'
assert not list(directory.glob('*.tmp'))
"""
    _run(
        [*compose, "exec", "--no-TTY", "gateway", "python", "-c", script],
        environment=environment,
    )
    _run([*compose, "stop", "--timeout", "20", "gateway"], environment=environment)
    _wait_for_clean_stop(compose, "gateway", environment)


def _assert_project_resources_removed(
    project_name: str,
    environment: dict[str, str],
) -> None:
    resource_commands = {
        "containers": [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ],
        "networks": [
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ],
        "volumes": [
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ],
    }
    for resource, command in resource_commands.items():
        result = _run(command, environment=environment, capture_output=True)
        if result.stdout.split():
            raise AssertionError(f"Compose project left {resource}: {result.stdout!r}")


def main() -> int:
    port = _available_port()
    project_name = f"cf-agent-gateway-e2e-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="cf-agent-gateway-e2e-") as temporary:
        env_file = Path(temporary) / "runtime.env"
        env_file.write_text(
            "\n".join(
                (
                    "CF_GATEWAY_API_TOKEN=e2e-api-value",
                    "CF_AGENT_GATEWAY_ADMIN_TOKEN=e2e-admin-value",
                    "CF_AGENT_WECHAT_TOKEN=e2e-wechat-value",
                    "HERMES_API_KEY=e2e-hermes-value",
                    "",
                )
            ),
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        environment = os.environ.copy()
        environment.update(
            {
                "CF_GATEWAY_IMAGE": "cf-agent-gateway:e2e",
                "CF_GATEWAY_ENV_FILE": str(env_file),
                "CF_GATEWAY_CONFIG_FILE": str(E2E_CONFIG),
                "CF_AGENT_GATEWAY_DATABASE_URL": (
                    "postgresql+psycopg://gateway:e2e_database_value@"
                    "postgres:5432/gateway?connect_timeout=5"
                ),
                "CF_GATEWAY_BIND_ADDRESS": "127.0.0.1",
                "CF_GATEWAY_PORT": str(port),
                "CF_GATEWAY_WORKER_CONCURRENCY": "1",
                "CF_GATEWAY_WORKER_HEARTBEAT_INTERVAL_SECONDS": "1",
                "CF_GATEWAY_WORKER_HEARTBEAT_MAX_AGE_SECONDS": "10",
                "CF_GATEWAY_STOP_GRACE_PERIOD": "20s",
            }
        )
        compose = [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "--file",
            str(PRODUCTION_COMPOSE),
            "--file",
            str(E2E_COMPOSE),
            "--profile",
            "worker",
        ]
        succeeded = False
        try:
            _run([*compose, "config", "--quiet"], environment=environment)
            _run(
                [
                    *compose,
                    "up",
                    "--build",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    *APP_SERVICES,
                ],
                environment=environment,
            )
            _assert_application_containers(compose, environment)
            _assert_postgresql_16(compose, environment)
            _assert_heartbeat_permissions(compose, environment)
            initial_heartbeats = _heartbeat_payloads(compose, environment)
            _wait_for_heartbeat_advances(compose, environment, initial_heartbeats)
            _assert_gateway_is_read_only(compose, environment)
            _assert_runtime_health(port)
            _assert_token_boundaries(port)
            _assert_stale_heartbeat_and_recovery(compose, environment, port)
            _assert_restart_recovery(compose, environment, port)
            _assert_clean_shutdown(compose, environment)
            succeeded = True
        except BaseException:
            _run([*compose, "ps", "--all"], environment=environment, check=False)
            _run(
                [*compose, "logs", "--no-color", "--timestamps"],
                environment=environment,
                check=False,
            )
            raise
        finally:
            down = _run(
                [*compose, "down", "--volumes", "--remove-orphans", "--timeout", "20"],
                environment=environment,
                check=False,
            )
            if succeeded:
                if down.returncode != 0:
                    raise AssertionError(
                        f"successful E2E failed to clean up Compose resources: {down.returncode}"
                    )
                _assert_project_resources_removed(project_name, environment)
    print("real-container production Compose E2E passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
