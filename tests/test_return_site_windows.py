"""Windows deployment phases in private fixtures; official management is a test peer."""

from __future__ import annotations

import importlib.util
import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_hermes_tls import _test_certificates, _tls_peer

from cf_agent_gateway.hermes.return_bridge import enablement as en
from cf_agent_gateway.hermes.return_bridge import site_windows as site

OLD_SECRET = "isolated-existing-auth-key-do-not-publish"


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    config = {
        "model": {"provider": "unchanged", "api_key": OLD_SECRET},
        "platforms": {"api_server": {"enabled": True}},
        "plugins": {"enabled": ["existing-plugin"]},
        "platform_toolsets": {"cli": ["file"]},
        "skills": {"unchanged": True},
    }
    path = home / "config.yaml"
    path.write_text("# original comment\n" + yaml.safe_dump(config), encoding="utf-8")
    (home / ".env").write_text(
        f"API_SERVER_KEY={OLD_SECRET}\nAPI_SERVER_HOST=192.0.2.5\nAPI_SERVER_PORT=8642\n",
        encoding="utf-8",
    )
    ca, cert, key = _test_certificates(tmp_path)
    request = {
        "deployment_id": "fixture-deploy",
        "release_commit": "a" * 40,
        "hermes_home": str(home),
        "legacy_origin": "http://192.0.2.5:8642",
        "hermes_origin": "https://localhost:18642",
        "gateway_origin": "https://192.0.2.6:18444",
        "gateway_ca_file": str(ca),
        "hermes_ca_file": str(ca),
        "tls_cert_file": str(cert),
        "tls_key_file": str(key),
    }
    proof = {"commit": en.OFFICIAL_COMMIT, "files": {"fixture": "a" * 64}}
    tools = {"enabled": ["file", "vision", "cf_filebridge"], "rows_sha256": "b" * 64}

    def process(pid):
        return {
            "pid": pid,
            "created": datetime.now(UTC).isoformat(),
            "executable": str(home / "tools/python.exe"),
        }

    runtime = {"processes": [process(42)], "listeners": []}
    actions, http = [], []
    monkeypatch.setattr(site, "source_proof", lambda _: proof)
    monkeypatch.setattr(
        site, "release_proof", lambda _: {"commit": "a" * 40, "files": {"fixture": "c" * 64}}
    )
    monkeypatch.setattr(
        site,
        "installation_proof",
        lambda _: {"management_sha256": "d" * 64, "facts_sha256": "e" * 64},
    )
    monkeypatch.setattr(site, "executor", lambda _: str(home / "tools/python.exe"))
    monkeypatch.setattr(site, "toolset_snapshot", lambda *_: tools)
    monkeypatch.setattr(site, "process_snapshot", lambda _: runtime)

    def management(_, action):
        actions.append(action)
        runtime["processes"] = [] if action == "stop" else [process(43)]
        runtime["listeners"] = (
            [{"host": "127.0.0.1", "port": 8642, "pid": 43}] if action == "start" else []
        )

    def get_json(origin, endpoint, key, ca_file=None):
        assert key == OLD_SECRET
        http.append((origin, endpoint, ca_file))
        return {"object": "list", "data": []}

    monkeypatch.setattr(site, "_management", management)
    monkeypatch.setattr(site, "_get_json", get_json)
    monkeypatch.setattr(
        site,
        "_gateway_tls",
        lambda _: {"certificate_and_hostname_verified": True, "fixture_only": True},
    )
    monkeypatch.setattr(site, "OBSERVATION_SECONDS", 0)
    return request, path, config, runtime, actions, http


def peer(request, state):
    return {
        "schema": site.PEER_SCHEMA,
        "role": "server",
        "deployment_id": request["deployment_id"],
        "release_commit": request["release_commit"],
        "state": state,
        "activation_allowed": False,
        "plan_sha256": "f" * 64,
        "public_references": site._public_references(request),
    }


def stop(fixture):
    request = fixture[0]
    summary, _ = site.plan(request)
    site.execute(
        request,
        "stop",
        authorized=True,
        expected_plan_sha256=summary["plan_sha256"],
        peer=peer(request, "quiesced"),
    )
    return summary


