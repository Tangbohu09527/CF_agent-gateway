"""Exercise installer preservation guards without a daemon, network, or root.

These tests use real POSIX files. Only pytest's shared temporary ancestors are
presented as protected directories, matching the installer's /var/lib location.
The files and directories under each test's root retain their actual metadata.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="installer requires POSIX file semantics"
)


@pytest.fixture
def installer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    script = Path(__file__).parents[1] / "deploy/prepare-clean-host.py"
    spec = importlib.util.spec_from_file_location("clean_install_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # CI pytest directories may descend from world-writable /tmp. Simulate only
    # those ancestors as the protected /var/lib tree; never alter target metadata.
    ancestors = set(tmp_path.parents)
    original_lstat = Path.lstat

    def protected_ancestors(path: Path, *args, **kwargs):
        result = original_lstat(path, *args, **kwargs)
        if path in ancestors:
            values = list(result)
            values[stat.ST_MODE] &= ~0o022
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", protected_ancestors)
    return module


def local_owner() -> tuple[int, int]:
    return os.getuid(), os.getgid()


def protected_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def valid_inputs() -> dict:
    return {
        "version": 1,
        "hermes_url": "http://192.0.2.50:8765",
        "hermes_model": "approved-test-model",
        "hermes_api_key_file": "/approved/hermes-key",
        "initial_identity_file": "/approved/identity.json",
        "database": {"mode": "managed"},
    }


def test_once_reuses_secret_without_touching_inode_or_permissions(
    installer, tmp_path: Path
) -> None:
    path = tmp_path / "secret"
    installer.once(path, "original-test-secret", owner=local_owner())
    before = path.stat()
    installer.once(path, "original-test-secret", owner=local_owner())
    after = path.stat()
    assert path.read_text() == "original-test-secret"
    assert (after.st_ino, after.st_mtime_ns, stat.S_IMODE(after.st_mode)) == (
        before.st_ino,
        before.st_mtime_ns,
        0o600,
    )
    assert after.st_nlink == 1


def test_once_conflict_preserves_existing_secret_and_reports_only_path(
    installer, tmp_path: Path
) -> None:
    path = tmp_path / "secret"
    installer.once(path, "preserve-this-secret", owner=local_owner())
    with pytest.raises(installer.InstallError, match="existing_asset_differs") as error:
        installer.once(path, "new-secret-must-not-replace", owner=local_owner())
    assert path.read_text() == "preserve-this-secret"
    assert "preserve-this-secret" not in str(error.value)
    assert "new-secret-must-not-replace" not in str(error.value)


def test_once_failure_before_publish_cleans_only_its_temp_and_can_retry(
    installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    unrelated = tmp_path / "preserved-business-record"
    protected_file(unrelated, b"retained")
    original_fsync = installer.os.fsync

    def fail_fsync(_descriptor):
        raise OSError("injected disk failure")

    monkeypatch.setattr(installer.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="injected disk failure"):
        installer.once(path, "test-secret", owner=local_owner())
    assert not path.exists()
    assert list(tmp_path.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"retained"
    monkeypatch.setattr(installer.os, "fsync", original_fsync)
    installer.once(path, "test-secret", owner=local_owner())
    assert path.read_text() == "test-secret"


@pytest.mark.parametrize("operation", ["once", "regular"])
def test_symlink_never_follows_or_replaces_target(
    installer, tmp_path: Path, operation: str
) -> None:
    target = tmp_path / "real-secret"
    protected_file(target, b"original")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(installer.InstallError, match="symlink_conflict"):
        if operation == "once":
            installer.once(link, "replacement", owner=local_owner())
        else:
            installer.regular(link, owner=local_owner())
    assert link.is_symlink()
    assert target.read_bytes() == b"original"


def test_hardlinked_secret_is_rejected_without_modification(installer, tmp_path: Path) -> None:
    target = tmp_path / "secret"
    protected_file(target, b"retained")
    os.link(target, tmp_path / "alias")
    with pytest.raises(installer.InstallError, match="file_permission_conflict"):
        installer.once(target, b"retained", owner=local_owner())
    assert target.read_bytes() == b"retained"
    assert target.stat().st_nlink == 2


def test_parent_write_permission_conflict_does_not_get_chmod_fixed(
    installer, tmp_path: Path
) -> None:
    parent = tmp_path / "unsafe-parent"
    parent.mkdir(mode=0o770)
    parent.chmod(0o770)
    with pytest.raises(installer.InstallError, match="writable_path_conflict"):
        installer.once(parent / "secret", "test-secret", owner=local_owner())
    assert stat.S_IMODE(parent.stat().st_mode) == 0o770
    assert list(parent.iterdir()) == []


def test_file_permission_conflict_does_not_get_chmod_fixed(installer, tmp_path: Path) -> None:
    path = tmp_path / "secret"
    protected_file(path, b"retained")
    path.chmod(0o644)
    with pytest.raises(installer.InstallError, match="file_permission_conflict"):
        installer.once(path, b"retained", owner=local_owner())
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert path.read_bytes() == b"retained"


def test_administrator_input_requires_owner_only_file(installer, tmp_path: Path) -> None:
    import pwd

    manager = pwd.getpwuid(os.getuid()).pw_name
    path = tmp_path / "input.json"
    protected_file(path, b'{"version":1}')
    assert installer.administrator_input(str(path), manager) == b'{"version":1}'
    path.chmod(0o644)
    with pytest.raises(installer.InstallError, match="input_requires_owner_only_regular_file"):
        installer.administrator_input(str(path), manager)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "http://192.0.2.50:8765/v1",
        "http://user:password@192.0.2.50:8765",
        "http://192.0.2.50:8765?key=secret",
        "http://192.0.2.50:8765/#fragment",
    ],
)
def test_hermes_input_cannot_point_back_to_gateway_container_or_embed_credentials(
    installer, url: str
) -> None:
    inputs = valid_inputs()
    inputs["hermes_url"] = url
    with pytest.raises(installer.InstallError):
        installer.validate_inputs(inputs)


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"allow_all_users": True},
        {"database": {"mode": "auto"}},
        {"database": {"mode": "managed", "external_url_file": "/secret"}},
        {"database": {"mode": "external"}},
        {"hermes_api_key_file": ""},
    ],
)
def test_invalid_install_inputs_fail_before_asset_generation(installer, change: dict) -> None:
    with pytest.raises(installer.InstallError):
        installer.validate_inputs({**valid_inputs(), **change})


def test_environment_values_reject_line_injection(installer) -> None:
    with pytest.raises(installer.InstallError, match="invalid_environment_value"):
        installer.dotenv({"HERMES_API_KEY": "approved\nCF_GATEWAY_IMAGE=attacker"})


def test_source_version_conflict_stops_before_checkout_or_external_command(
    installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    versions = state / "source-versions.json"
    existing = {
        "gateway_commit": "a" * 40,
        "wechat_commit": "b" * 40,
        "manager": "operator",
        "configuration_version": 1,
    }
    protected_file(versions, installer.encoded(existing).encode())
    monkeypatch.setattr(installer, "STATE", state)
    monkeypatch.setattr(installer, "platform", lambda: None)

    # The state directory has already been prepared; permission guard tests above
    # exercise actual metadata independently. Run this ordering test as any UID.
    def supplied_state_directory(path, mode=0o700):
        assert path == state and path.is_dir() and mode == 0o700

    original_regular = installer.regular

    def regular_as_test_owner(path, **kwargs):
        assert path == versions
        return original_regular(path, owner=local_owner(), mode=kwargs.get("mode", 0o600))

    def forbidden_external_command(*args, **kwargs):
        pytest.fail("version conflict must stop before any external command")

    monkeypatch.setattr(installer, "directory", supplied_state_directory)
    monkeypatch.setattr(installer, "regular", regular_as_test_owner)
    monkeypatch.setattr(installer, "run", forbidden_external_command)
    code = installer.main(
        [
            "build",
            "--manager",
            "operator",
            "--gateway-commit",
            "c" * 40,
            "--wechat-commit",
            "b" * 40,
        ]
    )
    assert code == 1
    assert "existing_asset_differs" in capsys.readouterr().err
    assert json.loads(versions.read_text()) == existing
    assert set(state.iterdir()) == {versions, state / "install.lock"}
