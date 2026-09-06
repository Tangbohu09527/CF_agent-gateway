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
from types import SimpleNamespace

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
    previous_umask = os.umask(0o077)
    try:
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
    finally:
        os.umask(previous_umask)
    assert code == 1
    assert "existing_asset_differs" in capsys.readouterr().err
    assert json.loads(versions.read_text()) == existing
    assert set(state.iterdir()) == {versions, state / "install.lock"}


@pytest.mark.parametrize("failure", [None, "create", "start", "rm"])
def test_external_database_preflight_keeps_credentials_temporary_and_cleans_on_failure(
    installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    monkeypatch.setattr(installer, "STATE", tmp_path)
    preserved = tmp_path / "unrelated-record"
    protected_file(preserved, b"must remain")
    database_url = "postgresql+psycopg://operator:literal-'$%5C@192.0.2.20/gateway?sslmode=require"
    container_id = "a" * 64
    calls = []
    temporary_files = []

    def docker_preflight(arguments, *, timeout):
        calls.append(arguments)
        assert database_url not in " ".join(str(value) for value in arguments)
        assert timeout == 30
        if arguments[0] == "create":
            temporary = Path(arguments[arguments.index("--env-file") + 1])
            temporary_files.append(temporary)
            metadata = temporary.stat()
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            assert (metadata.st_uid, metadata.st_gid) == local_owner()
            assert metadata.st_nlink == 1
            assert temporary.parent == tmp_path
            assert temporary.read_text() == "CF_AGENT_GATEWAY_DATABASE_URL=" + database_url + "\n"
            assert arguments[arguments.index("--log-driver") + 1] == "none"
            assert arguments[arguments.index("--network") + 1] == "cf-internal"
            compile(arguments[-1], "external-database-preflight", "exec")
        else:
            # Cleanup must identify only the ephemeral container we just created.
            assert arguments[-1] == container_id
        if arguments[0] == failure:
            installer.fail("injected_" + failure + "_failure")
        return container_id if arguments[0] == "create" else ""

    monkeypatch.setattr(installer, "docker", docker_preflight)
    if failure:
        with pytest.raises(installer.InstallError, match="injected_" + failure) as error:
            installer.preflight_external_database("sha256:" + "b" * 64, database_url)
        assert database_url not in str(error.value)
    else:
        installer.preflight_external_database("sha256:" + "b" * 64, database_url)
    assert temporary_files and all(not path.exists() for path in temporary_files)
    assert set(tmp_path.iterdir()) == {preserved}
    assert preserved.read_bytes() == b"must remain"
    assert [call[0] for call in calls] == (
        ["create"] if failure == "create" else ["create", "start", "rm"]
    )


def test_external_database_failed_first_credentials_do_not_freeze_configuration(
    installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "STATE", tmp_path)
    inputs = valid_inputs()
    inputs["database"] = {"mode": "external", "external_url_file": "/approved/postgres-url"}
    options = SimpleNamespace(inputs="/approved/install.json", manager="operator")
    image_id = "sha256:" + "b" * 64
    current_url = "postgresql+psycopg://operator:wrong-password@192.0.2.20/gateway?sslmode=require"
    accepted_url = current_url.replace("wrong-password", "correct-password")
    preflight_urls = []
    writes = []

    def administrator_input(path, manager):
        assert manager == "operator"
        return json.dumps(inputs if path == options.inputs else {"identity": "input"}).encode()

    def secret_input(path, manager):
        assert manager == "operator"
        return current_url if path == "/approved/postgres-url" else "independent-hermes-key"

    def image_record(path):
        if path.name == "gateway-image.json":
            return {"image_id": image_id}
        assert path.name == "python-image.json"
        return {"requested_reference": "python:3.12-slim-bookworm"}

    def image_only_validation(arguments, *, data):
        assert arguments[0] == "run" and arguments[arguments.index("--network") + 1] == "none"
        assert image_id in arguments
        assert json.loads(data)["model"] == inputs["hermes_model"]
        return ""

    def connection_preflight(image, url):
        assert image == image_id
        assert not writes
        preflight_urls.append(url)
        if url != accepted_url:
            installer.fail("external_database_authentication_failed")

    class FirstPersistentWrite(Exception):
        pass

    def first_write(path, content, **kwargs):
        writes.append(path)
        raise FirstPersistentWrite

    def forbidden_secret_generation(*args):
        pytest.fail("rejected database input must not generate credentials")

    monkeypatch.setattr(installer, "administrator_input", administrator_input)
    monkeypatch.setattr(installer, "secret_input", secret_input)
    monkeypatch.setattr(installer, "read_json", image_record)
    monkeypatch.setattr(installer, "docker", image_only_validation)
    monkeypatch.setattr(installer, "preflight_external_database", connection_preflight)
    monkeypatch.setattr(installer, "once", first_write)
    monkeypatch.setattr(installer.secrets, "token_urlsafe", forbidden_secret_generation)
    with pytest.raises(installer.InstallError, match="authentication_failed"):
        installer.configure(options)
    assert not writes
    assert list(tmp_path.iterdir()) == []

    # Changing the administrator's protected input is enough to retry. The first
    # persistent write is reachable only after the corrected credential passes.
    current_url = accepted_url
    with pytest.raises(FirstPersistentWrite):
        installer.configure(options)
    assert preflight_urls == [
        accepted_url.replace("correct-password", "wrong-password"),
        accepted_url,
    ]
    assert writes == [tmp_path / "inputs.json"]


@pytest.fixture
def manager_system(installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Model account commands only; installer intent uses real protected files."""
    accounts = {}
    groups = {
        "root": SimpleNamespace(gr_name="root", gr_gid=0),
        "sudo": SimpleNamespace(gr_name="sudo", gr_gid=27),
    }
    memberships = {}
    calls = []
    monkeypatch.setattr(installer, "STATE", tmp_path)
    monkeypatch.setattr(installer, "WECHAT_ENV", tmp_path / "wechat.env")
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda name: accounts[name])
    monkeypatch.setattr(installer.pwd, "getpwall", lambda: list(accounts.values()))
    monkeypatch.setattr(installer.grp, "getgrnam", lambda name: groups[name])
    monkeypatch.setattr(installer.grp, "getgrall", lambda: list(groups.values()))
    monkeypatch.setattr(
        installer.os, "getgrouplist", lambda name, gid: [gid, *memberships.get(name, [])]
    )
    original_regular = installer.regular
    original_once = installer.once
    monkeypatch.setattr(
        installer,
        "regular",
        lambda path, **kw: original_regular(
            path, owner=kw.get("owner", local_owner()), mode=kw.get("mode", 0o600)
        ),
    )
    monkeypatch.setattr(
        installer,
        "once",
        lambda path, content, **kw: original_once(
            path, content, owner=kw.get("owner", local_owner()), mode=kw.get("mode", 0o600)
        ),
    )

    def add_account(name, uid, gid):
        accounts[name] = SimpleNamespace(
            pw_name=name, pw_uid=uid, pw_gid=gid, pw_dir="/home/" + name, pw_shell="/bin/bash"
        )
        return accounts[name]

    def run(args, **kwargs):
        calls.append(args)
        assert (tmp_path / "manager-identity.json").exists(), "intent must precede mutation"
        if args[0] == "groupadd":
            groups[args[-1]] = SimpleNamespace(gr_name=args[-1], gr_gid=int(args[2]))
        elif args[0] == "useradd":
            add_account(args[-1], int(args[2]), int(args[4]))
        else:
            pytest.fail("unexpected management account command")
        return ""

    monkeypatch.setattr(installer, "run", run)
    return SimpleNamespace(
        accounts=accounts,
        groups=groups,
        memberships=memberships,
        calls=calls,
        add_account=add_account,
        run=run,
    )


def test_new_manager_uses_unoccupied_identity_and_protected_retry_intent(
    installer, manager_system, tmp_path: Path
) -> None:
    manager_system.add_account("other", 20000, 30000)
    manager_system.groups["occupied"] = SimpleNamespace(gr_name="occupied", gr_gid=20001)
    assert installer.prepare_manager("newoperator") is True
    account = manager_system.accounts["newoperator"]
    assert (account.pw_uid, account.pw_gid) == (20002, 20002)
    intent = tmp_path / "manager-identity.json"
    assert stat.S_IMODE(intent.stat().st_mode) == 0o600 and intent.stat().st_nlink == 1
    assert json.loads(intent.read_text()) == {"manager": "newoperator", "uid": 20002, "gid": 20002}
    before = intent.read_bytes(), intent.stat().st_ino
    calls = list(manager_system.calls)
    assert installer.prepare_manager("newoperator") is True
    assert manager_system.calls == calls
    assert (intent.read_bytes(), intent.stat().st_ino) == before


def test_manager_creation_resumes_after_group_creation_without_reallocating(
    installer, manager_system, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(args, **kwargs):
        if args[0] == "useradd":
            raise installer.InstallError("injected_account_creation_failure")
        return manager_system.run(args, **kwargs)

    monkeypatch.setattr(installer, "run", interrupted)
    with pytest.raises(installer.InstallError, match="injected_account_creation_failure"):
        installer.prepare_manager("newoperator")
    saved = (tmp_path / "manager-identity.json").read_bytes()
    assert manager_system.groups["newoperator"].gr_gid == 20000
    assert "newoperator" not in manager_system.accounts
    monkeypatch.setattr(installer, "run", manager_system.run)
    assert installer.prepare_manager("newoperator") is True
    assert (tmp_path / "manager-identity.json").read_bytes() == saved
    assert [call[0] for call in manager_system.calls] == ["groupadd", "useradd"]


@pytest.mark.parametrize(
    "uid,gid,extra_groups",
    [
        (1000, 20000, []),
        (20000, 1000, []),
        (20000, 20000, [1000]),
        (10001, 20000, []),
        (20000, 20000, [10001]),
    ],
)
def test_existing_manager_service_collision_preserves_account_and_creates_no_intent(
    installer, manager_system, tmp_path: Path, uid, gid, extra_groups
) -> None:
    original = manager_system.add_account("operator", uid, gid)
    manager_system.memberships["operator"] = extra_groups
    with pytest.raises(
        installer.InstallError, match="manager_and_service_identity_must_be_separate"
    ):
        installer.prepare_manager("operator")
    assert manager_system.accounts["operator"] is original
    assert manager_system.calls == []
    assert not (tmp_path / "manager-identity.json").exists()


def test_existing_separate_manager_is_reused_without_account_or_group_changes(
    installer, manager_system, tmp_path: Path
) -> None:
    account = manager_system.add_account("operator", 1500, 1500)
    assert installer.prepare_manager("operator") is False
    assert manager_system.accounts["operator"] is account and manager_system.calls == []
    assert not (tmp_path / "manager-identity.json").exists()


def test_wechat_literal_runtime_identity_is_read_without_shell_evaluation(
    installer, manager_system, tmp_path: Path
) -> None:
    manager_system.add_account("operator", os.getuid(), os.getgid())
    protected_file(
        tmp_path / "wechat.env",
        b"OTHER=$(must-never-run)\nCF_AGENT_WECHAT_RUNTIME_UID=21000\n"
        b"CF_AGENT_WECHAT_RUNTIME_GID=22000\n",
    )
    assert installer.wechat_service_identity("operator") == (21000, 22000)
    assert manager_system.calls == []


@pytest.mark.parametrize("kind", ["uid", "gid"])
def test_actual_wechat_runtime_identity_cannot_overlap_manager(
    installer, manager_system, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    manager_system.add_account("operator", 21000 if kind == "uid" else 23000, 23000)
    manager_system.memberships["operator"] = [22000] if kind == "gid" else []
    monkeypatch.setattr(installer, "wechat_service_identity", lambda manager: (21000, 22000))
    with pytest.raises(
        installer.InstallError, match="manager_and_service_identity_must_be_separate"
    ):
        installer.manager_identity("operator")
    assert manager_system.calls == []


@pytest.mark.parametrize(
    "content",
    [
        "CF_AGENT_WECHAT_RUNTIME_UID=1000\nCF_AGENT_WECHAT_RUNTIME_GID=$(id -g)\n",
        "CF_AGENT_WECHAT_RUNTIME_UID=1000\nCF_AGENT_WECHAT_RUNTIME_UID=2000\nCF_AGENT_WECHAT_RUNTIME_GID=1000\n",
        "CF_AGENT_WECHAT_RUNTIME_UID=1000\n",
    ],
)
def test_wechat_identity_requires_unique_literal_numeric_fields(
    installer, manager_system, tmp_path: Path, content: str
) -> None:
    manager_system.add_account("operator", os.getuid(), os.getgid())
    protected_file(tmp_path / "wechat.env", content.encode())
    with pytest.raises(installer.InstallError, match="wechat_service_identity"):
        installer.wechat_service_identity("operator")
    assert manager_system.calls == []


def test_recorded_postgres_identity_cannot_overlap_manager_groups(
    installer, manager_system, tmp_path: Path
) -> None:
    manager_system.add_account("operator", 20000, 20000)
    manager_system.memberships["operator"] = [999]
    protected_file(tmp_path / "postgres-service-identity.json", b'{"uid":999,"gid":999}')
    with pytest.raises(
        installer.InstallError, match="manager_and_service_identity_must_be_separate"
    ):
        installer.manager_identity("operator")
    assert manager_system.calls == []


def test_existing_identity_conflict_precedes_source_versions_and_checkout(
    installer, manager_system, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    manager_system.add_account("operator", 1000, 1000)
    monkeypatch.setattr(installer, "platform", lambda: None)
    monkeypatch.setattr(installer, "directory", lambda path, mode=0o700: None)
    previous_umask = os.umask(0o077)
    try:
        result = installer.main(
            [
                "system-packages",
                "--manager",
                "operator",
                "--gateway-commit",
                "a" * 40,
                "--wechat-commit",
                "b" * 40,
            ]
        )
    finally:
        os.umask(previous_umask)
    assert (
        result == 1 and "manager_and_service_identity_must_be_separate" in capsys.readouterr().err
    )
    assert not (tmp_path / "source-versions.json").exists()
    assert not (tmp_path / "manager-identity.json").exists()
    assert manager_system.calls == []


def test_base_inputs_do_not_require_business_identity(installer) -> None:
    values = valid_inputs()
    values.pop("initial_identity_file")
    assert "initial_identity_file" not in installer.validate_inputs(values)


@pytest.mark.parametrize("invalid", [None, "", "  ", False, 0, {}])
def test_explicit_optional_identity_must_be_a_nonempty_path(installer, invalid) -> None:
    values = {**valid_inputs(), "initial_identity_file": invalid}
    with pytest.raises(installer.InstallError, match="invalid_optional_input"):
        installer.validate_inputs(values)


def test_business_inputs_do_not_freeze_or_mutate_base_installation(
    installer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(installer, "STATE", tmp_path)
    original_regular = installer.regular
    monkeypatch.setattr(
        installer,
        "regular",
        lambda path, **kwargs: original_regular(path, owner=local_owner(), **kwargs),
    )
    original_once = installer.once
    monkeypatch.setattr(
        installer,
        "once",
        lambda path, content: original_once(path, content, owner=local_owner()),
    )
    base = installer.validate_inputs(valid_inputs())
    base.pop("initial_identity_file")
    installer.preserve_installation_inputs(base)
    path = tmp_path / "inputs.json"
    before = path.stat()
    installer.preserve_installation_inputs({**base, "initial_identity_file": "/new/business.json"})
    installer.preserve_installation_inputs(base)
    assert json.loads(path.read_text()) == base
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_ino == before.st_ino
    with pytest.raises(installer.InstallError, match="existing_asset_differs"):
        installer.preserve_installation_inputs({**base, "hermes_model": "changed-base-model"})


def test_legacy_optional_identity_path_is_preserved_but_not_a_base_conflict(
    installer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(installer, "STATE", tmp_path)
    legacy = installer.validate_inputs(valid_inputs())
    path = tmp_path / "inputs.json"
    protected_file(path, installer.encoded(legacy).encode())
    original_regular = installer.regular
    monkeypatch.setattr(
        installer, "regular", lambda target: original_regular(target, owner=local_owner())
    )
    saved = path.read_bytes()
    base = {key: value for key, value in legacy.items() if key != "initial_identity_file"}
    installer.preserve_installation_inputs(base)
    assert path.read_bytes() == saved


def test_optional_initialize_absence_checks_database_without_business_mutation(
    installer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(installer, "STATE", tmp_path)
    calls = []
    monkeypatch.setattr(installer, "database_ready", lambda: calls.append("database_check"))
    monkeypatch.setattr(installer, "business_status", lambda: calls.append("business_read_only"))
    monkeypatch.setattr(installer, "compose", lambda *a, **kw: pytest.fail("identity applied"))
    installer.initialize()
    assert calls == ["database_check", "business_read_only"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage", ["start", "diagnose"])
def test_core_operations_do_not_apply_or_recheck_business_identity(
    installer, tmp_path, monkeypatch, stage
) -> None:
    monkeypatch.setattr(installer, "STATE", tmp_path)
    # A stored legacy onboarding request is not authority to restore a disabled employee.
    protected_file(tmp_path / "initial-identity.json", b'{"legacy":"preserve"}')
    calls = []
    monkeypatch.setattr(installer, "initialize", lambda **kw: pytest.fail("business was reset"))
    monkeypatch.setattr(installer, "database_ready", lambda: calls.append("schema_checked"))
    monkeypatch.setattr(installer, "core_ready", lambda: calls.append("healthy"))
    monkeypatch.setattr(installer, "business_status", lambda: calls.append("business_read_only"))
    monkeypatch.setattr(installer, "once", lambda *a, **kw: None)
    monkeypatch.setattr(installer, "compose", lambda command, **kw: calls.append(command) or "{}")
    getattr(installer, stage)()
    assert calls[0] == "schema_checked"
    assert "healthy" in calls
    assert (tmp_path / "initial-identity.json").read_bytes() == b'{"legacy":"preserve"}'
    commands = [call for call in calls if isinstance(call, list)]
    assert all("cf_agent_gateway.initial_identity" not in command for command in commands)
    assert all("--allow-model-call" not in command for command in commands)
    if stage == "diagnose":
        assert "business_read_only" in calls


@pytest.mark.parametrize("invalid_identity", [None, [], False, {}, {"version": 1}])
def test_explicit_bad_identity_is_rejected_before_configuration_is_saved(
    installer, tmp_path, monkeypatch, invalid_identity
) -> None:
    from cf_agent_gateway.initial_identity import InitialIdentityConfig

    monkeypatch.setattr(installer, "STATE", tmp_path)
    values = valid_inputs()
    options = SimpleNamespace(inputs="/approved/install.json", manager="operator")
    monkeypatch.setattr(
        installer,
        "administrator_input",
        lambda path, manager: json.dumps(
            values if path == options.inputs else invalid_identity
        ).encode(),
    )
    monkeypatch.setattr(installer, "secret_input", lambda *args: "independent-hermes-key")
    monkeypatch.setattr(
        installer,
        "read_json",
        lambda path: (
            {"image_id": "sha256:" + "b" * 64}
            if path.name == "gateway-image.json"
            else {"requested_reference": "python:3.12-slim-bookworm"}
        ),
    )

    def application_validation(command, *, data):
        assert command[command.index("--network") + 1] == "none"
        payload = json.loads(data)
        assert payload["identity_supplied"] is True
        InitialIdentityConfig.model_validate(payload["identity"])
        pytest.fail("invalid application identity was accepted")

    monkeypatch.setattr(installer, "docker", application_validation)
    monkeypatch.setattr(installer, "once", lambda *a, **kw: pytest.fail("configuration saved"))
    with pytest.raises(ValueError):
        installer.configure(options)
    assert list(tmp_path.iterdir()) == []