def test_missing_certificate_plan_is_partial_and_readonly(fixture):
    request, path, _, _, actions, _ = fixture
    original = path.read_bytes()
    del request["gateway_ca_file"]
    del request["tls_cert_file"]
    summary, material = site.plan(request)
    assert material is None and summary["pending"] == ["gateway_ca_file", "tls_cert_file"]
    assert "enablement" not in summary and summary["activation_allowed"] is False
    assert path.read_bytes() == original and actions == []
    assert not (path.parent / "cf-artifact-return-site").exists()
    with pytest.raises(site.SiteError, match="tls_material_pending"):
        site.execute(
            request,
            "stop",
            authorized=True,
            expected_plan_sha256=summary["plan_sha256"],
            peer=peer(request, "quiesced"),
        )
    assert actions == []


def test_plan_preserves_effective_tools_existing_model_and_does_not_stop(fixture):
    request, path, config, _, actions, _ = fixture
    original = path.read_bytes()
    summary, material = site.plan(request)
    changed = yaml.safe_load(material.changes[-1].after)
    assert changed["platform_toolsets"]["api_server"] == [
        "file",
        "vision",
        "cf_filebridge",
        "cf_artifact_return",
    ]
    assert changed["model"] == config["model"] and changed["skills"] == config["skills"]
    assert changed["plugins"]["enabled"] == ["existing-plugin", "cf-artifact-return"]
    assert changed["platforms"]["api_server"]["host"] == "127.0.0.1"
    assert path.read_bytes() == original and actions == []
    assert OLD_SECRET not in json.dumps(summary)


@pytest.mark.parametrize(
    "phase", ["prepare-csr", "stop", "apply", "start", "quiesce", "rollback", "start-legacy"]
)
def test_every_mutation_requires_explicit_authorization(fixture, phase):
    request, _, _, _, actions, _ = fixture
    with pytest.raises(site.SiteError, match="explicit_maintenance_authorization"):
        site.execute(request, phase)
    assert actions == []


def test_wrong_peer_or_changed_tool_snapshot_refuses_stop(fixture, monkeypatch):
    request, _, _, _, actions, _ = fixture
    summary, _ = site.plan(request)
    bad = {**peer(request, "quiesced"), "deployment_id": "other"}
    with pytest.raises(site.SiteError, match="server_phase_receipt"):
        site.execute(
            request, "stop", authorized=True, expected_plan_sha256=summary["plan_sha256"], peer=bad
        )
    monkeypatch.setattr(
        site, "toolset_snapshot", lambda *_: {"enabled": ["all"], "rows_sha256": "c" * 64}
    )
    with pytest.raises(site.SiteError, match="reviewed_plan_changed"):
        site.execute(
            request,
            "stop",
            authorized=True,
            expected_plan_sha256=summary["plan_sha256"],
            peer=peer(request, "quiesced"),
        )
    assert actions == []


def test_full_phase_and_exact_rollback_without_models(fixture):
    request, path, _, runtime, actions, http = fixture
    original = path.read_bytes()
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    applied = site.execute(request, "apply", peer=peer(request, "applied"), **options)
    assert applied["state"] == "applied" and runtime["processes"] == []
    ready = site.execute(request, "start", peer=peer(request, "applied"), **options)
    assert ready["state"] == "tls_ready" and ready["activation_allowed"] is False
    assert http == [(request["hermes_origin"], "/v1/models", request["hermes_ca_file"])]
    site.execute(request, "quiesce", peer=peer(request, "quiesced"), **options)
    rolled = site.execute(request, "rollback", peer=peer(request, "rolled_back"), **options)
    assert rolled["state"] == "rolled_back" and path.read_bytes() == original
    site.execute(request, "start-legacy", peer=peer(request, "rolled_back"), **options)
    assert actions == ["stop", "start", "stop", "start"]
    assert [entry[1] for entry in http] == ["/v1/models", "/v1/models"]
    assert OLD_SECRET not in json.dumps(ready)


