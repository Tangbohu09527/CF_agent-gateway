from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from cf_agent_gateway.config import WorkerSettings, load_settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.prod.yml"
SYSTEMD_DIRECTORY = ROOT / "deploy" / "systemd"

WORKERS = {
    "dispatch-worker": {
        "module": "cf_agent_gateway.runtime.dispatch_worker",
        "service": "cf-agent-dispatch-worker",
        "heartbeat": "/run/cf-agent-gateway/dispatch-worker-heartbeat.json",
    },
    "delivery-worker": {
        "module": "cf_agent_gateway.runtime.delivery_worker",
        "service": "cf-agent-delivery-worker",
        "heartbeat": "/run/cf-agent-gateway/delivery-worker-heartbeat.json",
    },
}


def _load_compose() -> dict[str, object]:
    loaded = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


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
    assert {"migration", "gateway", "worker", *WORKERS}.issubset(services)

    heartbeat_paths: set[str] = set()
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
        assert "/run/cf-agent-gateway:size=1m,mode=1777" in service["tmpfs"]

        environment = service["environment"]
        assert environment["CF_GATEWAY_SERVICE"] == expected["service"]
        assert environment["CF_GATEWAY_WORKER_ID"] == expected["service"]
        heartbeat_path = environment["CF_GATEWAY_WORKER_HEARTBEAT_PATH"]
        assert heartbeat_path == expected["heartbeat"]
        heartbeat_paths.add(heartbeat_path)

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

    assert len(heartbeat_paths) == len(WORKERS)
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
    assert "gateway-state" in compose["volumes"]


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
