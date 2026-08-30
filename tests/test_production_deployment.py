from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from cf_agent_gateway.config import WorkerSettings, load_settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.prod.yml"
DOCKERFILE_PATH = ROOT / "docker" / "Dockerfile"
DOCKERIGNORE_PATH = ROOT / ".dockerignore"
CONTAINER_E2E_COMPOSE_PATH = ROOT / "tests" / "container" / "docker-compose.e2e.yml"
CONTAINER_E2E_CONFIG_PATH = ROOT / "tests" / "container" / "production.yaml"
CONTAINER_E2E_RUNNER_PATH = ROOT / "tests" / "container" / "run_compose_e2e.py"
RUNTIME_CONTROL_PATH = ROOT / "deploy" / "wechat-runtime-control"
SYSTEMD_DIRECTORY = ROOT / "deploy" / "systemd"

WORKERS = {
    "dispatch-worker": {
        "module": "cf_agent_gateway.runtime.dispatch_worker",
        "service": "cf-agent-dispatch-worker",
    },
    "delivery-worker": {
        "module": "cf_agent_gateway.runtime.delivery_worker",
        "service": "cf-agent-delivery-worker",
    },
}

COMPOSE_WORKER_HEARTBEATS = {
    "worker": "/run/cf-agent-gateway/wechat-worker-heartbeat.json",
    "dispatch-worker": "/run/cf-agent-gateway/dispatch-worker-heartbeat.json",
    "delivery-worker": "/run/cf-agent-gateway/delivery-worker-heartbeat.json",
}
TOKEN_FILE_SERVICES = frozenset({"worker", "delivery-worker"})
TOKEN_CONTAINER_PATH = "/run/secrets/cf-agent-wechat-auth-token"
TOKEN_HOST_SOURCE = (
    "${CF_AGENT_WECHAT_TOKEN_HOST_FILE:-/srv/storage/cf-agent-wechat/secrets/auth-token}"
)

GATEWAY_HEARTBEAT_ENVIRONMENTS = {
    "CF_GATEWAY_WECHAT_HEARTBEAT_PATH": COMPOSE_WORKER_HEARTBEATS["worker"],
    "CF_GATEWAY_DISPATCH_HEARTBEAT_PATH": COMPOSE_WORKER_HEARTBEATS["dispatch-worker"],
    "CF_GATEWAY_DELIVERY_HEARTBEAT_PATH": COMPOSE_WORKER_HEARTBEATS["delivery-worker"],
}

SYSTEMD_GATEWAY_HEARTBEATS = {
    "CF_GATEWAY_WECHAT_HEARTBEAT_PATH": "/run/cf-agent-gateway/worker-heartbeat.json",
    "CF_GATEWAY_DISPATCH_HEARTBEAT_PATH": "/run/cf-agent-dispatch-worker/heartbeat.json",
    "CF_GATEWAY_DELIVERY_HEARTBEAT_PATH": "/run/cf-agent-delivery-worker/heartbeat.json",
}


def _load_compose() -> dict[str, object]:
    loaded = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _targets_token_file(mount: object) -> bool:
    if isinstance(mount, dict):
        return mount.get("target") == TOKEN_CONTAINER_PATH
    if isinstance(mount, str):
        return (
            mount == TOKEN_CONTAINER_PATH
            or mount.startswith(f"{TOKEN_CONTAINER_PATH}:")
            or f":{TOKEN_CONTAINER_PATH}" in mount
        )
    return False


def _parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    directives: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    section: str | None = None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        assert section is not None, f"{path}:{line_number}: directive outside section"
        assert "=" in line, f"{path}:{line_number}: malformed directive"
        name, value = line.split("=", 1)
        directives[section][name].append(value)
    return directives