def test_apply_refuses_restarted_process_and_start_refuses_changed_config(fixture):
    request, path, _, runtime, actions, _ = fixture
    summary = stop(fixture)
    options = {
        "authorized": True,
        "expected_plan_sha256": summary["plan_sha256"],
        "peer": peer(request, "applied"),
    }
    runtime["processes"] = [{"pid": 99}]
    with pytest.raises(site.SiteError, match="must_remain_stopped"):
        site.execute(request, "apply", **options)
    runtime["processes"] = []
    site.execute(request, "apply", **options)
    path.write_bytes(path.read_bytes() + b"\n# external edit")
    with pytest.raises(site.SiteError, match="applied_asset_changed"):
        site.execute(request, "start", **options)
    assert actions == ["stop"]


def test_partial_apply_resumes_only_journal_owned_changes(fixture, monkeypatch):
    request = fixture[0]
    summary = stop(fixture)
    options = {
        "authorized": True,
        "expected_plan_sha256": summary["plan_sha256"],
        "peer": peer(request, "applied"),
    }
    real_replace = en._replace
    counter = 0

    def interrupt(path, content, **kwargs):
        nonlocal counter
        if "plugins" in path.parts:
            counter += 1
            if counter == 2:
                raise OSError("isolated crash")
        return real_replace(path, content, **kwargs)

    monkeypatch.setattr(en, "_replace", interrupt)
    with pytest.raises(OSError, match="isolated crash"):
        site.execute(request, "apply", **options)
    monkeypatch.setattr(en, "_replace", real_replace)
    assert site.execute(request, "apply", **options)["state"] == "applied"


def test_csr_stage_creates_only_private_fixture_and_preserves_existing(fixture):
    from cryptography import x509

    request = fixture[0]
    csr = site.execute(request, "csr-plan")
    assert not Path(csr["directory"]).exists()
    result = site.execute(request, "prepare-csr", authorized=True)
    parsed = x509.load_pem_x509_csr(Path(result["csr"]).read_bytes())
    assert parsed.is_signature_valid
    assert parsed.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName) == ["localhost"]
    assert "PRIVATE KEY" not in json.dumps(result)
    with pytest.raises(site.SiteError, match="existing_certificate_stage_preserved"):
        site.execute(request, "prepare-csr", authorized=True)


def test_wrong_certificate_hostname_and_reused_port_fail_closed(fixture):
    request = fixture[0]
    with pytest.raises(site.SiteError, match="hostname_invalid"):
        site.plan({**request, "hermes_origin": "https://192.0.2.7:18642"})
    with pytest.raises(site.SiteError, match="must_not_reuse"):
        site.plan({**request, "hermes_origin": "https://localhost:8642"})


def test_observed_uv_generation_version_info_and_base_python(tmp_path):
    home = tmp_path / "home"
    env = home / "installs/fixed/environments/selected/venv"
    env.mkdir(parents=True)
    runtime = home / "tools/python-3.14.7"
    runtime.mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"test fixture, never executed")
    (env / "pyvenv.cfg").write_text(f"home = {runtime}\nversion_info = 3.14.7\n", encoding="utf-8")
    facts = home / "installs/fixed/facts.json"
    facts.write_text(
        json.dumps({"packages": {"venv": {"environment": str(env)}}}), encoding="utf-8"
    )
    assert site.executor({"hermes_home": str(home), "facts": str(facts)}) == str(
        runtime / "python.exe"
    )


def test_release_proof_rejects_dirty_or_wrong_release(monkeypatch):
    head = "a" * 40
    commands = []

    def run(command):
        commands.append(command)
        if "rev-parse" in command:
            return head.encode()
        if "status" in command:
            return b" M src/modified.py"
        raise AssertionError("must fail before reading committed assets")

    monkeypatch.setattr(site, "_run", run)
    with pytest.raises(site.SiteError, match="release_head_changed"):
        site.release_proof({"release_commit": "b" * 40})
    with pytest.raises(site.SiteError, match="tracked_changes"):
        site.release_proof({"release_commit": head})
    assert all("show" not in command for command in commands)


