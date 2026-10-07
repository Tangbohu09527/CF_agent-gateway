"""Configuration-only activation in isolated directories; no service is operated."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from cf_agent_gateway.hermes.return_bridge import enablement as deploy

OLD_SECRET = "synthetic-existing-key-must-never-appear-in-plan"


def hermes_config(tmp_path: Path):
    path = tmp_path / "hermes.yaml"
    config = {
        "model": {"default": "keep-model", "provider": "keep-provider", "api_key": OLD_SECRET},
        "platforms": {
            "api_server": {
                "enabled": True,
                "host": "0.0.0.0",
                "port": 8642,
                "extra": {"host": "192.0.2.2", "key": OLD_SECRET},
            }
        },
        "platform_toolsets": {"api_server": ["terminal", "vision"]},
        "plugins": {"enabled": ["existing-plugin"], "entries": {"existing-plugin": {"keep": True}}},
        "skills": {"keep": "unchanged"},
    }
    path.write_text(
        "# preserve exact bytes on rollback\n" + yaml.safe_dump(config), encoding="utf-8"
    )
    return path, config


def bridge_settings(tmp_path):
    return {
        "gateway_origin": "https://gateway.invalid:8443",
        "work_root": str(tmp_path / "tasks"),
        "profile_reference": "existing-profile",
        "tls_host": "127.0.0.1",
        "tls_port": 9443,
        "tls_cert_file": str(tmp_path / "existing-server.pem"),
        "tls_key_file": str(tmp_path / "existing-server-key.pem"),
        "gateway_ca_file": str(tmp_path / "existing-gateway-ca.pem"),
    }


def apply(plan, tmp_path):
    return deploy.apply_plan(
        plan, tmp_path / "private-journal", expected_plan_sha256=plan.summary()["plan_sha256"]
    )


def test_hermes_bundle_plan_apply_and_exact_rollback_preserve_existing_config(tmp_path):
    path, original = hermes_config(tmp_path)
    raw = path.read_bytes()
    plugin = tmp_path / "plugins/cf-artifact-return"
    plan = deploy.plan_hermes(path, plugin, bridge_settings(tmp_path))
    public = json.dumps(plan.summary()) + repr(plan) + repr(plan.changes)
    assert OLD_SECRET not in public
    assert path.read_bytes() == raw and not plugin.exists()
    receipt = apply(plan, tmp_path)
    changed = yaml.safe_load(path.read_bytes())
    assert changed["model"] == original["model"]
    assert changed["skills"] == original["skills"]
    assert changed["platforms"]["api_server"]["port"] == 8642
    assert changed["platforms"]["api_server"]["host"] == "127.0.0.1"
    assert changed["platforms"]["api_server"]["extra"]["host"] == "127.0.0.1"
    assert changed["platforms"]["api_server"]["extra"]["key"] == OLD_SECRET
    assert changed["plugins"]["entries"]["existing-plugin"] == {"keep": True}
    assert changed["plugins"]["enabled"] == ["existing-plugin", "cf-artifact-return"]
    assert changed["platform_toolsets"]["api_server"] == [
        "terminal",
        "vision",
        "cf_artifact_return",
    ]
    assert (plugin / "lib/cf_agent_gateway/hermes/return_bridge/plugin.py").is_file()
    assert not (plugin / "lib/cf_agent_gateway/database.py").exists()
    from test_artifact_return_imports import _cold_script

    imported = _cold_script(
        "import runpy,sys\n"
        f"plugin=runpy.run_path({str(plugin / '__init__.py')!r})\n"
        "assert callable(plugin['register'])\n"
        "assert 'sqlalchemy' not in sys.modules\n"
        "assert 'cf_agent_gateway.database' not in sys.modules\n"
        "print('bundle-ok')\n",
        tmp_path,
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.strip() == "bundle-ok"
    assert apply(plan, tmp_path) == receipt
    manifest = Path(receipt["manifest"])
    assert OLD_SECRET not in manifest.read_text()
    if os.name != "nt":
        assert manifest.stat().st_mode & 0o777 == 0o600
        assert manifest.parent.stat().st_mode & 0o777 == 0o700
    assert deploy.rollback(manifest)["state"] == "rolled_back"
    assert path.read_bytes() == raw
    assert not (plugin / "__init__.py").exists()
    assert deploy.rollback(manifest)["state"] == "rolled_back"
    assert manifest.exists()  # Evidence and backups are retained.


@pytest.mark.parametrize("shape", ["flat", "extra", "implicit_env"])
def test_official_host_precedence_pinned_without_changing_auth_or_port(tmp_path, shape):
    path, config = hermes_config(tmp_path)
    api = config["platforms"]["api_server"]
    if shape in {"flat", "implicit_env"}:
        del api["extra"]["host"]
    if shape in {"extra", "implicit_env"}:
        del api["host"]
    path.write_text(yaml.safe_dump(config))
    plan = deploy.plan_hermes(
        path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path)
    )
    after = yaml.safe_load(plan.changes[-1].after)["platforms"]["api_server"]
    assert after.get("extra", {}).get("host", after.get("host")) == "127.0.0.1"
    assert after["port"] == 8642 and after["extra"]["key"] == OLD_SECRET


def test_gateway_whitelist_preserves_existing_auth_and_only_plans_mounts(tmp_path):
    path = tmp_path / "gateway.yaml"
    config = {
        "hermes": {
            "enabled": True,
            "base_url": "http://old.invalid:8642",
            "model": "keep",
            "api_key_env": "EXISTING_HERMES_KEY",
        },
        "database": {"url": OLD_SECRET},
        "artifact": {"storage_root": str(tmp_path / "artifacts")},
        "runtime": {"v2_routing_enabled": True},
    }
    path.write_text(yaml.safe_dump(config))
    raw = path.read_bytes()
    plan = deploy.plan_gateway(
        path,
        {
            "hermes.base_url": "https://hermes.invalid:9443",
            "hermes.ca_file": "/run/ca/hermes.pem",
            "artifact_return.enabled": True,
            "artifact_return.host_contract_confirmed": True,
            "artifact_return.public_base_url": "https://gateway.invalid:8443",
            "artifact_return.profile_reference": "existing-profile",
        },
    )
    assert OLD_SECRET not in json.dumps(plan.summary()) and OLD_SECRET not in repr(plan)
    references = [item for item in plan.requirements if item["kind"] == "environment_reference"]
    assert references[0]["only_services"] == ["gateway", "dispatch-worker"]
    receipt = apply(plan, tmp_path)
    updated = yaml.safe_load(path.read_bytes())
    assert updated["hermes"]["model"] == "keep"
    assert updated["hermes"]["api_key_env"] == "EXISTING_HERMES_KEY"
    assert updated["database"] == config["database"] and updated["runtime"] == config["runtime"]
    assert not (tmp_path / "runtime.env").exists()
    deploy.rollback(receipt["manifest"])
    assert path.read_bytes() == raw
    for name in ("hermes.model", "hermes.api_key_env", "legacy_runtime_confirmed", "runtime.any"):
        with pytest.raises(deploy.EnablementError, match="unmanaged_gateway_field"):
            deploy.plan_gateway(path, {name: "not-authorized"})


def test_bundle_conflict_and_unknown_files_are_never_overwritten(tmp_path):
    path, _ = hermes_config(tmp_path)
    plugin = tmp_path / "plugins/cf-artifact-return"
    plugin.mkdir(parents=True)
    unknown = plugin / "unrelated-note.txt"
    unknown.write_bytes(b"keep unknown")
    (plugin / "__init__.py").write_bytes(b"unknown old implementation")
    with pytest.raises(deploy.EnablementError, match="existing_bundle_asset_conflict"):
        deploy.plan_hermes(path, plugin, bridge_settings(tmp_path))
    assert unknown.read_bytes() == b"keep unknown"
    assert (plugin / "__init__.py").read_bytes() == b"unknown old implementation"


def test_changed_config_refuses_apply_and_changed_output_refuses_all_rollback(tmp_path):
    path, _ = hermes_config(tmp_path)
    plan = deploy.plan_hermes(
        path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path)
    )
    raw = path.read_bytes()
    path.write_bytes(raw + b"\n# concurrent change\n")
    with pytest.raises(deploy.EnablementError, match="asset_changed_since_plan"):
        apply(plan, tmp_path)
    assert not (tmp_path / "plugins").exists()
    path.write_bytes(raw)
    receipt = apply(plan, tmp_path)
    output = path.read_bytes()
    path.write_bytes(output + b"\n# newer operator change\n")
    module = tmp_path / "plugins/cf-artifact-return/__init__.py"
    module_bytes = module.read_bytes()
    with pytest.raises(deploy.EnablementError, match="rollback_conflicts"):
        deploy.rollback(receipt["manifest"])
    assert module.read_bytes() == module_bytes
    assert path.read_bytes() == output + b"\n# newer operator change\n"


def test_partial_apply_recovers_from_saved_originals_without_overwriting_unknown(
    tmp_path, monkeypatch
):
    path, _ = hermes_config(tmp_path)
    raw = path.read_bytes()
    plugin = tmp_path / "plugins/cf-artifact-return"
    plan = deploy.plan_hermes(path, plugin, bridge_settings(tmp_path))
    original = deploy._replace
    writes = 0

    def interrupted(target, content, **kwargs):
        nonlocal writes
        if plugin in target.parents:
            writes += 1
            if writes == 3:
                raise OSError("isolated crash")
        return original(target, content, **kwargs)

    monkeypatch.setattr(deploy, "_replace", interrupted)
    with pytest.raises(OSError, match="isolated crash"):
        apply(plan, tmp_path)
    assert path.read_bytes() == raw
    manifest = tmp_path / "private-journal" / plan.summary()["plan_sha256"] / "manifest.json"
    assert json.loads(manifest.read_text())["state"] == "prepared"
    monkeypatch.setattr(deploy, "_replace", original)
    deploy.rollback(manifest)
    assert path.read_bytes() == raw
    assert not (plugin / "__init__.py").exists()


def test_plan_shape_and_cli_errors_do_not_echo_inline_secrets(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("model: '" + OLD_SECRET + "\n")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "side": "gateway",
                "config_path": str(path),
                "delta": {"hermes.base_url": "https://hermes.invalid"},
            }
        )
    )
    assert deploy.main(["plan", "--request", str(request)]) == 2
    output = capsys.readouterr().out
    assert OLD_SECRET not in output and "configuration_parse_failed" in output
    path.write_text("model: first\nmodel: second\n")
    assert deploy.main(["plan", "--request", str(request)]) == 2
    assert "first" not in capsys.readouterr().out


def test_plan_hash_mismatch_and_existing_lock_fail_closed(tmp_path):
    path, _ = hermes_config(tmp_path)
    plan = deploy.plan_hermes(
        path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path)
    )
    with pytest.raises(deploy.EnablementError, match="reviewed_plan_changed"):
        deploy.apply_plan(plan, tmp_path / "state", expected_plan_sha256="0" * 64)
    state = tmp_path / "private-journal"
    state.mkdir()
    (state / ".operation.lock").write_text("unknown previous operation")
    with pytest.raises(deploy.EnablementError, match="enablement_operation_busy"):
        apply(plan, tmp_path)
    assert (state / ".operation.lock").read_text() == "unknown previous operation"


def test_rollback_rejects_corrupted_backup_without_changing_enabled_assets(tmp_path):
    path, _ = hermes_config(tmp_path)
    plan = deploy.plan_hermes(
        path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path)
    )
    receipt = apply(plan, tmp_path)
    enabled = path.read_bytes()
    manifest = Path(receipt["manifest"])
    backup = manifest.parent / f"{len(plan.changes) - 1}.before"
    backup.write_bytes(b"corrupt original")
    with pytest.raises(deploy.EnablementError, match="rollback_backup_integrity_failed"):
        deploy.rollback(manifest)
    assert path.read_bytes() == enabled


def test_hard_linked_configuration_is_rejected(tmp_path):
    path, _ = hermes_config(tmp_path)
    linked = tmp_path / "linked.yaml"
    os.link(path, linked)
    with pytest.raises(deploy.EnablementError, match="invalid_configuration_asset"):
        deploy.plan_hermes(path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path))


def test_gateway_cannot_reuse_model_secret_or_enable_plaintext_callback(tmp_path):
    path = tmp_path / "gateway.yaml"
    config = {
        "hermes": {"enabled": True, "base_url": "https://hermes.invalid", "api_key_env": "MODEL"},
        "artifact": {"storage_root": str(tmp_path / "artifacts")},
        "artifact_return": {
            "enabled": True,
            "host_contract_confirmed": True,
            "public_base_url": "https://gateway.invalid",
            "profile_reference": "existing",
        },
    }
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(deploy.EnablementError, match="invalid_gateway_enablement"):
        deploy.plan_gateway(path, {"artifact_return.signing_key_env": "MODEL"})
    with pytest.raises(deploy.EnablementError, match="invalid_gateway_enablement"):
        deploy.plan_gateway(path, {"artifact_return.public_base_url": "http://127.0.0.1:8080"})


def test_partial_apply_can_resume_same_plan_without_duplicate_installation(tmp_path, monkeypatch):
    path, _ = hermes_config(tmp_path)
    plugin = tmp_path / "plugins/cf-artifact-return"
    plan = deploy.plan_hermes(path, plugin, bridge_settings(tmp_path))
    original = deploy._replace

    def interrupted(target, content, **kwargs):
        if target == path:
            raise OSError("isolated last-step crash")
        return original(target, content, **kwargs)

    monkeypatch.setattr(deploy, "_replace", interrupted)
    with pytest.raises(OSError, match="last-step crash"):
        apply(plan, tmp_path)
    first_asset = plugin / "__init__.py"
    initial_inode = first_asset.stat().st_ino
    monkeypatch.setattr(deploy, "_replace", original)
    receipt = apply(plan, tmp_path)
    assert first_asset.stat().st_ino == initial_inode
    assert json.loads(Path(receipt["manifest"]).read_text())["state"] == "applied"


def test_modified_manifest_cannot_redirect_rollback(tmp_path):
    path, _ = hermes_config(tmp_path)
    plan = deploy.plan_hermes(
        path, tmp_path / "plugins/cf-artifact-return", bridge_settings(tmp_path)
    )
    receipt = apply(plan, tmp_path)
    manifest_path = Path(receipt["manifest"])
    manifest = json.loads(manifest_path.read_text())
    foreign = tmp_path / "foreign.txt"
    foreign.write_bytes(plan.changes[0].after)
    manifest["plan"]["changes"][0]["path"] = str(foreign)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(deploy.EnablementError, match="journal_integrity_failed"):
        deploy.rollback(manifest_path)
    assert foreign.read_bytes() == plan.changes[0].after


def _cli(tmp_path, arguments, *, crash_before=None):
    from test_artifact_return_imports import _cold_script

    script = "from cf_agent_gateway.hermes.return_bridge import enablement as deploy\n"
    if crash_before is not None:
        script += (
            "import os\n"
            "original = deploy._replace\n"
            "def interrupted(path, content, **kwargs):\n"
            f"    if str(path) == {str(crash_before)!r}: os._exit(86)\n"
            "    return original(path, content, **kwargs)\n"
            "deploy._replace = interrupted\n"
        )
    script += f"raise SystemExit(deploy.main({arguments!r}))\n"
    return _cold_script(script, tmp_path)


def _cli_request(tmp_path):
    path, _ = hermes_config(tmp_path)
    # Hold reviewed module bytes fixed while other tests/development may update
    # repository source. This is a test fixture, not another runtime installation.
    source = Path(deploy.__file__).resolve().parents[2]
    fixture = tmp_path / "reviewed-source"
    for name in deploy.BUNDLE_FILES:
        destination = fixture / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())
    request = tmp_path / "enable-request.json"
    request.write_text(
        json.dumps(
            {
                "side": "hermes",
                "config_path": str(path),
                "plugin_directory": str(tmp_path / "plugins/cf-artifact-return"),
                "settings": bridge_settings(tmp_path),
                "package_source": str(fixture),
            }
        ),
        encoding="utf-8",
    )
    planned = _cli(tmp_path, ["plan", "--request", str(request)])
    assert planned.returncode == 0, planned.stderr + planned.stdout
    summary = json.loads(planned.stdout)
    assert OLD_SECRET not in planned.stdout + planned.stderr
    arguments = [
        "apply",
        "--request",
        str(request),
        "--state-directory",
        str(tmp_path / "journal"),
        "--expected-plan-sha256",
        summary["plan_sha256"],
    ]
    return path, request, summary, arguments


def test_separate_cli_plan_apply_repeat_and_exact_rollback(tmp_path):
    path, _request, summary, arguments = _cli_request(tmp_path)
    original = path.read_bytes()
    first = _cli(tmp_path, arguments)
    assert first.returncode == 0, first.stderr + first.stdout
    receipt = json.loads(first.stdout)
    assert receipt["plan_sha256"] == summary["plan_sha256"]
    repeated = _cli(tmp_path, arguments)
    assert repeated.returncode == 0, repeated.stderr + repeated.stdout
    assert json.loads(repeated.stdout) == receipt
    assert OLD_SECRET not in first.stdout + repeated.stdout
    rolled = _cli(tmp_path, ["rollback", "--manifest", receipt["manifest"]])
    assert rolled.returncode == 0, rolled.stderr + rolled.stdout
    assert path.read_bytes() == original
    manifest = Path(receipt["manifest"])
    assert manifest.exists()
    assert (
        OLD_SECRET.encode()
        in (manifest.parent / f"{len(summary['changes']) - 1}.before").read_bytes()
    )
    assert OLD_SECRET not in manifest.read_text()


def test_hard_process_exit_resumes_same_cli_plan_then_refuses_later_edit(tmp_path):
    path, _request, summary, arguments = _cli_request(tmp_path)
    original = path.read_bytes()
    crashed = _cli(tmp_path, arguments, crash_before=path)
    assert crashed.returncode == 86, crashed.stderr + crashed.stdout
    assert path.read_bytes() == original
    state = tmp_path / "journal"
    assert (state / ".operation.lock").exists()  # kernel lock released, evidence retained
    manifest = state / summary["plan_sha256"] / "manifest.json"
    assert json.loads(manifest.read_text())["state"] == "prepared"
    installed = tmp_path / "plugins/cf-artifact-return/__init__.py"
    installed_inode = installed.stat().st_ino
    unknown = installed.parent / "unknown-note.txt"
    unknown.write_bytes(b"unrelated existing material")
    resumed = _cli(tmp_path, arguments)
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert json.loads(resumed.stdout)["plan_sha256"] == summary["plan_sha256"]
    assert installed.stat().st_ino == installed_inode
    assert unknown.read_bytes() == b"unrelated existing material"
    later = path.read_bytes() + b"\n# later operator change\n"
    path.write_bytes(later)
    rejected = _cli(tmp_path, ["rollback", "--manifest", str(manifest)])
    assert rejected.returncode == 2
    assert "rollback_conflicts_with_later_change" in rejected.stdout
    assert path.read_bytes() == later and installed.is_file()


def test_cli_resume_rejects_changed_request_and_corrupt_private_after_backup(tmp_path):
    path, request, summary, arguments = _cli_request(tmp_path)
    original_request = request.read_bytes()
    assert _cli(tmp_path, arguments, crash_before=path).returncode == 86
    before = path.read_bytes()
    modified = json.loads(original_request)
    modified["settings"]["tls_port"] = 9555
    request.write_text(json.dumps(modified), encoding="utf-8")
    changed = _cli(tmp_path, arguments)
    assert changed.returncode == 2 and "reviewed_request_changed" in changed.stdout
    assert path.read_bytes() == before
    request.write_bytes(original_request)
    after_backup = tmp_path / "journal" / summary["plan_sha256"] / "0.after"
    after_backup.write_bytes(b"unreviewed bytes")
    corrupted = _cli(tmp_path, arguments)
    assert corrupted.returncode == 2 and "resume_backup_integrity_failed" in corrupted.stdout
    assert path.read_bytes() == before
