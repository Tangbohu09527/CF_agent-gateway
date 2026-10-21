"""Fixed controller policies and guarded state transitions; no real service control."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_console import Harness

from cf_agent_gateway.management_console.service_control import FixedController
from cf_agent_gateway.profile_b.service import ManagementError


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path)
    value.sync()
    yield value
    value.close()


def operations(h):
    return h.app.state.console_live.control


def test_online_start_and_login_preserve_current_process_and_session(h):
    op = operations(h)
    op.settings["sources"] = {"native": {"controls_wechat_service": True}}
    calls = []
    op.controller = SimpleNamespace(request=lambda action: calls.append(action))
    assert op.start("native", "service")["state"] == "already_online_preserved"
    assert op.login("native")["state"] == "already_online_preserved"
    assert calls == []


def test_source_archive_owner_must_be_explicit_and_old_poll_stopped(h):
    op = operations(h)
    state = {"state": "available", "workers": {"poll": "running"}}
    op.controller = SimpleNamespace(request=lambda _: state)
    with pytest.raises(ManagementError, match="archive_owner_not_verified"):
        op.ensure_archive_owner("native")
    op.settings["sources"] = {"native": {"archive_owner": "candidate_archive_only"}}
    with pytest.raises(ManagementError, match="production_collector_running"):
        op.ensure_archive_owner("native")
    for unknown in ["not_found", "not_registered", "paused", "restarting"]:
        state["workers"]["poll"] = unknown
        with pytest.raises(ManagementError, match="archive_owner_not_verified"):
            op.ensure_archive_owner("native")
    state["workers"]["poll"] = "exited"
    op.ensure_archive_owner("native")


def test_controller_client_has_no_generic_command_path():
    client = FixedController("/nonexistent/test-only.sock")
    with pytest.raises(ValueError, match="controller_action_not_allowed"):
        client.request("dispatch/start")
    with pytest.raises(ValueError, match="controller_action_not_allowed"):
        client.request("sh")
    assert client.request("status")["state"] == "unavailable"


def load_controller():
    path = Path(__file__).resolve().parents[2] / "deploy/wechat-console/control.py"
    spec = importlib.util.spec_from_file_location("fixed_wechat_control_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_start_rejects_image_change_and_preserves_running(monkeypatch):
    module = load_controller()
    controller = module.Controller(
        {"wechat_container": "fixed-wechat", "wechat_image_id": "sha256:pinned"}
    )
    calls = []
    monkeypatch.setattr(
        module, "run", lambda argv: calls.append(argv) or '[{"Image":"sha256:other"}]'
    )
    with pytest.raises(ValueError, match="wechat_image_identity_changed"):
        controller.start()
    assert all("start" not in argv for argv in calls)
    monkeypatch.setattr(
        controller, "target", lambda: {"Id": "fixed-id", "State": {"Running": True}}
    )
    assert controller.start() == {"state": "already_running_preserved", "changed": False}
    assert controller.start(app=True)["state"] == "unsupported"


def test_supervisor_stopped_exit_three_can_start_only_fixed_program(monkeypatch):
    module = load_controller()
    controller = module.Controller({"app_start": "supervisor-wechat"})
    monkeypatch.setattr(
        controller, "target", lambda: {"Id": "fixed-id", "State": {"Running": True}}
    )
    commands = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=3, stdout="wechat STOPPED"),
    )
    monkeypatch.setattr(module, "run", lambda argv: commands.append(argv) or "wechat: started")
    assert controller.start(app=True)["changed"]
    assert commands == [
        ["/usr/bin/docker", "exec", "fixed-id", "/usr/bin/supervisorctl", "start", "wechat"]
    ]


def test_login_requires_fresh_observed_upstream_guard_not_operator_flag(h):
    from datetime import timedelta

    from fastapi import HTTPException

    from cf_agent_gateway.management_console.live_models import LiveObservation
    from cf_agent_gateway.management_console.service import now

    op = operations(h)
    op.settings["sources"] = {"native": {"login_contract": "expected-account-v1"}}
    value = {"status": "logged_out", "loggedInUser": h.peer.account}
    h.config.reader_factory = lambda _: SimpleNamespace(
        read=lambda *a, **k: value, close=lambda: None
    )
    op.live.probe("native", True)
    assert op.status("native")["capabilities"]["login"] is False
    with pytest.raises(HTTPException, match="target_login_identity_guard_not_verified"):
        op.login("native")
    value["loginCapabilities"] = {
        "contract": "expected-account-v1",
        "required": True,
        "preservesSessionOnMismatch": True,
    }
    op.live.probe("native", True)
    assert op.status("native")["capabilities"]["login"] is True
    with h.app.state.database_session_factory() as session:
        row = session.get(LiveObservation, "wechat:native")
        row.observed_at = now() - timedelta(seconds=21)
        session.commit()
    assert op.status("native")["capabilities"]["login"] is False
