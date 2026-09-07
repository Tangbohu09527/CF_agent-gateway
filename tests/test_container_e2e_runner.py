from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_runner() -> ModuleType:
    path = Path(__file__).parent / "container" / "run_compose_e2e.py"
    spec = importlib.util.spec_from_file_location("container_e2e_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("container E2E runner could not be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _heartbeat_set(sequence: int) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "state": "running",
            "updated_at": f"2026-08-23T08:00:0{sequence}Z",
        }
        for name in runner.HEARTBEAT_FILES
    }


def test_continuous_heartbeat_proof_requires_multiple_complete_renewals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = [_heartbeat_set(sequence) for sequence in range(3)]
    calls: list[dict[str, dict[str, Any]]] = []
    monkeypatch.setattr(runner, "_heartbeat_payloads", lambda compose, environment: snapshots[0])

    def advance(
        compose: list[str],
        environment: dict[str, str],
        previous: dict[str, dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, dict[str, Any]]:
        del compose, environment, kwargs
        calls.append(previous)
        return snapshots[len(calls)]

    monkeypatch.setattr(runner, "_wait_for_heartbeat_advances", advance)

    result = runner._assert_continuous_heartbeat_updates(
        ["docker", "compose"],
        {},
        renewal_cycles=2,
    )

    assert calls == snapshots[:2]
    assert result == snapshots[2]
    with pytest.raises(ValueError, match="at least two"):
        runner._assert_continuous_heartbeat_updates([], {}, renewal_cycles=1)


def test_stale_heartbeat_proof_pauses_running_file_until_it_ages_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _heartbeat_set(0)
    frozen = _heartbeat_set(1)
    recovered = _heartbeat_set(2)
    commands: list[list[str]] = []
    # A heartbeat may advance while Docker is still processing pause.
    heartbeat_reads = iter((before, frozen, frozen))
    recovery_baselines: list[dict[str, dict[str, Any]]] = []

    def run(
        arguments: list[str],
        *,
        environment: dict[str, str],
        check: bool = True,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del environment, check, kwargs
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(runner, "_run", run)
    monkeypatch.setattr(
        runner,
        "_heartbeat_payloads",
        lambda compose, environment: next(heartbeat_reads),
    )
    monkeypatch.setattr(
        runner,
        "_wait_for_component_status",
        lambda *args, **kwargs: {"status": "degraded"},
    )

    def advance(
        compose: list[str],
        environment: dict[str, str],
        previous: dict[str, dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, dict[str, Any]]:
        del compose, environment
        assert kwargs["names"] == ("dispatch-worker-heartbeat.json",)
        recovery_baselines.append(previous)
        return recovered

    monkeypatch.setattr(runner, "_wait_for_heartbeat_advances", advance)
    monkeypatch.setattr(runner, "_wait_for_healthy", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        runner,
        "_runtime_health",
        lambda port: {"components": {"dispatch_worker": {"status": "ok"}}},
    )

    runner._assert_stale_heartbeat_and_recovery(["docker", "compose"], {}, 8080)

    assert commands[0][-2:] == ["pause", "dispatch-worker"]
    assert commands[1][-2:] == ["unpause", "dispatch-worker"]
    assert recovery_baselines == [frozen]


@pytest.mark.parametrize("exit_code", [1, 137, 143])
def test_worker_clean_stop_rejects_nonzero_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    monkeypatch.setattr(runner, "_container_id", lambda *args: "worker-container")
    monkeypatch.setattr(
        runner,
        "_inspect",
        lambda *args: {
            "State": {
                "Running": False,
                "ExitCode": exit_code,
                "OOMKilled": False,
            }
        },
    )

    with pytest.raises(AssertionError, match=f"code {exit_code}"):
        runner._wait_for_clean_stop([], "worker", {})


def test_gateway_sigterm_exit_requires_complete_ordered_shutdown_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = "\n".join(runner.GATEWAY_CLEAN_SHUTDOWN_MARKERS)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(["docker", "logs"], 0, "", logs)

    monkeypatch.setattr(runner, "_run", run)
    inspected = {"Id": "gateway-container", "State": {"ExitCode": 143}}

    runner._assert_gateway_shutdown_completed({}, inspected)

    monkeypatch.setattr(
        runner,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["docker", "logs"],
            0,
            "",
            runner.GATEWAY_CLEAN_SHUTDOWN_MARKERS[-1],
        ),
    )
    with pytest.raises(AssertionError, match="sequence is incomplete"):
        runner._assert_gateway_shutdown_completed({}, inspected)


def test_cleanup_retries_and_verifies_resources_on_every_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifications: list[str] = []
    down_calls: list[str] = []

    def verify(project_name: str, environment: dict[str, str]) -> None:
        del environment
        verifications.append(project_name)

    monkeypatch.setattr(runner, "_assert_project_resources_removed", verify)

    def failed_down(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        down_calls.append("failed")
        return subprocess.CompletedProcess(["compose", "down"], 9, "", "")

    monkeypatch.setattr(runner, "_run", failed_down)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    with pytest.raises(AssertionError, match="compose down returned 9"):
        runner._cleanup_project([], {}, "failed-project")

    assert down_calls == ["failed", "failed"]
    assert verifications == ["failed-project"]
    monkeypatch.setattr(
        runner,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["compose", "down"], 0, "", ""),
    )
    runner._cleanup_project([], {}, "successful-project")
    assert verifications == ["failed-project", "successful-project"]


def test_controller_fixture_uses_effective_isolated_compose_and_unchanged_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    compose = ["docker", "compose", "--file", "production", "--file", "e2e"]
    environment = {"COMPOSE_PROJECT_NAME": "isolated-e2e"}
    rendered = {
        "name": "isolated-e2e",
        "services": {
            "worker": {
                "environment": {"SENTINEL": "fixture-private-value"},
                "volumes": [
                    {"source": "/absolute/auth-token", "target": runner.TOKEN_CONTAINER_PATH}
                ],
                "networks": {"default": None},
            },
            "postgres": {"networks": {"default": None}},
        },
        "networks": {"default": {"name": "isolated-e2e_default", "external": False}},
        "volumes": {"runtime-heartbeats": {"name": "isolated-e2e_runtime-heartbeats"}},
    }
    commands = []

    def run(arguments, **kwargs):
        commands.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, json.dumps(rendered), "")

    monkeypatch.setattr(runner, "_run", run)
    fixture = tmp_path / "controller-root"
    controller = runner._prepare_runtime_control_fixture(compose, environment, fixture)
    assert commands == [
        (
            compose + ["config", "--format", "json"],
            {
                "environment": environment,
                "capture_output": True,
            },
        )
    ]
    assert controller == fixture / "deploy" / "wechat-runtime-control"
    assert controller.read_bytes() == runner.RUNTIME_CONTROL.read_bytes()
    actual = json.loads((fixture / "docker-compose.prod.yml").read_text())
    assert actual == rendered
    if os.name == "posix":
        assert stat.S_IMODE(fixture.stat().st_mode) == 0o700
        assert stat.S_IMODE((fixture / "docker-compose.prod.yml").stat().st_mode) == 0o600
        assert stat.S_IMODE(controller.stat().st_mode) == 0o755
    assert "fixture-private-value" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "network",
    [
        {"name": "cf-internal", "external": True},
        {"name": "other-project_default", "external": False},
    ],
)
def test_controller_fixture_refuses_a_shared_or_different_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, network: dict
) -> None:
    monkeypatch.setattr(
        runner,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["compose", "config"],
            0,
            json.dumps({"networks": {"default": network}}),
            "",
        ),
    )
    fixture = tmp_path / "controller-root"
    with pytest.raises(AssertionError, match="isolated project network"):
        runner._prepare_runtime_control_fixture(
            [], {"COMPOSE_PROJECT_NAME": "isolated-e2e"}, fixture
        )
    assert not fixture.exists()


def test_runtime_control_invocation_uses_prepared_fixed_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = []

    def run(arguments, **kwargs):
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    monkeypatch.setattr(runner, "_run", run)
    controller = tmp_path / "deploy" / "wechat-runtime-control"
    runner._run_runtime_control("start", {}, timeout_seconds=30, runtime_control=controller)
    assert commands[0][-4:] == [str(controller), "start", "--timeout-seconds", "30"]
    assert commands[0][0] == "sudo"
