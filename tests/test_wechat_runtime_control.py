from __future__ import annotations

import json
import os
import runpy
import sys
import time
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
        self.container_ids = {service: f"container-{service}" for service in self.states} | {
            "migration": "container-migration"
        }
        self.heartbeat_ages = {"worker": 1.25, "delivery-worker": 2.5}
        mount = {
            "type": "bind",
            "source": self.token_source,
            "target": TOKEN_PATH,
            "read_only": True,
        }
        self.rendered_services = {
            service: {"environment": {}, "volumes": []}
            for service in ("gateway", "dispatch-worker", "migration")
        }
        for service in CONTROLLED_SERVICES:
            self.rendered_services[service] = {
                "environment": {"CF_AGENT_WECHAT_TOKEN_FILE": TOKEN_PATH},
                "volumes": [dict(mount)],
            }
        self.missing_containers: set[str] = set()
        self.invalid_token_attestation: set[str] = set()
        self.inspect_failures_remaining: dict[str, int] = {}
        self.stop_error = False
        self.stop_keeps_running: set[str] = set()
        self.up_error = False
        self.start_error_after: int | None = None
        self.mutate_uncontrolled_after_prepare: str | None = None
        self.duplicate_controlled_ids = False

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
        if arguments[:2] == ["docker", "start"]:
            identifiers = arguments[2:]
            assert len(identifiers) == len(CONTROLLED_SERVICES)
            for index, identifier in enumerate(identifiers):
                if self.start_error_after == index:
                    raise RuntimeError(self.secret_sentinel)
                service = self._service_for_container(identifier)
                assert service in CONTROLLED_SERVICES
                self.states[service]["running"] = True
            return "\n".join(identifiers) + "\n"
        assert arguments[:2] == ["docker", "compose"]
        command = arguments[6:]
        action = command[0]
        if action == "config":
            return json.dumps({"services": self.rendered_services})
        if action == "ps":
            service = command[-1]
            if service in self.missing_containers:
                return ""
            identifier = self.container_ids.get(service)
            return f"{identifier}\n" if identifier is not None else ""
        if action == "stop":
            assert command[:2] == ["stop", "--timeout"]
            assert float(command[2]) > 0
            services = tuple(command[3:])
            assert services == CONTROLLED_SERVICES
            if self.stop_error:
                raise RuntimeError(self.secret_sentinel)
            for service in services:
                if service not in self.stop_keeps_running:
                    self.states[service]["running"] = False
            return ""
        if action == "up":
            assert command[:4] == ["up", "--no-start", "--no-deps", "--force-recreate"]
            assert tuple(command[4:]) == CONTROLLED_SERVICES
            if self.up_error:
                raise RuntimeError(self.secret_sentinel)
            for service in CONTROLLED_SERVICES:
                self.states[service]["running"] = False
                self.container_ids[service] = f"prepared-{service}"
            if self.duplicate_controlled_ids:
                self.container_ids["delivery-worker"] = self.container_ids["worker"]
            if self.mutate_uncontrolled_after_prepare is not None:
                service = self.mutate_uncontrolled_after_prepare
                self.container_ids[service] = f"changed-{service}"
            if self.clock is not None:
                self.clock.current += self.advance_after_up
            return ""
        if action == "exec":
            service = command[2]
            age = self.heartbeat_ages[service]
            return "" if age is None else f"{age:.6f}\n"
        raise AssertionError(f"unexpected fake Docker command: {arguments!r}")

    def _inspect(self, arguments: list[str]) -> str:
        service = self._service_for_container(arguments[-1])
        failures_remaining = self.inspect_failures_remaining.get(service, 0)
        if failures_remaining:
            self.inspect_failures_remaining[service] = failures_remaining - 1
            return self.secret_sentinel
        state = self.states[service]
        environment = [f"UNRELATED_SECRET={self.secret_sentinel}"]
        mounts: list[dict[str, Any]] = []
        if service in CONTROLLED_SERVICES and service not in self.invalid_token_attestation:
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

    def _service_for_container(self, identifier: str) -> str:
        return next(
            service
            for service, container_id in self.container_ids.items()
            if identifier == container_id
        )


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


