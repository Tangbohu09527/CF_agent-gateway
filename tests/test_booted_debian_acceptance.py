"""B-entry guards only; these tests do not establish real TTY/host reboot acceptance."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def load_entry() -> ModuleType:
    source = Path(__file__).parent / "deployment/accept_booted_debian.py"
    spec = importlib.util.spec_from_file_location("boot_acceptance_under_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("missing_fd", [0, 1, 2])
def test_noninteractive_manager_auth_never_launches_sudo(
    monkeypatch: pytest.MonkeyPatch,
    missing_fd: int,
) -> None:
    entry = load_entry()
    monkeypatch.setattr(entry.os, "isatty", lambda fd: fd != missing_fd)
    monkeypatch.setattr(
        entry.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("sudo was launched"),
    )
    with pytest.raises(SystemExit, match="real interactive TTY"):
        entry.authenticate_manager("testmanager")


def test_manager_auth_inherits_stdio_without_password_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = load_entry()
    monkeypatch.setattr(entry.os, "isatty", lambda fd: True)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(entry.subprocess, "run", run)
    entry.authenticate_manager("testmanager")
    command, options = calls[0]
    assert command == ["sudo", "-H", "-u", "testmanager", "--", "sudo", "-v"]
    assert not (
        {"input", "stdin", "stdout", "stderr", "capture_output", "start_new_session"}
        & options.keys()
    )


@pytest.fixture
def report_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if os.name != "posix":
        pytest.skip("atomic reports require Debian/POSIX filesystem semantics")
    entry = load_entry()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(entry, "STATE", state)
    monkeypatch.setattr(entry, "REPORT", state / "report.json")
    # Isolate file replacement tests from the root-owned /var/lib deployment tree.
    # Preserve real file modes/types/link counts; simulate only root ownership.
    monkeypatch.setattr(entry, "report_location", lambda: None)
    original_lstat = Path.lstat

    def root_owned(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == entry.REPORT:
            values = list(info)
            values[stat.ST_UID] = 0
            values[stat.ST_GID] = 0
            return os.stat_result(values)
        return info

    def chown(fd, uid, gid):
        assert (uid, gid) == (0, 0)

    monkeypatch.setattr(Path, "lstat", root_owned)
    monkeypatch.setattr(entry.os, "fchown", chown)
    return entry


def test_report_is_private_on_first_write_and_preserves_evidence_on_failure(
    report_entry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = report_entry
    entry.write_report({"result": "awaiting reboot"})
    assert stat.S_IMODE(entry.REPORT.stat().st_mode) == 0o600
    assert entry.read_report() == {"result": "awaiting reboot"}
    before = entry.REPORT.read_bytes()
    with pytest.raises(SystemExit, match="already exists"):
        entry.write_report({"result": "overwrite"})
    assert entry.REPORT.read_bytes() == before

    def failed_replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert destination == entry.REPORT
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(entry.os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated"):
        entry.write_report({"result": "passed"}, update=True)
    assert entry.REPORT.read_bytes() == before
    assert list(entry.STATE.iterdir()) == [entry.REPORT]


def test_report_update_refuses_symlink_hardlink_and_open_permissions(report_entry) -> None:
    entry = report_entry
    target = entry.STATE / "unrelated.json"
    target.write_text(json.dumps({"keep": True}))
    entry.REPORT.symlink_to(target)
    with pytest.raises(SystemExit, match="single-link"):
        entry.write_report({"result": "passed"}, update=True)
    entry.REPORT.unlink()
    os.link(target, entry.REPORT)
    with pytest.raises(SystemExit, match="single-link"):
        entry.write_report({"result": "passed"}, update=True)
    entry.REPORT.unlink()
    entry.REPORT.write_text("{}")
    entry.REPORT.chmod(0o644)
    with pytest.raises(SystemExit, match="0600"):
        entry.read_report()
    assert json.loads(target.read_text()) == {"keep": True}


def test_manager_stage_validates_and_captures_in_same_helper(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = load_entry()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="private-stage-output")

    monkeypatch.setattr(entry.subprocess, "run", run)
    monkeypatch.setattr(
        "sys.argv", ["manager-stage", "stage", "bash", "/approved/prepare.sh", "checkout"]
    )
    exec(entry.MANAGER_STAGE, {})
    assert calls[0] == (["sudo", "-v"], {"timeout": 300})
    assert calls[1][0] == ["bash", "/approved/prepare.sh", "checkout"]
    assert calls[1][1] == {"capture_output": True, "text": True, "timeout": 1800}
    assert "private-stage-output" not in capsys.readouterr().out


def test_outer_manager_session_preserves_interactive_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = load_entry()
    monkeypatch.setattr(entry.os, "isatty", lambda fd: True)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(entry.subprocess, "run", run)
    entry.run_manager_stage("testmanager", ["bash", "/approved/prepare.sh", "checkout"])
    command, options = calls[0]
    assert command == [
        "sudo",
        "-H",
        "-u",
        "testmanager",
        "--",
        "python3",
        "-c",
        entry.MANAGER_STAGE,
        "stage",
        "bash",
        "/approved/prepare.sh",
        "checkout",
    ]
    assert options == {"timeout": 2100}


@pytest.mark.parametrize(
    ("status", "payload", "succeeds"),
    [
        (1, '{"ready":false}', True),
        (0, '{"ready":false}', False),
        (1, '{"ready":true}', False),
        (1, "", False),
        (1, "[]", False),
    ],
)
def test_after_reboot_status_authenticates_before_checking_closed_gate(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: str,
    succeeds: bool,
) -> None:
    entry = load_entry()
    calls = []
    command = ["sudo", "-n", "--", "/opt/cf-agent-gateway/deploy/wechat-runtime-control", "status"]

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv == ["sudo", "-v"]:
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, status, stdout=payload)

    monkeypatch.setattr(entry.subprocess, "run", run)
    monkeypatch.setattr("sys.argv", ["manager-stage", "closed-gate", *command])
    if succeeds:
        exec(entry.MANAGER_STAGE, {})
    else:
        with pytest.raises(SystemExit, match="B "):
            exec(entry.MANAGER_STAGE, {})
    assert calls[0] == (["sudo", "-v"], {"timeout": 300})
    assert calls[1][0] == command
    assert calls[1][1]["capture_output"] is True


def test_reboot_wait_only_observes_pending_units_until_active(monkeypatch):
    entry = load_entry()
    states = {
        "docker.service": iter(["active"]),
        "cf-agent-gateway-core.service": iter(["activating", "active"]),
    }
    calls = []

    def run(command, **kwargs):
        assert command[:2] == ["systemctl", "is-active"]
        calls.append(command[-1])
        state = next(states[command[-1]])
        return subprocess.CompletedProcess(command, 0 if state == "active" else 3, stdout=state)

    monkeypatch.setattr(entry.subprocess, "run", run)
    monkeypatch.setattr(entry.time, "sleep", lambda delay: None)
    entry.wait_boot_services()
    assert calls.count("docker.service") == 1
    assert calls.count("cf-agent-gateway-core.service") == 2


@pytest.mark.parametrize("failure", ["failed", "timeout"])
def test_reboot_wait_has_deadline_and_does_not_repair_services(monkeypatch, failure):
    entry = load_entry()
    tick = iter([0, 241])
    if failure == "timeout":
        monkeypatch.setattr(entry.time, "monotonic", lambda: next(tick))

    def run(command, **kwargs):
        assert failure == "failed"
        assert command[:2] == ["systemctl", "is-active"]
        return subprocess.CompletedProcess(command, 3, stdout="failed")

    monkeypatch.setattr(entry.subprocess, "run", run)
    with pytest.raises(SystemExit, match="timed out|failed or missing"):
        entry.wait_boot_services()
