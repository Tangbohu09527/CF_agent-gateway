"""Persistent listener changes preserve all unrelated dotenv bytes and journals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_return_bridge_enablement import (
    OLD_SECRET,
    _cli,
    _cli_request,
    apply,
    bridge_settings,
    hermes_config,
)

from cf_agent_gateway.hermes.return_bridge import enablement as deploy


def _plan(tmp_path):
    path, _ = hermes_config(tmp_path)
    return deploy.plan_hermes(
        path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path)
    )


@pytest.mark.parametrize(
    "binding",
    [
        b"API_SERVER_HOST=192.168.1.232",
        b" API_SERVER_HOST = 192.168.1.232  # existing listener",
        b"export API_SERVER_HOST='192.168.1.232'\t# approved",
        b'\tAPI_SERVER_HOST="192.168.1.232"',
        b"'API_SERVER_HOST' = '192.168.1.232'",
    ],
)
@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_exact_listener_value_patch_preserves_bom_comments_credentials_and_port(
    tmp_path, binding, newline
):
    env_path = tmp_path / ".env"
    raw = (
        b"\xef\xbb\xbf# synthetic private fixture"
        + newline
        + b"API_SERVER_KEY="
        + OLD_SECRET.encode()
        + newline
        + binding
        + newline
        + b'API_SERVER_PORT="8642" # retained port'
        + newline
        + b"MODEL_VALUE='unrelated\nAPI_SERVER_HOST=do-not-change-this-line\nvalue'"
        + newline
        + "COMMENT=保留原文".encode()
    )
    env_path.write_bytes(raw)
    plan = _plan(tmp_path)
    change = next(change for change in plan.changes if change.path == env_path)
    assert change.before == raw
    assert change.after == raw.replace(b"192.168.1.232", b"127.0.0.1")
    assert change.fields == ("API_SERVER_HOST",)
    assert deploy.listener_values(change.after) == {
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": "8642",
    }
    assert OLD_SECRET not in repr(plan) + repr(plan.changes) + json.dumps(plan.summary())
    receipt = apply(plan, tmp_path)
    index = plan.changes.index(change)
    manifest = Path(receipt["manifest"])
    assert (manifest.parent / f"{index}.before").read_bytes() == raw
    assert OLD_SECRET not in manifest.read_text()
    assert env_path.read_bytes() == change.after
    deploy.rollback(manifest)
    assert env_path.read_bytes() == raw


@pytest.mark.parametrize(
    "original, expected",
    [
        (None, b"API_SERVER_HOST=127.0.0.1\n"),
        (b"", b"API_SERVER_HOST=127.0.0.1\n"),
        (b"\xef\xbb\xbf", b"\xef\xbb\xbfAPI_SERVER_HOST=127.0.0.1\n"),
        (
            b"# keep\r\nAPI_SERVER_PORT=8642",
            b"# keep\r\nAPI_SERVER_PORT=8642\r\nAPI_SERVER_HOST=127.0.0.1\r\n",
        ),
        (b"API_SERVER_HOST=", b"API_SERVER_HOST=127.0.0.1"),
    ],
)
def test_missing_or_empty_host_is_persistent_with_exact_rollback(tmp_path, original, expected):
    path = tmp_path / ".env"
    if original is not None:
        path.write_bytes(original)
    plan = _plan(tmp_path)
    assert next(change.after for change in plan.changes if change.path == path) == expected
    receipt = apply(plan, tmp_path)
    assert path.read_bytes() == expected
    deploy.rollback(receipt["manifest"])
    assert (path.read_bytes() if path.exists() else None) == original


@pytest.mark.parametrize(
    "raw, error",
    [
        (b"API_SERVER_HOST=one\nexport API_SERVER_HOST='two'", "duplicate_listener"),
        (b"API_SERVER_PORT=8642\n'API_SERVER_PORT'=8642", "duplicate_listener"),
        (b"api_server_host=192.168.1.232", "ambiguous_listener"),
        (b"API_SERVER_HOST=127.0.0.1\napi_server_host=192.168.1.232", "ambiguous_listener"),
        (b"API_Server_Host=192.168.1.232\nAPI_SERVER_HOST=127.0.0.1", "ambiguous_listener"),
        (b"api_server_port=9999", "ambiguous_listener"),
        (b"API_SERVER_PORT=8642\nApi_Server_Port=9999", "ambiguous_listener"),
        (b"API_SERVER_HOST=${BIND_HOST}", "ambiguous_listener"),
        (b"API_SERVER_HOST=", None),
        (b"API_SERVER_HOST", "ambiguous_listener"),
        (b'API_SERVER_HOST="127.0.0.1"bad', "ambiguous_listener"),
        (b'API_SERVER_HOST="127.0.0.1\\n"', "ambiguous_listener"),
        (b'API_SERVER_HOST="127.0.0.1\n"', "ambiguous_listener"),
        (b"API_SERVER_HOST=one#not-a-comment", "ambiguous_listener"),
        (b"API_SERVER_PORT=0", "invalid_listener"),
        (b"API_SERVER_PORT=65536", "invalid_listener"),
        (b"API_SERVER_PORT=invalid", "invalid_listener"),
        (b"UNRELATED='unterminated\nAPI_SERVER_HOST=127.0.0.1", "ambiguous_listener"),
        (b"UNRELATED='closed' junk\nAPI_SERVER_HOST=127.0.0.1", "ambiguous_listener"),
        (b"# invalid\xff\nAPI_SERVER_HOST=127.0.0.1", "unsupported_listener"),
        (b"API_SERVER_HOST=127.0.0.1\x00", "ambiguous_listener"),
    ],
)
def test_ambiguous_dotenv_is_rejected_without_writes_or_secret_values(tmp_path, raw, error):
    path = tmp_path / ".env"
    path.write_bytes(raw)
    if error is None:
        _plan(tmp_path)
    else:
        with pytest.raises(deploy.EnablementError, match=error) as caught:
            _plan(tmp_path)
        assert str(caught.value) not in {raw.decode(errors="replace"), OLD_SECRET}
    assert path.read_bytes() == raw
    assert not (tmp_path / "plugins").exists()


def test_dotenv_drift_rejects_apply_and_rollback_before_any_other_asset_write(tmp_path):
    path = tmp_path / ".env"
    raw = b"API_SERVER_HOST=192.168.1.232\n"
    path.write_bytes(raw)
    plan = _plan(tmp_path)
    path.write_bytes(raw + b"# concurrent edit\n")
    with pytest.raises(deploy.EnablementError, match="asset_changed_since_plan"):
        apply(plan, tmp_path)
    assert not (tmp_path / "plugins").exists()
    path.write_bytes(raw)
    receipt = apply(plan, tmp_path)
    changed = path.read_bytes() + b"# newer edit\n"
    path.write_bytes(changed)
    plugin = tmp_path / "plugins/cf-artifact-return/__init__.py"
    with pytest.raises(deploy.EnablementError, match="rollback_conflicts"):
        deploy.rollback(receipt["manifest"])
    assert path.read_bytes() == changed and plugin.is_file()


def test_auth_probe_uses_same_parser_without_treating_multiline_body_as_a_key():
    raw = (
        b"UNRELATED='first\nAPI_SERVER_KEY=not-a-real-binding\nlast'\r\n"
        b"\texport 'API_SERVER_KEY' = '"
        + OLD_SECRET.encode()
        + b"' # existing authentication\r\nAPI_SERVER_HOST=127.0.0.1\r\n"
    )
    assert deploy.http_env_values(raw) == {
        "API_SERVER_KEY": OLD_SECRET,
        "API_SERVER_HOST": "127.0.0.1",
    }
    assert "API_SERVER_KEY" not in deploy.listener_values(raw)
    for suffix in (b"export API_SERVER_KEY=duplicate", b"API_SERVER_PORT=${PORT}"):
        with pytest.raises(deploy.EnablementError):
            deploy.http_env_values(raw + suffix)
    for raw_key in (
        b"api_server_key=lowercase-synthetic-key",
        b"API_SERVER_KEY=canonical-synthetic-key\nApi_Server_Key=overridden-synthetic-key",
    ):
        with pytest.raises(deploy.EnablementError, match="ambiguous_listener_dotenv_field"):
            deploy.http_env_values(raw_key)


def test_windows_uppercase_alias_cannot_override_the_reviewed_host(tmp_path):
    # os.environ on Windows canonicalizes via upper(), not Unicode casefold().
    raw = "API_SERVER_HOST=127.0.0.1\nAPı_SERVER_HOST=192.0.2.5\n".encode()
    (tmp_path / ".env").write_bytes(raw)
    with pytest.raises(deploy.EnablementError, match="ambiguous_listener_dotenv_field"):
        _plan(tmp_path)
    assert (tmp_path / ".env").read_bytes() == raw


def test_real_process_crash_after_dotenv_write_resumes_then_restores_private_original(tmp_path):
    env_path = tmp_path / ".env"
    raw = (
        b"# private fixture\r\nAPI_SERVER_HOST=192.168.1.232\r\nAPI_SERVER_KEY="
        + OLD_SECRET.encode()
    )
    env_path.write_bytes(raw)
    config, _request, summary, arguments = _cli_request(tmp_path)
    crashed = _cli(tmp_path, arguments, crash_before=config)
    assert crashed.returncode == 86
    assert env_path.read_bytes() == raw.replace(b"192.168.1.232", b"127.0.0.1")
    inode = env_path.stat().st_ino
    resumed = _cli(tmp_path, arguments)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert env_path.stat().st_ino == inode
    assert OLD_SECRET not in resumed.stdout + resumed.stderr
    receipt = json.loads(resumed.stdout)
    assert receipt["plan_sha256"] == summary["plan_sha256"]
    rolled_back = _cli(tmp_path, ["rollback", "--manifest", receipt["manifest"]])
    assert rolled_back.returncode == 0
    assert env_path.read_bytes() == raw


def test_new_plan_preserves_already_applied_bundle_and_old_journal(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"API_SERVER_HOST=192.168.1.232\n")
    first = _plan(tmp_path)
    # Reproduce the old twelve-asset plan without mutating its saved manifest.
    legacy = deploy.Plan(
        "hermes", tuple(change for change in first.changes if change.path != env_path), ()
    )
    old_receipt = apply(legacy, tmp_path)
    old_manifest = Path(old_receipt["manifest"])
    old_bytes = old_manifest.read_bytes()
    config_path = tmp_path / "hermes.yaml"
    revised = deploy.plan_hermes(
        config_path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path)
    )
    assert [change.path for change in revised.changes if change.before != change.after] == [
        env_path
    ]
    new_receipt = apply(revised, tmp_path)
    assert old_manifest.read_bytes() == old_bytes
    deploy.rollback(new_receipt["manifest"])
    assert env_path.read_bytes() == b"API_SERVER_HOST=192.168.1.232\n"
    assert old_manifest.read_bytes() == old_bytes
    deploy.rollback(old_receipt["manifest"])