def test_start_rechecks_management_binary_and_facts(fixture, monkeypatch):
    request = fixture[0]
    summary = stop(fixture)
    options = {
        "authorized": True,
        "expected_plan_sha256": summary["plan_sha256"],
        "peer": peer(request, "applied"),
    }
    site.execute(request, "apply", **options)
    monkeypatch.setattr(site, "installation_proof", lambda _: {"management_sha256": "changed"})
    with pytest.raises(site.SiteError, match="official_installation_changed"):
        site.execute(request, "start", **options)
    assert fixture[4] == ["stop"]


def test_init_request_inherits_only_endpoint_refs_and_leaves_tls_pending(fixture, monkeypatch):
    request = fixture[0]
    monkeypatch.setattr(site, "_run", lambda *_: ("a" * 40).encode())
    result = site.initial_request("shared-id", hermes_home=request["hermes_home"])
    assert result["legacy_origin"] == "http://192.0.2.5:8642"
    assert result["hermes_origin"] is None and result["gateway_ca_file"] is None
    assert result["tls_key_file"] is None and OLD_SECRET not in json.dumps(result)
    assert "model" not in result and "token" not in result


def test_live_toolsets_snapshot_does_not_load_plugin_or_expand_mcp(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        f"API_SERVER_KEY={OLD_SECRET}\nAPI_SERVER_HOST=192.0.2.5\nAPI_SERVER_PORT=8642\n"
    )
    request = {"hermes_home": str(home), "legacy_origin": "http://192.0.2.5:8642"}
    calls = []

    def read(origin, endpoint, key):
        calls.append((origin, endpoint))
        assert key == OLD_SECRET
        return {
            "platform": "api_server",
            "data": [
                {"name": "file", "enabled": True, "tools": ["read_file"]},
                {"name": "disabled", "enabled": False, "tools": []},
            ],
        }

    monkeypatch.setattr(site, "_get_json", read)
    result = site.toolset_snapshot(request, {"platforms": {"api_server": {"enabled": True}}})
    assert result["enabled"] == ["file"]
    assert calls == [(request["legacy_origin"], "/v1/toolsets")]
    with pytest.raises(site.SiteError, match="mcp_toolset_preservation"):
        site.toolset_snapshot(request, {"mcp_servers": {"keep": {}}})
    assert len(calls) == 1


def test_partial_apply_can_rollback_without_applied_receipt(fixture, monkeypatch):
    request, path, *_ = fixture
    original = path.read_bytes()
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    real_replace = en._replace
    writes = 0

    def crash(path, content, **kwargs):
        nonlocal writes
        if "plugins" in path.parts:
            writes += 1
            if writes == 2:
                raise OSError("fixture interrupted apply")
        return real_replace(path, content, **kwargs)

    monkeypatch.setattr(en, "_replace", crash)
    with pytest.raises(OSError, match="interrupted apply"):
        site.execute(request, "apply", peer=peer(request, "applied"), **options)
    monkeypatch.setattr(en, "_replace", real_replace)
    normalized = site.request_defaults(request)
    assert site._load_optional(normalized, "applied.json") is None
    rolled = site.execute(request, "rollback", peer=peer(request, "quiesced"), **options)
    assert rolled["state"] == "rolled_back" and path.read_bytes() == original
    assert en._manifest_read(site._journal_path(normalized, summary))["state"] == "rolled_back"


def test_stop_receipt_crash_recovers_without_second_stop_or_old_http(fixture, monkeypatch):
    request, _, _, runtime, actions, _ = fixture
    summary, _ = site.plan(request)
    options = {
        "authorized": True,
        "expected_plan_sha256": summary["plan_sha256"],
        "peer": peer(request, "quiesced"),
    }
    real_save = site._save

    def crash(request, name, value):
        if name == "quiesced.json":
            raise OSError("fixture lost quiesced receipt")
        return real_save(request, name, value)

    monkeypatch.setattr(site, "_save", crash)
    with pytest.raises(OSError, match="lost quiesced"):
        site.execute(request, "stop", **options)
    assert runtime["processes"] == [] and actions == ["stop"]
    monkeypatch.setattr(site, "_save", real_save)
    monkeypatch.setattr(
        site, "toolset_snapshot", lambda *_: pytest.fail("must not query stopped HTTP")
    )
    assert site.execute(request, "stop", **options)["state"] == "quiesced"
    assert actions == ["stop"]