def test_production_compose_defines_independent_v2_workers() -> None:
    compose = _load_compose()
    services = compose["services"]
    assert isinstance(services, dict)
    assert {"heartbeat-init", "migration", "gateway", "worker", *WORKERS}.issubset(services)

    for service_name in ("migration", "gateway", "worker", *WORKERS):
        service = services[service_name]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]

    for service_name, expected in WORKERS.items():
        service = services[service_name]
        assert service["command"] == ["python", "-m", expected["module"]]
        assert service["profiles"] == ["worker"]
        assert service["depends_on"]["migration"]["condition"] == ("service_completed_successfully")
        assert service["restart"] == "unless-stopped"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert "gateway-state:/var/lib/cf-agent-gateway" in service["volumes"]

        environment = service["environment"]
        assert environment["CF_GATEWAY_SERVICE"] == expected["service"]
        assert environment["CF_GATEWAY_WORKER_ID"] == expected["service"]

    shared_worker_mount = "runtime-heartbeats:/run/cf-agent-gateway"
    for service_name, heartbeat_path in COMPOSE_WORKER_HEARTBEATS.items():
        service = services[service_name]
        assert shared_worker_mount in service["volumes"]
        assert service["environment"]["CF_GATEWAY_WORKER_HEARTBEAT_PATH"] == heartbeat_path
        health_test = service["healthcheck"]["test"]
        assert health_test[:4] == [
            "CMD",
            "python",
            "-m",
            "cf_agent_gateway.runtime.heartbeat",
        ]
        assert health_test[health_test.index("--file") + 1] == heartbeat_path
        assert health_test[health_test.index("--max-age-seconds") + 1] == (
            "${CF_GATEWAY_WORKER_HEARTBEAT_MAX_AGE_SECONDS:-30}"
        )

    assert len(set(COMPOSE_WORKER_HEARTBEATS.values())) == len(COMPOSE_WORKER_HEARTBEATS)
    gateway = services["gateway"]
    assert "runtime-heartbeats:/run/cf-agent-gateway:ro" in gateway["volumes"]
    for name, heartbeat_path in GATEWAY_HEARTBEAT_ENVIRONMENTS.items():
        assert gateway["environment"][name] == heartbeat_path
    assert gateway["environment"]["CF_GATEWAY_RUNTIME_HEARTBEAT_MAX_AGE_SECONDS"] == (
        "${CF_GATEWAY_WORKER_HEARTBEAT_MAX_AGE_SECONDS:-30}"
    )
    dispatch_environment = services["dispatch-worker"]["environment"]
    assert dispatch_environment["CF_GATEWAY_WORKER_CONCURRENCY"] == (
        "${CF_GATEWAY_WORKER_CONCURRENCY:-4}"
    )
    assert dispatch_environment["CF_GATEWAY_WORKER_LEASE_SECONDS"] == (
        "${CF_GATEWAY_WORKER_LEASE_SECONDS:-60}"
    )
    assert dispatch_environment["CF_GATEWAY_WORKER_RETRY_LIMIT"] == (
        "${CF_GATEWAY_WORKER_RETRY_LIMIT:-3}"
    )
    assert {"gateway-state", "runtime-heartbeats"}.issubset(compose["volumes"])


