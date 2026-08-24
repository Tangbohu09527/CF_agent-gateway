from __future__ import annotations

import json
import os
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "deploy" / "wechat-runtime-control"
CONTROLLED_SERVICES = ("worker", "delivery-worker")
TOKEN_PATH = "/run/secrets/cf-agent-wechat-auth-token"


class FakeClock:
    def __init__(self) -> None:
        self.current = 100.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


class FakeDocker:
    def __init__(
        self,
        token_file: Path,
        *,
        controlled_running: bool = True,
        clock: FakeClock | None = None,
        advance_after_up: float = 0,
    ) -> None:
        self.secret_sentinel = "inspect-secret-must-not-leak"
        self.token_source = os.path.abspath(token_file)
        self.clock = clock
        self.advance_after_up = advance_after_up
        self.calls: list[list[str]] = []
        self.states = {
            "gateway": {"running": True, "health": "healthy"},
            "postgres": {"running": True, "health": "healthy"},
            "dispatch-worker": {"running": True, "health": "healthy"},
            "worker": {"running": controlled_running, "health": "healthy"},
            "delivery-worker": {"running": controlled_running, "health": "healthy"},
        }
        self.heartbeat_ages = {"worker": 1.25, "delivery-worker": 2.5}

    def __call__(
        self,
        arguments: list[str],
        *,
        deadline: Any,
        error_code: str,
    ) -> str:
        assert error_code
        deadline.remaining()
        self.calls.append(list(arguments))
        if arguments[:2] == ["docker", "inspect"]:
            return self._inspect(arguments)
        assert arguments[:2] == ["docker", "compose"]
        command = arguments[6:]
        action = command[0]
        if action == "config":
            mount = {
                "type": "bind",
                "source": self.token_source,
                "target": TOKEN_PATH,
                "read_only": True,
            }
            return json.dumps(
                {"services": {service: {"volumes": [mount]} for service in CONTROLLED_SERVICES}}
            )
        if action == "ps":
            service = command[-1]
            return f"container-{service}\n" if service in self.states else ""
        if action == "stop":
            assert command[:2] == ["stop", "--timeout"]
            assert float(command[2]) > 0
            services = tuple(command[3:])
            assert services == CONTROLLED_SERVICES
            for service in services:
                self.states[service]["running"] = False
            return ""
        if action == "up":
            assert command[:4] == ["up", "--detach", "--no-deps", "--force-recreate"]
            assert tuple(command[4:]) == CONTROLLED_SERVICES
            for service in CONTROLLED_SERVICES:
                self.states[service] = {"running": True, "health": "healthy"}
            if self.clock is not None:
                self.clock.current += self.advance_after_up
            return ""
        if action == "exec":
            service = command[2]
            return f"{self.heartbeat_ages[service]:.6f}\n"
        raise AssertionError(f"unexpected fake Docker command: {arguments!r}")

    def _inspect(self, arguments: list[str]) -> str:
        service = arguments[-1].removeprefix("container-")
        state = self.states[service]
        environment = [f"UNRELATED_SECRET={self.secret_sentinel}"]
        mounts: list[dict[str, Any]] = []
        if service in CONTROLLED_SERVICES:
            environment.append(f"CF_AGENT_WECHAT_TOKEN_FILE={TOKEN_PATH}")
            mounts.append(
                {
                    "Destination": TOKEN_PATH,
                    "Type": "bind",
                    "RW": False,
                    "Source": self.token_source,
                }
            )
        payload = [
            {
                "Config": {"Env": environment},
                "Mounts": mounts,
                "State": {
                    "Running": state["running"],
                    "Health": {"Status": state["health"]},
                },
            }
        ]
        return json.dumps(payload)


def _secure_token_file(tmp_path: Path) -> Path:
    token_file = tmp_path / "auth-token"
    token_file.write_text("control-test-token", encoding="ascii")
    token_file.chmod(0o600)
    return token_file