def test_uncertain_stop_never_resubmits_and_recovers_from_observation(fixture, monkeypatch):
    request, _, _, runtime, actions, _ = fixture
    summary, _ = site.plan(request)
    options = {
        "authorized": True,
        "expected_plan_sha256": summary["plan_sha256"],
        "peer": peer(request, "quiesced"),
    }

    def uncertain(_, action):
        actions.append(action)
        raise site.SiteError("official_management_command_failed")

    monkeypatch.setattr(site, "_management", uncertain)
    for _ in range(2):
        with pytest.raises(site.SiteError, match="uncertain_observation_only"):
            site.execute(request, "stop", **options)
    assert actions == ["stop"]
    runtime["processes"] = []
    assert site.execute(request, "stop", **options)["state"] == "quiesced"
    assert actions == ["stop"]


@pytest.mark.parametrize("legacy", [False, True])
def test_started_process_awaits_readiness_without_launching_again(fixture, monkeypatch, legacy):
    request, _, _, runtime, actions, _ = fixture
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    site.execute(request, "apply", peer=peer(request, "applied"), **options)
    if legacy:
        site.execute(request, "rollback", peer=peer(request, "quiesced"), **options)
    phase = "start-legacy" if legacy else "start"
    server = peer(request, "rolled_back" if legacy else "applied")
    real_get = site._get_json

    def unavailable(*_):
        raise OSError("fixture process started before listener is ready")

    monkeypatch.setattr(site, "_get_json", unavailable)
    with pytest.raises(site.SiteError, match="uncertain_observation_only"):
        site.execute(request, phase, peer=server, **options)
    first_process = dict(runtime["processes"][0])
    assert actions == ["stop", "start"]
    monkeypatch.setattr(site, "_get_json", real_get)
    result = site.execute(request, phase, peer=server, **options)
    assert result["state"] == ("rolled_back" if legacy else "tls_ready")
    assert result["process"] == first_process and actions == ["stop", "start"]
    runtime["processes"][0] = {**first_process, "pid": 99}
    with pytest.raises(site.SiteError, match="process_identity_changed"):
        site.execute(request, phase, peer=server, **options)
    assert actions == ["stop", "start"]


def test_tls_ready_requires_gateway_handshake_then_only_observes_retry(fixture, monkeypatch):
    request = fixture[0]
    summary = stop(fixture)
    options = {
        "authorized": True,
        "expected_plan_sha256": summary["plan_sha256"],
        "peer": peer(request, "applied"),
    }
    site.execute(request, "apply", **options)

    def rejected(_):
        raise ssl.SSLCertVerificationError("fixture untrusted Gateway certificate")

    monkeypatch.setattr(site, "_gateway_tls", rejected)
    with pytest.raises(site.SiteError, match="uncertain_observation_only"):
        site.execute(request, "start", **options)
    assert site._load_optional(site.request_defaults(request), "tls-ready.json") is None
    monkeypatch.setattr(site, "_gateway_tls", lambda _: {"certificate_and_hostname_verified": True})
    ready = site.execute(request, "start", **options)
    assert ready["gateway_tls"]["certificate_and_hostname_verified"] is True
    assert fixture[4] == ["stop", "start"]


