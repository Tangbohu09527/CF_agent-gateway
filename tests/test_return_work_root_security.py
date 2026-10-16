"""Work-root lifecycle guards; native tests touch only fresh pytest directories."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

import pytest
from test_return_site_windows import fixture as site_fixture
from test_return_site_windows import peer, stop

from cf_agent_gateway.hermes.return_bridge import site_windows as site
from cf_agent_gateway.hermes.return_bridge import windows_work_root as security

fixture = site_fixture
CURRENT_USER = security.current_user


@pytest.mark.parametrize("change", ["elevated", "different_user", "different_runtime"])
def test_identity_guard_rejects_before_stop_or_work_root_change(fixture, monkeypatch, change):
    request, config, _, _, actions, _ = fixture
    summary, _ = site.plan(request)
    before = config.read_bytes()
    work = Path(site.request_defaults(request)["work_root"])
    monkeypatch.setattr(security, "current_user", CURRENT_USER)
    original = dict(summary["runtime_user"])

    def token(pid):
        value = dict(original)
        if change == "elevated" and pid == os.getpid():
            value["elevated"] = True
        if (change == "different_user" and pid == os.getpid()) or (
            change == "different_runtime" and pid != os.getpid()
        ):
            value["sid"] = "S-1-5-21-123-456-789-1002"
        return value

    monkeypatch.setattr(security, "process_user", token)
    with pytest.raises(
        site.en.EnablementError, match="identity_required|user_changed|user_mismatch"
    ):
        site.execute(
            request,
            "stop",
            authorized=True,
            expected_plan_sha256=summary["plan_sha256"],
            peer=peer(request, "quiesced"),
        )
    assert actions == [] and not work.exists() and config.read_bytes() == before
    if change == "elevated":
        assert not Path(site.request_defaults(request)["state_directory"]).exists()


def test_existing_bad_root_is_rejected_during_readonly_plan(fixture, monkeypatch):
    request, config, _, _, actions, _ = fixture
    root = Path(site.request_defaults(request)["work_root"])
    root.mkdir()
    sentinel = root / "preserve.txt"
    sentinel.write_bytes(b"existing-child")
    before = config.read_bytes(), sentinel.read_bytes(), root.stat()

    def denied(*args, **kwargs):
        raise security.WorkRootError("work_root_owner_mismatch")

    monkeypatch.setattr(security, "check", denied)
    with pytest.raises(security.WorkRootError, match="owner_mismatch"):
        site.plan(request)
    assert (config.read_bytes(), sentinel.read_bytes(), root.stat()) == before
    assert actions == []


def test_apply_identity_change_cannot_create_root_or_edit_assets(fixture, monkeypatch):
    request, config, _, _, actions, _ = fixture
    summary = stop(fixture)
    before = config.read_bytes()
    root = Path(site.request_defaults(request)["work_root"])
    monkeypatch.setattr(security, "current_user", CURRENT_USER)
    monkeypatch.setattr(
        security, "process_user", lambda _: {**summary["runtime_user"], "elevated": True}
    )
    with pytest.raises(security.WorkRootError, match="ordinary_hermes_identity_required"):
        site.execute(
            request,
            "apply",
            authorized=True,
            expected_plan_sha256=summary["plan_sha256"],
            peer=peer(request, "quiesced"),
        )
    assert actions == ["stop"] and not root.exists() and config.read_bytes() == before


def test_started_gateway_must_keep_the_frozen_ordinary_user(fixture, monkeypatch):
    request = fixture[0]
    summary = stop(fixture)
    site.execute(
        request,
        "apply",
        authorized=True,
        expected_plan_sha256=summary["plan_sha256"],
        peer=peer(request, "quiesced"),
    )
    monkeypatch.setattr(
        security, "process_user", lambda _: {**summary["runtime_user"], "elevated": True}
    )
    with pytest.raises(site.SiteError, match="started_runtime_user_mismatch"):
        site.execute(
            request,
            "start",
            authorized=True,
            expected_plan_sha256=summary["plan_sha256"],
            peer=peer(request, "applied"),
        )
    assert fixture[4] == ["stop", "start"]


def test_legacy_rollback_before_apply_does_not_require_new_work_root(fixture, monkeypatch):
    request = fixture[0]
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    site.execute(request, "rollback", peer=peer(request, "rolled_back"), **options)
    observations = []

    def existing_only(path, user, *, missing_allowed=False):
        observations.append(missing_allowed)
        assert not path.exists()
        if not missing_allowed:
            raise security.WorkRootError("work_root_missing")

    monkeypatch.setattr(security, "check", existing_only)
    site.execute(request, "start-legacy", peer=peer(request, "rolled_back"), **options)
    assert observations and all(observations)
    assert fixture[4] == ["stop", "start"]


def test_work_root_drift_blocks_start_but_cannot_block_bound_quiesce(fixture, monkeypatch):
    request = fixture[0]
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    site.execute(request, "apply", peer=peer(request, "applied"), **options)
    original = security.check

    def denied(*args, **kwargs):
        raise security.WorkRootError("work_root_owner_mismatch")

    monkeypatch.setattr(security, "check", denied)
    with pytest.raises(security.WorkRootError, match="owner_mismatch"):
        site.execute(request, "start", peer=peer(request, "applied"), **options)
    assert fixture[4] == ["stop"]
    monkeypatch.setattr(security, "check", original)
    site.execute(request, "start", peer=peer(request, "applied"), **options)
    monkeypatch.setattr(security, "check", denied)
    site.execute(request, "quiesce", peer=peer(request, "quiesced"), **options)
    assert fixture[4] == ["stop", "start", "stop"]


def _sddl(path):
    kernel, advapi = security._api()
    owner, group, dacl, sd = (wintypes.LPVOID() for _ in range(4))
    assert (
        advapi.GetNamedSecurityInfoW(
            str(path),
            1,
            7,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            None,
            ctypes.byref(sd),
        )
        == 0
    )
    text = wintypes.LPWSTR()
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    try:
        assert convert(sd, 1, 7, ctypes.byref(text), None)
        return text.value
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.LocalFree(sd)


@pytest.mark.skipif(os.name != "nt", reason="requires a real ordinary Windows token")
def test_native_atomic_root_owner_access_and_existing_children_unchanged(tmp_path):
    user = security.current_user()
    assert user["elevated"] is False
    root = tmp_path / "new-return-work"
    security.check(root, user, missing_allowed=True)
    assert not root.exists()
    security.prepare(root, user)
    security.check(root, user)
    child = root / "sentinel.txt"
    child.write_bytes(b"isolated-existing-child")
    before = _sddl(root), _sddl(child), child.stat(), child.read_bytes()
    security.prepare(root, user)
    assert (_sddl(root), _sddl(child), child.stat(), child.read_bytes()) == before


@pytest.mark.skipif(os.name != "nt", reason="requires a real ordinary Windows token")
def test_native_existing_unknown_dacl_is_not_repaired(tmp_path):
    user = security.current_user()
    root = tmp_path / "existing-inherited-root"
    root.mkdir()
    child = root / "sentinel.txt"
    child.write_bytes(b"existing-state")
    before = _sddl(root), _sddl(child), child.stat(), child.read_bytes()
    with pytest.raises(security.WorkRootError, match="work_root_policy_mismatch"):
        security.prepare(root, user)
    assert (_sddl(root), _sddl(child), child.stat(), child.read_bytes()) == before