def _main(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeDocker,
) -> Callable[[list[str]], int]:
    namespace = runpy.run_path(str(CONTROL_PATH))
    main = namespace["main"]
    assert callable(main)
    globals_ = main.__globals__
    globals_["_run"] = fake
    if os.name == "posix":
        globals_["SERVICE_IDENTITY"] = (os.getuid(), os.getgid())
    if fake.clock is not None:
        monkeypatch.setattr(globals_["time"], "monotonic", fake.clock.monotonic)
        monkeypatch.setattr(globals_["time"], "sleep", fake.clock.sleep)
    monkeypatch.delenv("CF_GATEWAY_WORKER_HEARTBEAT_MAX_AGE_SECONDS", raising=False)
    return main


def test_contract_outputs_only_the_versioned_public_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path))
    main = _main(monkeypatch, fake)

    assert main(["contract"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "contract_version": 1,
        "poll_worker_service": "worker",
        "delivery_worker_service": "delivery-worker",
        "dispatch_worker_service": "dispatch-worker",
        "token_mode": "file",
        "token_container_path": TOKEN_PATH,
    }
    assert captured.err == ""
    assert fake.calls == []


def test_stop_controls_only_poll_and_delivery_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path))
    main = _main(monkeypatch, fake)

    assert main(["stop", "--timeout-seconds", "2"]) == 0

    assert fake.states["worker"]["running"] is False
    assert fake.states["delivery-worker"]["running"] is False
    for service in ("gateway", "postgres", "dispatch-worker"):
        assert fake.states[service]["running"] is True
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"stopped": True}
    assert captured.err == ""


def test_start_uses_no_dependencies_and_waits_until_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False)
    main = _main(monkeypatch, fake)

    assert main(["start", "--timeout-seconds", "2"]) == 0

    up_call = next(call for call in fake.calls if call[6:7] == ["up"])
    assert up_call[6:] == [
        "up",
        "--detach",
        "--no-deps",
        "--force-recreate",
        "worker",
        "delivery-worker",
    ]
    for forbidden in ("gateway", "postgres", "dispatch-worker", "migration"):
        assert forbidden not in up_call[6:]
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True


def test_status_is_redacted_and_reports_maximum_heartbeat_age(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path))
    main = _main(monkeypatch, fake)

    assert main(["status"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "worker_health": "healthy",
        "delivery_health": "healthy",
        "heartbeat_age": 2.5,
        "token_contract_valid": True,
        "ready": True,
    }
    serialized = captured.out + captured.err
    assert fake.secret_sentinel not in serialized


def test_start_uses_one_wall_clock_timeout_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    fake = FakeDocker(
        _secure_token_file(tmp_path),
        controlled_running=False,
        clock=clock,
        advance_after_up=2.1,
    )
    main = _main(monkeypatch, fake)

    assert main(["start", "--timeout-seconds", "2"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "runtime_start_failed"}
    compose_actions = [call[6] for call in fake.calls if call[:2] == ["docker", "compose"]]
    assert compose_actions == ["config", "up"]


def test_status_reports_invalid_host_token_in_the_stable_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = _secure_token_file(tmp_path)
    token_file.write_bytes(b"invalid\n")
    fake = FakeDocker(token_file)
    main = _main(monkeypatch, fake)

    assert main(["status"]) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "worker_health": "unknown",
        "delivery_health": "unknown",
        "heartbeat_age": None,
        "token_contract_valid": False,
        "ready": False,
    }
    assert captured.err == ""


def test_start_rejects_a_host_token_symlink_before_compose_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = _secure_token_file(tmp_path)
    link = tmp_path / "auth-token-link"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    fake = FakeDocker(link, controlled_running=False)
    main = _main(monkeypatch, fake)

    assert main(["start"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "token_file_invalid"}
    assert not any(call[6:7] == ["up"] for call in fake.calls)