def test_saved_plan_and_server_plan_and_public_refs_are_bound(fixture):
    request = fixture[0]
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    changed_peer = {**peer(request, "applied"), "plan_sha256": "0" * 64}
    with pytest.raises(site.SiteError, match="server_plan_changed"):
        site.execute(request, "apply", peer=changed_peer, **options)
    for field in ("gateway_origin", "hermes_ca_sha256", "profile_reference"):
        bad_peer = peer(request, "applied")
        bad_peer["public_references"][field] = "other"
        with pytest.raises(site.SiteError, match="server_phase_receipt"):
            site.execute(request, "apply", peer=bad_peer, **options)
    site._save(
        site.request_defaults(request), "reviewed-plan.json", {**summary, "config_sha256": "x"}
    )
    with pytest.raises(site.SiteError, match="reviewed_plan_changed"):
        site.execute(request, "apply", peer=peer(request, "applied"), **options)
    assert fixture[4] == ["stop"]


def test_windows_gateway_tls_real_handshake_ca_and_hostname(tmp_path, monkeypatch):
    certificates = _test_certificates(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    wrong_ca, _, _ = _test_certificates(other)
    monkeypatch.setenv("SSL_CERT_FILE", "not-a-trust-reference")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with _tls_peer(certificates) as (origin, http_calls):
        request = {"gateway_origin": origin, "gateway_ca_file": str(certificates[0])}
        proof = site._gateway_tls(request)
        assert proof["certificate_and_hostname_verified"] is True
        assert proof["ca_sha256"] == site._sha(certificates[0].read_bytes())
        with pytest.raises(ssl.SSLCertVerificationError):
            site._gateway_tls({**request, "gateway_ca_file": str(wrong_ca)})
        with pytest.raises(ssl.SSLCertVerificationError):
            site._gateway_tls(
                {**request, "gateway_origin": origin.replace("localhost", "127.0.0.1")}
            )
        assert http_calls == []  # TLS-only: no model, authenticated request, or HTTP fallback.


def test_initial_public_handoff_precedes_either_side_stopping(fixture):
    request, path, _, runtime, actions, _ = fixture
    original = path.read_bytes()
    result = site.execute(request, "export-public")
    assert result["schema"] == site.PEER_SCHEMA and result["state"] == "prepared"
    assert result["role"] == "hermes" and result["activation_allowed"] is False
    assert result["public_references"] == site._public_references(request)
    assert len(result["plan_sha256"]) == 64
    assert path.read_bytes() == original and len(runtime["processes"]) == 1 and actions == []
    assert not (path.parent / "cf-artifact-return-site").exists()


def test_quiesce_does_not_stop_an_unrelated_replacement_process(fixture):
    request, _, _, runtime, actions, _ = fixture
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    site.execute(request, "apply", peer=peer(request, "applied"), **options)
    site.execute(request, "start", peer=peer(request, "applied"), **options)
    runtime["processes"][0] = {**runtime["processes"][0], "pid": 99}
    with pytest.raises(site.SiteError, match="process_identity_changed"):
        site.execute(request, "quiesce", peer=peer(request, "quiesced"), **options)
    assert actions == ["stop", "start"]


def test_quiesce_accepts_previously_bound_process_exit_without_another_stop(fixture):
    request, _, _, runtime, actions, _ = fixture
    summary = stop(fixture)
    options = {"authorized": True, "expected_plan_sha256": summary["plan_sha256"]}
    site.execute(request, "apply", peer=peer(request, "applied"), **options)
    site.execute(request, "start", peer=peer(request, "applied"), **options)
    runtime["processes"] = []
    runtime["listeners"] = []
    assert (
        site.execute(request, "quiesce", peer=peer(request, "quiesced"), **options)["state"]
        == "quiesced"
    )
    assert actions == ["stop", "start"]


def test_real_windows_server_public_and_phase_receipts_interoperate(fixture, tmp_path, monkeypatch):
    """Both actual producers/consumers; only process/Docker/HTTP peers are synthetic."""
    module_path = Path(__file__).parents[1] / "deploy/prepare-return-server.py"
    spec = importlib.util.spec_from_file_location("return_server_interop", module_path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    request, original_config, _, _, actions, _ = fixture
    original = original_config.read_bytes()
    prepared = site.execute(request, "export-public")
    server_root = tmp_path / "server-fixture"
    server_root.mkdir(mode=0o700)
    roots = {name: server_root / name for name in ("gateway", "nginx", "release", "state")}
    for path in roots.values():
        path.mkdir(mode=0o700)
    incoming = server_root / "hermes-peer.json"
    server.save_json(incoming, prepared)
    # These are separate host-local paths, containing identical public CA bytes.
    server_ca = server_root / "approved-public-ca.pem"
    server_ca.write_bytes(Path(request["hermes_ca_file"]).read_bytes())
    server_ca.chmod(0o600)
    baseline_image, candidate_image, nginx_image = (
        "sha256:" + char * 64 for char in ("b", "c", "d")
    )
    containers = {
        name: {"id": str(i + 1) * 64, "image": baseline_image, "running": True}
        for i, name in enumerate(server.APPS)
    }
    monkeypatch.setattr(server.ServerEntry, "containers", lambda _: containers)

    def compose(_, args, **kwargs):
        assert kwargs.get("nginx") is True
        if args[0] == "config":
            return json.dumps({"services": {"https": {"image": nginx_image}}})
        assert args == ["ps", "--all", "--quiet", "https"]
        return "9" * 64

    def inspect(_, identifier):
        if identifier == "9" * 64:
            return {"Image": nginx_image}
        assert identifier == containers["gateway"]["id"]
        return {
            "NetworkSettings": {
                "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18999"}]}
            }
        }

    monkeypatch.setattr(server.ServerEntry, "compose", compose)
    monkeypatch.setattr(server.ServerEntry, "inspect", inspect)
    server_request = server_root / "request.json"
    options = SimpleNamespace(
        request=str(server_request),
        release_commit=request["release_commit"],
        candidate_image=candidate_image,
        release_root=str(roots["release"]),
        gateway_root=str(roots["gateway"]),
        nginx_root=str(roots["nginx"]),
        deployment_id=request["deployment_id"],
        peer_public=str(incoming),
        state_directory=str(roots["state"]),
        hermes_ca_file=str(server_ca),
        gateway_ca_file=str(server_ca),
    )
    assert server.init_request(options)["missing_public_references"] == []
    entry = server.ServerEntry(
        server_request, release_commit=request["release_commit"], candidate_image=candidate_image
    )
    assert entry.public_references() == prepared["public_references"]
    assert entry.request["hermes_ca_file"] != request["hermes_ca_file"]
    entry.persist("quiesced", request_sha256=entry.request_hash, plan_sha256="e" * 64)
    win_options = {
        "authorized": True,
        "expected_plan_sha256": prepared["plan_sha256"],
    }
    win_stopped = site.execute(request, "stop", peer=entry.receipt("quiesced"), **win_options)
    server.save_json(incoming, win_stopped)
    assert entry.peer("quiesced") == win_stopped
    assert entry.state["hermes_plan_sha256"] == prepared["plan_sha256"]
    entry.persist("applied")
    server_applied = entry.receipt("applied")
    site.execute(request, "apply", peer=server_applied, **win_options)
    win_ready = site.execute(request, "start", peer=server_applied, **win_options)
    server.save_json(incoming, win_ready)
    assert entry.peer("tls_ready") == win_ready
    assert win_ready["gateway_tls"]["certificate_and_hostname_verified"] is True
    entry.persist("quiesced")
    server_quiesced = entry.receipt("quiesced")
    site.execute(request, "quiesce", peer=server_quiesced, **win_options)
    win_rolled = site.execute(request, "rollback", peer=server_quiesced, **win_options)
    entry.persist("rolled_back")
    server.save_json(incoming, win_rolled)
    with pytest.raises(server.EntryError, match="legacy_hermes_not_started"):
        entry.peer("rolled_back")
    legacy = site.execute(request, "start-legacy", peer=entry.receipt("rolled_back"), **win_options)
    server.save_json(incoming, legacy)
    assert entry.peer("rolled_back") == legacy and legacy["legacy_process_started"] is True
    assert original_config.read_bytes() == original
    assert actions == ["stop", "start", "stop", "start"]
    assert OLD_SECRET not in json.dumps((prepared, server_applied, win_ready, legacy))