def test_production_compose_limits_wechat_token_file_to_allowed_workers() -> None:
    compose = _load_compose()
    services = compose["services"]
    observed_mounts: set[str] = set()

    for service_name, service in services.items():
        environment = service.get("environment", {})
        assert "CF_AGENT_WECHAT_TOKEN" not in environment
        token_mounts = [mount for mount in service.get("volumes", []) if _targets_token_file(mount)]
        if service_name in TOKEN_FILE_SERVICES:
            assert environment["CF_AGENT_WECHAT_TOKEN_FILE"] == TOKEN_CONTAINER_PATH
            assert token_mounts == [
                {
                    "type": "bind",
                    "source": TOKEN_HOST_SOURCE,
                    "target": TOKEN_CONTAINER_PATH,
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            ]
            observed_mounts.add(service_name)
        else:
            assert "CF_AGENT_WECHAT_TOKEN_FILE" not in environment
            assert token_mounts == []

        non_environment_surfaces = {
            name: service.get(name) for name in ("command", "labels", "healthcheck")
        }
        assert "CF_AGENT_WECHAT_TOKEN" not in yaml.safe_dump(non_environment_surfaces)

    assert observed_mounts == TOKEN_FILE_SERVICES


def test_production_heartbeat_volume_initializer_is_bounded_and_least_privilege() -> None:
    compose = _load_compose()
    services = compose["services"]
    initializer = services["heartbeat-init"]

    assert initializer["user"] == "0:0"
    assert initializer["restart"] == "no"
    assert initializer["network_mode"] == "none"
    assert initializer["read_only"] is True
    assert initializer["env_file"] == []
    assert initializer["environment"] == {}
    assert initializer["volumes"] == ["runtime-heartbeats:/run/cf-agent-gateway"]
    assert initializer["cap_drop"] == ["ALL"]
    assert set(initializer["cap_add"]) == {"CHOWN", "FOWNER"}
    assert "no-new-privileges:true" in initializer["security_opt"]

    command = "\n".join(initializer["command"])
    assert "10001, 10001, 0o750" in command
    assert "0o777) ==" in command
    assert "chmod(path, 0o750)" in command
    assert "chmod(path, 0o777)" not in command
    assert services["migration"]["depends_on"]["heartbeat-init"]["condition"] == (
        "service_completed_successfully"
    )

    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "groupadd --gid 10001 gateway" in dockerfile
    assert "useradd --uid 10001 --gid gateway" in dockerfile
    assert "/run/cf-agent-gateway" in dockerfile
    assert "-m 0750" in dockerfile
    assert "USER 10001:10001" in dockerfile

    dockerignore = DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    assert ".git" in dockerignore
    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "!.env.example" in dockerignore
    assert "tests" not in dockerignore


def test_container_e2e_uses_isolated_postgresql_and_synthetic_adapters() -> None:
    e2e = yaml.safe_load(CONTAINER_E2E_COMPOSE_PATH.read_text(encoding="utf-8"))
    services = e2e["services"]

    assert services["postgres"]["image"] == "postgres:16-alpine"
    assert services["postgres"]["restart"] == "no"
    assert services["heartbeat-init"]["build"] == {
        "context": ".",
        "dockerfile": "docker/Dockerfile",
    }
    synthetic = services["synthetic-wechat"]
    assert synthetic["user"] == "10002:10002"
    assert synthetic["read_only"] is True
    assert synthetic["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in synthetic["security_opt"]

    config = yaml.safe_load(CONTAINER_E2E_CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["wechat"]["base_url"] == "http://synthetic-wechat:6174"
    assert config["hermes"]["base_url"] == "http://synthetic-wechat:6174"
    assert config["worker"]["enabled"] is True
    assert "postgres:16-alpine" in CONTAINER_E2E_COMPOSE_PATH.read_text(encoding="utf-8")

    runner = CONTAINER_E2E_RUNNER_PATH.read_text(encoding="utf-8")
    for evidence in (
        "ReadonlyRootfs",
        "/health/runtime",
        "restart",
        "pause",
        "updated_at",
        "ExitCode",
        "gateway-must-not-write",
        "heartbeat-init",
        "com.docker.compose.project",
        "wechat-runtime-control",
        "COMPOSE_PROJECT_NAME",
        "runtime_start_ready_timeout",
    ):
        assert evidence in runner

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "container-e2e:" in workflow
    assert "python tests/container/run_compose_e2e.py" in workflow


def test_runtime_control_uses_bounded_prepare_and_exact_container_launch() -> None:
    runtime_control = RUNTIME_CONTROL_PATH.read_text(encoding="utf-8")

    assert '["up", "--no-start", "--no-deps", "--force-recreate"' in runtime_control
    assert '["up", "--detach", "--no-deps", "--force-recreate"' not in runtime_control
    assert '["docker", "start", *controlled_container_ids]' in runtime_control
    assert '["config", "--services"]' in runtime_control
    assert "protected_services = _protected_services(" in runtime_control
    assert "if defined_after != defined_before:" in runtime_control
    assert "UNCONTROLLED_SERVICES" not in runtime_control


@pytest.mark.parametrize(
    ("unit_name", "command", "runtime_directory", "heartbeat_path"),
    [
        (
            "cf-agent-dispatch-worker",
            "cf-agent-dispatch-worker",
            "cf-agent-dispatch-worker",
            "/run/cf-agent-dispatch-worker/heartbeat.json",
        ),
        (
            "cf-agent-delivery-worker",
            "cf-agent-delivery-worker",
            "cf-agent-delivery-worker",
            "/run/cf-agent-delivery-worker/heartbeat.json",
        ),
    ],
)
def test_worker_systemd_units_are_installable_and_hardened(
    unit_name: str,
    command: str,
    runtime_directory: str,
    heartbeat_path: str,
) -> None:
    unit = _parse_unit(SYSTEMD_DIRECTORY / f"{unit_name}.service")

    assert unit["Unit"]["After"] == ["network-online.target cf-agent-gateway-migrate.service"]
    assert unit["Unit"]["Requires"] == ["cf-agent-gateway-migrate.service"]
    assert unit["Service"]["ExecStart"] == [f"/opt/cf-agent-gateway/.venv/bin/{command}"]
    assert unit["Service"]["EnvironmentFile"] == ["/etc/cf-agent-gateway/gateway.env"]
    environment = unit["Service"]["Environment"]
    assert "CF_GATEWAY_STARTUP_MIGRATION_MODE=check" in environment
    assert f"CF_GATEWAY_SERVICE={unit_name}" in environment
    assert f"CF_GATEWAY_WORKER_ID={unit_name}" in environment
    assert f"CF_GATEWAY_WORKER_HEARTBEAT_PATH={heartbeat_path}" in environment
    assert unit["Service"]["Restart"] == ["on-failure"]
    assert unit["Service"]["KillSignal"] == ["SIGTERM"]
    assert unit["Service"]["TimeoutStopSec"] == ["120s"]
    assert unit["Service"]["StateDirectory"] == ["cf-agent-gateway"]
    assert unit["Service"]["RuntimeDirectory"] == [runtime_directory]
    assert unit["Service"]["NoNewPrivileges"] == ["true"]
    assert unit["Service"]["ProtectSystem"] == ["strict"]
    assert unit["Service"]["CapabilityBoundingSet"] == [""]
    assert unit["Install"]["WantedBy"] == ["multi-user.target"]


def test_worker_deployment_configuration_and_documentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "CF_GATEWAY_WORKER_CONCURRENCY",
        "CF_GATEWAY_WORKER_LEASE_SECONDS",
        "CF_GATEWAY_WORKER_RETRY_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(ROOT / "config" / "production.yaml")
    assert settings.worker == WorkerSettings(
        enabled=True,
        concurrency=4,
        lease_seconds=60,
        retry_limit=3,
    )
    assert settings.artifact.storage_root == "/var/lib/cf-agent-gateway/artifacts"

    environment_sample = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "CF_GATEWAY_WORKER_CONCURRENCY",
        "CF_GATEWAY_WORKER_LEASE_SECONDS",
        "CF_GATEWAY_WORKER_RETRY_LIMIT",
    ):
        assert f"{name}=" in environment_sample
    assert "CF_GATEWAY_WORKER_HEARTBEAT_PATH=" not in environment_sample

    documentation = (ROOT / "docs" / "systemd-deployment.md").read_text(encoding="utf-8")
    for name, heartbeat_path in SYSTEMD_GATEWAY_HEARTBEATS.items():
        assert f"Environment={name}={heartbeat_path}" in documentation
    assert "Environment=CF_GATEWAY_RUNTIME_HEARTBEAT_MAX_AGE_SECONDS=30" in documentation
    for unit_name in ("cf-agent-dispatch-worker", "cf-agent-delivery-worker"):
        assert f"deploy/systemd/{unit_name}.service" in documentation
        assert "systemctl start" in documentation
        assert f"/run/{unit_name}/heartbeat.json" in documentation
    assert "Before=cf-agent-dispatch-worker.service cf-agent-delivery-worker.service" in (
        documentation
    )

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    assert scripts["cf-agent-dispatch-worker"].endswith("runtime.dispatch_worker:main")
    assert scripts["cf-agent-delivery-worker"].endswith("runtime.delivery_worker:main")