def test_start_prepares_only_controlled_services_and_launches_exact_container_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    fake = FakeDocker(
        _secure_token_file(tmp_path),
        controlled_running=False,
        clock=clock,
    )
    main = _main(monkeypatch, fake)

    assert main(["start", "--timeout-seconds", "2"]) == 0

    prepare_call = next(call for call in fake.calls if call[6:7] == ["up"])
    assert prepare_call[6:] == [
        "up",
        "--no-start",
        "--no-deps",
        "--force-recreate",
        "worker",
        "delivery-worker",
    ]
    for forbidden in ("gateway", "postgres", "dispatch-worker", "migration"):
        assert forbidden not in prepare_call[6:]
    assert not any(
        call[6:8] == ["up", "--detach"] for call in fake.calls if call[:2] == ["docker", "compose"]
    )
    assert _docker_start_calls(fake) == [
        ["docker", "start", "prepared-worker", "prepared-delivery-worker"]
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["worker_health"] == payload["delivery_health"] == "healthy"
    assert payload["heartbeat_age"] == 2.5
    assert payload["token_contract_valid"] is True
    assert payload["ready"] is True
    assert clock.current == 100.0
    assert "stop" not in _compose_actions(fake)
    _assert_uncontrolled_running(fake)


def test_start_preserves_uncontrolled_container_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False)
    protected_before = {
        service: fake.container_ids[service]
        for service in ("gateway", "postgres", "dispatch-worker", "migration")
    }
    main = _main(monkeypatch, fake)

    assert main(["start", "--timeout-seconds", "2"]) == 0

    assert {
        service: fake.container_ids[service] for service in protected_before
    } == protected_before


def test_start_fails_closed_if_prepare_changes_an_uncontrolled_container_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False)
    fake.mutate_uncontrolled_after_prepare = "gateway"
    main = _main(monkeypatch, fake)

    assert main(["start", "--timeout-seconds", "2"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "runtime_start_prepare_failed"}
    assert _docker_start_calls(fake) == []
    assert not any(fake.states[service]["running"] for service in CONTROLLED_SERVICES)
    _assert_uncontrolled_running(fake)


@pytest.mark.parametrize("failure", ["missing", "duplicate"])
def test_start_requires_two_unique_controlled_container_ids(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False)
    if failure == "missing":
        fake.missing_containers.add("worker")
    else:
        fake.duplicate_controlled_ids = True
    main = _main(monkeypatch, fake)

    assert main(["start", "--timeout-seconds", "2"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "runtime_start_prepare_failed"}
    assert _docker_start_calls(fake) == []
    assert not any(fake.states[service]["running"] for service in CONTROLLED_SERVICES)
    _assert_uncontrolled_running(fake)


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


def test_start_prepare_uses_the_shared_wall_clock_budget(
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
    assert json.loads(captured.err) == {"error_code": "runtime_start_prepare_failed"}
    actions = _compose_actions(fake)
    assert actions.count("config") == 1
    assert "up" in actions
    assert "stop" in actions
    assert _docker_start_calls(fake) == []
    assert not any(fake.states[service]["running"] for service in CONTROLLED_SERVICES)
    _assert_uncontrolled_running(fake)

    _assert_redacted(captured.err, fake)


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
    assert _docker_start_calls(fake) == []
    assert "stop" not in _compose_actions(fake)


def _compose_actions(fake: FakeDocker) -> list[str]:
    return [call[6] for call in fake.calls if call[:2] == ["docker", "compose"]]


def _docker_start_calls(fake: FakeDocker) -> list[list[str]]:
    return [call for call in fake.calls if call[:2] == ["docker", "start"]]


def _assert_uncontrolled_running(fake: FakeDocker) -> None:
    for service in ("gateway", "postgres", "dispatch-worker"):
        assert fake.states[service]["running"] is True
    mutating_calls = [
        call
        for call in fake.calls
        if call[:2] == ["docker", "start"]
        or (call[:2] == ["docker", "compose"] and call[6:7] in (["up"], ["stop"]))
    ]
    for service in ("gateway", "postgres", "dispatch-worker", "migration"):
        assert all(service not in call for call in mutating_calls)


PREFLIGHT_FAILURES = [
    ("worker", "plaintext"),
    ("delivery-worker", "plaintext"),
    ("worker", "plaintext_empty"),
    ("worker", "file_missing"),
    ("delivery-worker", "file_wrong"),
    ("worker", "mount_missing"),
    ("delivery-worker", "mount_duplicate"),
    ("worker", "mount_writable"),
    ("delivery-worker", "mount_type"),
    ("delivery-worker", "source_mismatch"),
] + [
    (service, defect)
    for service in ("gateway", "dispatch-worker", "migration")
    for defect in ("plaintext", "file", "mount")
]


@pytest.mark.parametrize("case", PREFLIGHT_FAILURES, ids=lambda case: "-".join(case))
def test_start_rejects_invalid_rendered_token_contract_before_up(
    case: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False)
    service, defect = case
    definition = fake.rendered_services[service]
    environment = definition["environment"]
    mounts = definition["volumes"]
    if defect == "plaintext":
        environment["CF_AGENT_WECHAT_TOKEN"] = fake.secret_sentinel
    elif defect == "plaintext_empty":
        environment["CF_AGENT_WECHAT_TOKEN"] = ""
    elif defect == "file_missing":
        environment.pop("CF_AGENT_WECHAT_TOKEN_FILE")
    elif defect == "file_wrong":
        environment["CF_AGENT_WECHAT_TOKEN_FILE"] = f"{TOKEN_PATH}-wrong"
    elif defect == "mount_missing":
        mounts.clear()
    elif defect == "mount_duplicate":
        mounts.append(dict(mounts[0]))
    elif defect == "mount_writable":
        mounts[0]["read_only"] = False
    elif defect == "mount_type":
        mounts[0]["type"] = "volume"
    elif defect == "source_mismatch":
        mounts[0]["source"] = f"{fake.token_source}-other"
    elif defect == "file":
        environment["CF_AGENT_WECHAT_TOKEN_FILE"] = TOKEN_PATH
    else:
        mount = dict(fake.rendered_services["worker"]["volumes"][0])
        mount["target"] = "/run/secrets/unexpected-path"
        mounts.append(mount)

    main = _main(monkeypatch, fake)
    assert main(["start"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "token_source_conflict"}
    assert _compose_actions(fake) == ["config"]
    assert not any(fake.states[service]["running"] for service in CONTROLLED_SERVICES)
    _assert_uncontrolled_running(fake)
    _assert_redacted(captured.err, fake)


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("worker_unhealthy", "runtime_start_ready_timeout"),
        ("delivery_unhealthy", "runtime_start_ready_timeout"),
        ("worker_heartbeat_stale", "runtime_start_ready_timeout"),
        ("delivery_heartbeat_missing", "runtime_start_ready_timeout"),
        ("token_attestation", "runtime_start_ready_timeout"),
        ("worker_not_created", "runtime_start_prepare_failed"),
        ("inspect", "runtime_control_unavailable"),
    ],
)
def test_start_failure_rolls_back_both_controlled_workers(
    failure: str,
    error_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False, clock=clock)
    if failure == "worker_unhealthy":
        fake.states["worker"]["health"] = "unhealthy"
    elif failure == "delivery_unhealthy":
        fake.states["delivery-worker"]["health"] = "unhealthy"
    elif failure == "worker_heartbeat_stale":
        fake.heartbeat_ages["worker"] = 31
    elif failure == "delivery_heartbeat_missing":
        fake.heartbeat_ages["delivery-worker"] = None
    elif failure == "token_attestation":
        fake.invalid_token_attestation.add("worker")
    elif failure == "worker_not_created":
        fake.missing_containers.add("worker")
    else:
        fake.inspect_failures_remaining["worker"] = 1

    main = _main(monkeypatch, fake)
    assert main(["start", "--timeout-seconds", "0.1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": error_code}
    actions = _compose_actions(fake)
    assert actions.count("up") == 1
    assert actions.count("stop") == 1
    if failure == "worker_not_created":
        assert _docker_start_calls(fake) == []
    else:
        assert _docker_start_calls(fake) == [
            ["docker", "start", "prepared-worker", "prepared-delivery-worker"]
        ]
    assert not any(fake.states[service]["running"] for service in CONTROLLED_SERVICES)
    _assert_uncontrolled_running(fake)
    _assert_redacted(captured.err, fake)


@pytest.mark.parametrize(
    ("failure", "worker_running", "delivery_running"),
    [("stop_error", True, True), ("worker_still_running", True, False)],
)
def test_start_reports_when_rollback_cannot_confirm_both_workers_stopped(
    failure: str,
    worker_running: bool,
    delivery_running: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False, clock=clock)
    fake.states["worker"]["health"] = "unhealthy"
    if failure == "stop_error":
        fake.stop_error = True
    else:
        fake.stop_keeps_running.add("worker")

    main = _main(monkeypatch, fake)
    assert main(["start", "--timeout-seconds", "0.1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "runtime_start_rollback_failed"}
    assert fake.states["worker"]["running"] is worker_running
    assert fake.states["delivery-worker"]["running"] is delivery_running
    _assert_uncontrolled_running(fake)
    _assert_redacted(captured.err, fake)


def test_start_prepare_failure_rolls_back_only_controlled_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False)
    fake.up_error = True
    main = _main(monkeypatch, fake)

    assert main(["start"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "runtime_start_prepare_failed"}
    assert "up" in _compose_actions(fake)
    assert "stop" in _compose_actions(fake)
    assert _docker_start_calls(fake) == []
    assert not any(fake.states[service]["running"] for service in CONTROLLED_SERVICES)
    _assert_uncontrolled_running(fake)
    _assert_redacted(captured.err, fake)


def test_start_partial_launch_rolls_back_both_controlled_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeDocker(_secure_token_file(tmp_path), controlled_running=False)
    fake.start_error_after = 1
    main = _main(monkeypatch, fake)

    assert main(["start", "--timeout-seconds", "2"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "runtime_start_launch_failed"}
    assert _docker_start_calls(fake) == [
        ["docker", "start", "prepared-worker", "prepared-delivery-worker"]
    ]
    assert not any(fake.states[service]["running"] for service in CONTROLLED_SERVICES)
    _assert_uncontrolled_running(fake)
    _assert_redacted(captured.err, fake)


def _install_blocking_legacy_docker(tmp_path: Path) -> Path:
    implementation = tmp_path / "fake_docker.py"
    implementation.write_text(
        r"""
import json
import os
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
log_path = Path(os.environ["FAKE_DOCKER_LOG"])
with log_path.open("a", encoding="utf-8") as log_file:
    log_file.write(json.dumps(arguments) + "\n")

token_source = os.path.abspath(os.environ["FAKE_DOCKER_TOKEN"])
token_path = "/run/secrets/cf-agent-wechat-auth-token"
controlled = {"worker", "delivery-worker"}

if arguments[0] == "compose":
    command = arguments[5:]
    action = command[0]
    if action == "config":
        mount = {
            "type": "bind",
            "source": token_source,
            "target": token_path,
            "read_only": True,
        }
        services = {
            service: {"environment": {}, "volumes": []}
            for service in ("gateway", "dispatch-worker", "migration")
        }
        for service in controlled:
            services[service] = {
                "environment": {"CF_AGENT_WECHAT_TOKEN_FILE": token_path},
                "volumes": [mount],
            }
        print(json.dumps({"services": services}))
    elif action == "ps":
        print(f"cid-{command[-1]}")
    elif action == "up":
        if "--detach" in command:
            time.sleep(30)
    elif action == "exec":
        print("0.100000")
    elif action == "stop":
        pass
    else:
        raise SystemExit(2)
elif arguments[0] == "start":
    print("\n".join(arguments[1:]))
elif arguments[0] == "inspect":
    service = arguments[1].removeprefix("cid-")
    environment = ["UNRELATED_SECRET=must-not-leak"]
    mounts = []
    if service in controlled:
        environment.append(f"CF_AGENT_WECHAT_TOKEN_FILE={token_path}")
        mounts.append(
            {
                "Destination": token_path,
                "Type": "bind",
                "RW": False,
                "Source": token_source,
            }
        )
    print(
        json.dumps(
            [
                {
                    "Config": {"Env": environment},
                    "Mounts": mounts,
                    "State": {
                        "Running": True,
                        "Health": {"Status": "healthy"},
                    },
                }
            ]
        )
    )
else:
    raise SystemExit(2)
""".lstrip(),
        encoding="utf-8",
    )
    return implementation


def test_blocking_legacy_compose_up_executable_does_not_gate_new_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = _secure_token_file(tmp_path)
    implementation = _install_blocking_legacy_docker(tmp_path)
    log_path = tmp_path / "docker-calls.jsonl"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log_path))
    monkeypatch.setenv("FAKE_DOCKER_TOKEN", str(token_file))
    namespace = runpy.run_path(str(CONTROL_PATH))
    main = namespace["main"]
    if os.name == "posix":
        main.__globals__["SERVICE_IDENTITY"] = (os.getuid(), os.getgid())
    actual_run = main.__globals__["_run"]

    def run_fake_docker(
        arguments: list[str],
        *,
        deadline: Any,
        error_code: str,
    ) -> str:
        assert arguments[0] == "docker"
        return actual_run(
            [sys.executable, str(implementation), *arguments[1:]],
            deadline=deadline,
            error_code=error_code,
        )

    main.__globals__["_run"] = run_fake_docker

    started_at = time.monotonic()
    assert main(["start", "--timeout-seconds", "10"]) == 0
    elapsed = time.monotonic() - started_at

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ready"] is True
    assert captured.err == ""
    assert elapsed < 8
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    compose_up = next(call for call in calls if call[:1] == ["compose"] and "up" in call)
    assert "--no-start" in compose_up
    assert "--detach" not in compose_up
    assert ["start", "cid-worker", "cid-delivery-worker"] in calls


def _assert_redacted(output: str, fake: FakeDocker) -> None:
    assert fake.secret_sentinel not in output and fake.token_source not in output


def test_subprocess_timeout_uses_deadline_code_and_returns_after_bounded_cleanup() -> None:
    namespace = runpy.run_path(str(CONTROL_PATH))
    deadline = namespace["Deadline"](
        time.monotonic() + 0.1,
        "runtime_start_ready_timeout",
    )
    started_at = time.monotonic()

    with pytest.raises(namespace["ControlError"], match="runtime_start_ready_timeout"):
        namespace["_run"](
            [sys.executable, "-c", "import time; time.sleep(30)"],
            deadline=deadline,
            error_code="runtime_control_unavailable",
        )

    assert time.monotonic() - started_at < 5
