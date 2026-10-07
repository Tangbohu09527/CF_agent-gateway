"""Host orchestration with a fake Docker receiver; no production/service/model calls."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module():
    path = Path(__file__).parents[1] / "deploy/prepare-return-server.py"
    spec = importlib.util.spec_from_file_location("return_server_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def protected(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


@pytest.fixture
def site_request(tmp_path):
    root, nginx, release, state = [
        tmp_path / name for name in ("gateway", "nginx", "release", "state")
    ]
    for path in (root, nginx, release, state):
        path.mkdir(mode=0o700)
    protected(root / "config/production.yaml", b"private: preserve-original\n")
    protected(nginx / "nginx.conf", b"http { }\n")
    value = {
        "schema": "cf-return-site/v1",
        "release_commit": "a" * 40,
        "candidate_image": "sha256:" + "b" * 64,
        "baseline_image": "sha256:" + "c" * 64,
        "gateway_root": str(root),
        "nginx_root": str(nginx),
        "hermes_origin": "https://hermes.example.invalid:8643",
        "hermes_ca_file": str(protected(tmp_path / "hermes-ca.pem", b"test certificate")),
        "gateway_origin": "https://gateway.example.invalid:18444",
        "gateway_ca_file": str(protected(tmp_path / "gateway-ca.pem", b"test certificate")),
        "profile_reference": "approved-reference",
        "profile_revision": 1,
        "signing_env_file": str(root / "secrets/artifact-return.env"),
        "release_root": str(release),
        "nginx_image": "sha256:" + "d" * 64,
        "state_directory": str(state),
        "deployment_id": "isolated-return-test",
        "gateway_health_url": "http://127.0.0.1:18999",
        "peer_state_file": str(tmp_path / "hermes-peer.json"),
    }
    path = protected(tmp_path / "request.json", json.dumps(value).encode())
    return value, path


@pytest.fixture
def entry(module, site_request, monkeypatch):
    value, path = site_request
    entry = module.ServerEntry(
        path, release_commit=value["release_commit"], candidate_image=value["candidate_image"]
    )
    monkeypatch.setattr(entry, "preflight", lambda: None)
    return entry


def health():
    return {
        "components": {name: {"status": "ok"} for name in ("database", "migration_schema")},
        "dispatch": dict.fromkeys(
            (
                "queued",
                "running",
                "failed",
                "uncertain",
                "dead",
                "stale_running",
                "blocked_threads",
                "missing_delivery",
                "reconciliation_backlog",
                "reconciliation_deferred",
                "reconciliation_poison",
            ),
            0,
        ),
        "delivery": dict.fromkeys(
            ("queued", "delivering", "failed", "uncertain", "stale_delivering", "missing_delivery"),
            0,
        ),
    }


def initialize(entry, phase="planned"):
    entry.persist(
        phase,
        request_sha256=entry.request_hash,
        plan_sha256="e" * 64,
        queues_quiesced=entry.validate_health(health()),
    )


def peer(entry, phase):
    value = {
        "schema": "cf-artifact-return/site-peer/v1",
        "role": "hermes",
        "deployment_id": entry.request["deployment_id"],
        "release_commit": entry.request["release_commit"],
        "state": phase,
        "plan_sha256": "f" * 64,
        "public_references": entry.public_references(),
        "legacy_process_started": phase == "rolled_back",
        "activation_allowed": False,
    }
    protected(Path(entry.request["peer_state_file"]), json.dumps(value).encode())


def rows(entry, *, running=True, image=None):
    return {
        name: {
            "id": str(i + 1) * 64,
            "image": image or entry.request["baseline_image"],
            "running": running,
        }
        for i, name in enumerate(entry.__class__.__init__.__globals__["APPS"])
    }


def test_asset_process_has_no_network_socket_or_writable_roots_until_apply(entry, monkeypatch):
    calls = []
    monkeypatch.setattr(entry, "docker", lambda args, **kw: calls.append(args) or "{}")
    entry.assets("plan")
    command = calls[-1]
    assert command[:6] == ["run", "--rm", "--network", "none", "--read-only", "--user"]
    assert "--cap-drop" in command and "ALL" in command and "no-new-privileges" in command
    assert not any("docker.sock" in str(v) for v in command)
    for path in (entry.root, entry.nginx):
        assert f"type=bind,src={path},dst={path},readonly" in command
    assert f"type=bind,src={entry.state_dir},dst={entry.state_dir}" in command
    entry.assets("apply", writable=True, plan_sha="e" * 64)
    assert f"type=bind,src={entry.root},dst={entry.root}" in calls[-1]
    assert "--expected-plan-sha256" in calls[-1]


def test_plan_refuses_mixed_baseline_before_asset_changes(entry, monkeypatch):
    original = rows(entry)
    original["dispatch-worker"]["image"] = entry.request["candidate_image"]
    monkeypatch.setattr(entry, "containers", lambda: original)
    monkeypatch.setattr(entry, "assets", lambda *a, **k: pytest.fail("must not reach asset plan"))
    with pytest.raises(RuntimeError, match="baseline_four_images_mismatch"):
        entry.plan()
    assert not Path(entry.request["signing_env_file"]).exists()


@pytest.mark.parametrize("component", ["database", "migration_schema"])
def test_zero_queue_fallback_is_not_drain_evidence(module, component):
    value = health()
    value["components"][component]["status"] = "unknown"
    with pytest.raises(RuntimeError, match="database_or_schema_unhealthy"):
        module.ServerEntry.validate_health(value)


def test_quiesce_waits_dispatch_then_database_then_api_and_never_force_kills(entry, monkeypatch):
    initialize(entry)
    current = rows(entry)
    effects = []
    monkeypatch.setattr(entry, "containers", lambda: current)
    monkeypatch.setattr(
        entry, "healthy_queues", lambda: effects.append("health") or entry.validate_health(health())
    )

    def controller(action):
        effects.append("gate-stop")
        for name in ("worker", "delivery-worker"):
            current[name]["running"] = False

    def stop(identifier, *, deadline):
        name = next(name for name, row in current.items() if row["id"] == identifier)
        effects.append("term:" + name)
        current[name]["running"] = False

    monkeypatch.setattr(entry, "controller", controller)
    monkeypatch.setattr(entry, "term_wait", stop)
    entry.quiesce()
    assert effects == ["health", "gate-stop", "term:dispatch-worker", "health", "term:gateway"]
    assert entry.state["phase"] == "quiesced"
    assert all(not row["running"] for row in current.values())


def test_graceful_deadline_never_turns_into_docker_kill(entry, monkeypatch):
    calls = []
    monkeypatch.setattr(entry, "inspect", lambda _: {"State": {"Running": True}})
    monkeypatch.setattr(entry, "docker", lambda args: calls.append(args))
    with pytest.raises(RuntimeError, match="graceful_shutdown_pending_no_force_kill"):
        entry.term_wait("1" * 64, deadline=0)
    assert calls == [["kill", "--signal", "SIGTERM", "1" * 64]]


def test_shutdown_new_uncertain_blocks_apply_and_preserves_gate(entry, monkeypatch):
    initialize(entry)
    current = rows(entry)
    before, after = entry.validate_health(health()), entry.validate_health(health())
    after["dispatch"]["uncertain"] = 1
    snapshots = iter((before, after))
    monkeypatch.setattr(entry, "containers", lambda: current)
    monkeypatch.setattr(entry, "healthy_queues", lambda: next(snapshots))

    def controller(_):
        for name in ("worker", "delivery-worker"):
            current[name]["running"] = False

    monkeypatch.setattr(entry, "controller", controller)
    monkeypatch.setattr(
        entry, "term_wait", lambda *a, **k: current["dispatch-worker"].update(running=False)
    )
    with pytest.raises(RuntimeError, match="new_uncertain_requires_review"):
        entry.quiesce()
    assert entry.state["phase"] == "quiescing"
    assert current["gateway"]["running"]
    assert not current["worker"]["running"] and not current["delivery-worker"]["running"]


def test_apply_requires_real_stopped_containers_and_matching_peer(entry, monkeypatch):
    initialize(entry, "quiesced")
    monkeypatch.setattr(entry, "containers", lambda: rows(entry))
    with pytest.raises(RuntimeError, match="apps_not_quiesced"):
        entry.apply()
    monkeypatch.setattr(entry, "containers", lambda: rows(entry, running=False))
    peer(entry, "applied")
    with pytest.raises(RuntimeError, match="peer_not_in_required_state"):
        entry.apply()
    assert not Path(entry.request["signing_env_file"]).exists()


def test_apply_secret_is_once_private_scoped_and_not_in_output(entry, monkeypatch):
    initialize(entry, "quiesced")
    peer(entry, "quiesced")
    monkeypatch.setattr(entry, "containers", lambda: rows(entry, running=False))
    calls = []
    monkeypatch.setattr(entry, "assets", lambda *a, **k: calls.append((a, k)) or {})
    monkeypatch.setattr(entry, "check_rendered", lambda _: None)
    monkeypatch.setattr(entry, "nginx_test", lambda: None)
    monkeypatch.setattr(entry, "prepare_artifact_root", lambda: None)
    result = entry.apply()
    path = Path(entry.request["signing_env_file"])
    original = path.read_bytes()
    assert original.startswith(b"CF_GATEWAY_ARTIFACT_RETURN_KEY=")
    assert original.decode().split("=", 1)[1].strip() not in json.dumps(result)
    entry.apply()
    assert path.read_bytes() == original
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert calls[0][1]["writable"] is True


@pytest.mark.parametrize(
    "field", ["queued", "failed", "reconciliation_backlog", "missing_delivery"]
)
def test_queued_work_blocks_dispatch_start_without_model_side_effect(entry, monkeypatch, field):
    initialize(entry, "applied")
    peer(entry, "tls_ready")
    entry.state["queues_quiesced"]["dispatch"][field] = 1
    monkeypatch.setattr(
        entry, "compose", lambda *a, **k: pytest.fail("no create/start when queued")
    )
    with pytest.raises(RuntimeError, match="queued_work_blocks_dispatch_start"):
        entry.start()


def test_stopped_delivery_storage_probe_uses_isolated_candidate_not_worker_start(
    entry, monkeypatch
):
    calls = []
    monkeypatch.setattr(entry, "docker", lambda args: calls.append(args) or "hash")
    delivery = rows(entry, running=False)["delivery-worker"]
    assert entry.container_python(delivery, "print('hash')") == "hash"
    command = calls[0]
    assert command[:5] == ["run", "--rm", "--network", "none", "--read-only"]
    assert delivery["id"] + ":ro" in command
    assert "10001:10001" in command
    assert "start" not in command


def test_rendered_secret_must_never_reach_workers_or_migration(entry, monkeypatch):
    services = {
        name: {"image": entry.request["candidate_image"], "environment": {}} for name in rows(entry)
    }
    services["worker"]["environment"]["CF_GATEWAY_ARTIFACT_RETURN_KEY"] = (
        "synthetic-secret-do-not-print"
    )
    monkeypatch.setattr(entry, "compose", lambda _: json.dumps({"services": services}))
    with pytest.raises(RuntimeError, match="signing_secret_scope_violation") as caught:
        entry.check_rendered(entry.request["candidate_image"])
    assert "synthetic-secret" not in str(caught.value)


def test_rollback_keeps_gate_closed_and_requires_windows_rollback_before_start(entry, monkeypatch):
    initialize(entry, "applied")
    monkeypatch.setattr(entry, "containers", lambda: rows(entry, running=False))
    calls = []
    monkeypatch.setattr(entry, "assets", lambda *a, **k: calls.append(a[0]) or {})
    monkeypatch.setattr(entry, "check_rendered", lambda _: None)
    monkeypatch.setattr(entry, "nginx_test", lambda: None)
    result = entry.rollback()
    assert calls == ["rollback"]
    assert result["activation_allowed"] is False
    assert result["restart"] == "await_hermes_rolled_back_peer"
    peer(entry, "tls_ready")
    with pytest.raises(RuntimeError, match="peer_not_in_required_state"):
        entry.start(old=True)


def test_request_and_plan_identity_survive_restart_and_refuse_changes(module, entry, site_request):
    initialize(entry)
    value, path = site_request
    value["deployment_id"] = "different-window"
    path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="request_changed"):
        module.ServerEntry(
            path, release_commit=value["release_commit"], candidate_image=value["candidate_image"]
        )


def test_missing_ca_is_pending_not_invented_and_does_not_issue_certificate(entry):
    Path(entry.request["gateway_ca_file"]).unlink()
    result = entry.certificate_status()
    assert "gateway_ca_file" in result["pending"]
    assert result["activation_allowed"] is False
    assert result["signing"] == "use_existing_approved_CA_process_on_server"


def test_configuration_bind_must_be_the_managed_actual_file(entry, monkeypatch):
    monkeypatch.setattr(
        entry,
        "inspect",
        lambda _: {
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(entry.root / "config/production.local.yaml"),
                    "Destination": "/app/config/production.yaml",
                    "RW": False,
                }
            ],
            "Config": {"Env": []},
        },
    )
    with pytest.raises(RuntimeError, match="config_mount_not_managed"):
        entry.config_mounts(rows(entry), enabled=False)


def test_request_rejects_credentials_and_nonlocal_health(module, site_request):
    value, path = site_request
    value["gateway_health_url"] = "http://192.0.2.1:8080"
    path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="origin_invalid"):
        module.ServerEntry(
            path, release_commit=value["release_commit"], candidate_image=value["candidate_image"]
        )


@pytest.mark.parametrize("native", [False, True])
def test_candidate_compose_and_nginx_are_validated_before_plan(entry, monkeypatch, native):
    initialize(entry)
    directory = entry.asset_dir / ("e" * 64)
    compose = protected(directory / "0.after", b"candidate-compose")
    nginx = protected(directory / "1.after", b"candidate-nginx")
    probe = protected(directory / "references.validation.yaml", b"private-reference-probe")
    resolved = protected(directory / "resolved.validation.yaml", b"private-resolved-probe")
    services = {
        name: {"image": entry.request["candidate_image"], "env_file": []} for name in rows(entry)
    }
    for name in ("gateway", "dispatch-worker"):
        services[name]["env_file"] = [{"path": entry.request["signing_env_file"]}]
    commands, nginx_tests = [], []

    def docker(args, **kw):
        commands.append(args)
        if args == ["compose", "config", "--help"]:
            return "  --no-env-resolution  Skip env files" if native else "  --format string"
        if args == ["compose", "version", "--short"]:
            return "2.39.4" if native else "2.26.1"
        result = {"services": services}
        if str(resolved) in args:
            result = {"services": {k: {"image": v["image"]} for k, v in services.items()}}
        else:
            result["x-cf-return-validation-env"] = {
                name: service["env_file"] for name, service in services.items()
            }
        return json.dumps(result)

    monkeypatch.setattr(entry, "docker", docker)
    monkeypatch.setattr(
        entry,
        "assets",
        lambda *a, **kw: {
            "plan_sha256": "e" * 64,
            "reference_candidate": str(probe),
            "validation_candidate": str(resolved),
        },
    )
    monkeypatch.setattr(entry, "nginx_test", lambda **kw: nginx_tests.append(kw))
    result = {
        "plan_sha256": "e" * 64,
        "candidates": [
            {"path": str(entry.root / "docker-compose.prod.yml"), "candidate_file": str(compose)},
            {"path": str(entry.nginx / "nginx.conf"), "candidate_file": str(nginx)},
        ],
    }
    report = entry.validate_candidates(result)
    assert report["original_env_files_resolved"] is True
    assert ("--no-env-resolution" in commands[2]) is native
    assert "--no-env-resolution" not in commands[3]
    assert commands[2][commands[2].index("--profile") + 1] == "*"
    assert str(resolved) in commands[3]
    assert nginx_tests == [{"candidate": nginx}]
    services["delivery-worker"]["env_file"] = [{"path": entry.request["signing_env_file"]}]
    with pytest.raises(RuntimeError, match="candidate_signing_scope_violation"):
        entry.validate_candidates(result)

    services["delivery-worker"]["env_file"] = []
    commands.clear()

    def broken(args, **kw):
        if args[:3] == ["compose", "config", "--help"] or args[:3] == [
            "compose",
            "version",
            "--short",
        ]:
            return docker(args, **kw)
        commands.append(args)
        raise RuntimeError("command_failed")

    monkeypatch.setattr(entry, "docker", broken)
    with pytest.raises(RuntimeError, match="command_failed"):
        entry.validate_candidates(result)
    assert len(commands) == 3  # No retry/fallback after a real config failure.


def test_start_creates_all_four_but_starts_only_core_then_recreates_https(entry, monkeypatch):
    initialize(entry, "applied")
    peer(entry, "tls_ready")
    current = rows(entry, running=False, image=entry.request["candidate_image"])
    effects = []
    monkeypatch.setattr(entry, "containers", lambda: current)
    monkeypatch.setattr(entry, "check_rendered", lambda _: None)
    monkeypatch.setattr(entry, "nginx_test", lambda: None)
    monkeypatch.setattr(entry, "healthy_queues", lambda: entry.validate_health(health()))
    monkeypatch.setattr(entry, "nginx_definition", lambda: ("https", {}))

    def compose(args, **kw):
        effects.append(("compose", args, kw))
        return "9" * 64 if args[0] == "ps" else ""

    monkeypatch.setattr(entry, "compose", compose)
    monkeypatch.setattr(entry, "docker", lambda args: effects.append(("docker", args)))
    monkeypatch.setattr(entry, "inspect", lambda _: {"Image": entry.request["nginx_image"]})
    result = entry.start()
    creates = [row for row in effects if row[0] == "compose" and "--no-start" in row[1]]
    assert len(creates) == 1 and set(rows(entry)).issubset(creates[0][1])
    assert creates[0][1][:3] == ["up", "--no-start", "--no-deps"]
    starts = [row[1] for row in effects if row[0] == "docker"]
    assert starts == [
        ["start", current["gateway"]["id"]],
        ["start", current["dispatch-worker"]["id"]],
    ]
    assert any(
        row[0] == "compose" and "--force-recreate" in row[1] and row[2].get("nginx")
        for row in effects
    )
    assert result["activation_allowed"] is False


def test_init_request_discovers_public_identities_leaves_tls_pending_and_preserves_inputs(
    module, site_request, tmp_path, monkeypatch
):
    value, _ = site_request
    current = {
        name: {"id": str(i + 1) * 64, "image": value["baseline_image"], "running": True}
        for i, name in enumerate(module.APPS)
    }
    monkeypatch.setattr(module.ServerEntry, "containers", lambda _: current)

    def compose(self, args, **kw):
        if args[0] == "config":
            return json.dumps({"services": {"https": {"image": value["nginx_image"]}}})
        return "9" * 64

    monkeypatch.setattr(module.ServerEntry, "compose", compose)

    def inspect(self, identifier):
        return {
            "Image": value["nginx_image"],
            "NetworkSettings": {
                "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18999"}]}
            },
        }

    monkeypatch.setattr(module.ServerEntry, "inspect", inspect)
    options = SimpleNamespace(
        request=str(tmp_path / "generated.json"),
        release_commit=value["release_commit"],
        candidate_image=value["candidate_image"],
        release_root=value["release_root"],
        gateway_root=value["gateway_root"],
        nginx_root=value["nginx_root"],
        deployment_id="return-test",
        peer_public=None,
        state_directory=value["state_directory"],
        hermes_ca_file=None,
        gateway_ca_file=None,
    )
    result = module.init_request(options)
    assert set(result["missing_public_references"]) == {
        "hermes_origin",
        "gateway_origin",
        "hermes_ca_file",
        "gateway_ca_file",
        "peer_state_file",
    }
    generated = json.loads(Path(options.request).read_bytes())
    assert generated["baseline_image"] == value["baseline_image"]
    assert generated["gateway_health_url"] == "http://127.0.0.1:18999"
    assert generated["profile_reference"] == "default"
    assert not Path(value["signing_env_file"]).exists()
    assert not result["configuration_changed"]
    public = {
        "schema": module.PEER_SCHEMA,
        "role": "hermes",
        "release_commit": value["release_commit"],
        "deployment_id": "return-test",
        "activation_allowed": False,
        "public_references": {
            "hermes_origin": value["hermes_origin"],
            "gateway_origin": value["gateway_origin"],
        },
    }
    options.peer_public = str(protected(tmp_path / "public.json", json.dumps(public).encode()))
    options.hermes_ca_file, options.gateway_ca_file = (
        value["hermes_ca_file"],
        value["gateway_ca_file"],
    )
    assert module.init_request(options)["missing_public_references"] == []
    assert list(tmp_path.glob("generated.json.pending-*.json"))


def test_apply_prepares_only_recorded_artifact_root_and_retains_private_rollback(
    entry, monkeypatch
):
    initialize(entry, "applying")
    before = {
        "path": "/var/lib/cf-agent-gateway/artifacts",
        "exists": True,
        "uid": 10001,
        "gid": 10001,
        "mode": 0o755,
    }
    entry.state["artifact_before"] = before
    monkeypatch.setattr(entry, "stopped", lambda: rows(entry, running=False))
    monkeypatch.setattr(entry, "audit_artifact_root", lambda _: before)
    calls = []

    def execute(row, code, **kw):
        calls.append((row, code, kw))
        return json.dumps({**before, "mode": 0o700})

    monkeypatch.setattr(entry, "container_python", execute)
    entry.prepare_artifact_root()
    assert calls[0][2] == {"writable": True}
    assert "p.chmod(0o700)" in calls[0][1]
    assert "chown" not in calls[0][1]
    assert entry.state["artifact_before"]["mode"] == 0o755
    assert entry.state["artifact_rollback_policy"] == "retain_private_root_and_contents"


def test_nginx_check_uses_existing_nonroot_identity_and_private_tmpfs(entry, monkeypatch):
    monkeypatch.setattr(
        entry, "nginx_definition", lambda: ("https", {"user": "101:101", "volumes": []})
    )
    calls = []
    monkeypatch.setattr(entry, "docker", lambda args, **kw: calls.append(args))
    entry._nginx_test()
    command = calls[0]
    assert command[command.index("--user") + 1] == "101:101"
    assert "/tmp:uid=101,gid=101,mode=0700" in command


def test_peer_public_references_and_legacy_process_are_required(entry):
    initialize(entry, "rolled_back")
    peer(entry, "rolled_back")
    path = Path(entry.request["peer_state_file"])
    value = json.loads(path.read_bytes())
    value["legacy_process_started"] = False
    path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="legacy_hermes_not_started"):
        entry.peer("rolled_back")
    value["legacy_process_started"] = True
    value["public_references"]["gateway_ca_sha256"] = "0" * 64
    path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="peer_public_references_mismatch"):
        entry.peer("rolled_back")


def schema_fixture(entry, monkeypatch, candidate=None, live=None):
    current = rows(entry)
    monkeypatch.setattr(entry, "containers", lambda: current)
    monkeypatch.setattr(
        entry,
        "inspect",
        lambda _: {
            "Image": entry.request["baseline_image"],
            "NetworkSettings": {
                "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18999"}]}
            },
        },
    )
    healthy = {"expected": "20260928_01", "packaged_heads": ["20260928_01"]}
    calls = []

    def docker(args, **kw):
        calls.append(args)
        return json.dumps((candidate or healthy) if args[0] == "run" else (live or healthy))

    monkeypatch.setattr(entry, "docker", docker)
    monkeypatch.setattr(entry, "healthy_queues", lambda: entry.validate_health(health()))
    return current, calls


def test_plan_schema_proof_combines_offline_head_with_live_readonly_health(entry, monkeypatch):
    current, calls = schema_fixture(entry, monkeypatch)
    result = entry.schema_compatibility(current)
    assert result["candidate_packaged_head"] == result["live_packaged_head"] == "20260928_01"
    assert result["schema_change_required"] is False
    assert calls[0][:5] == ["run", "--rm", "--network", "none", "--read-only"]
    assert calls[1][:4] == ["exec", "--user", "10001:10001", current["gateway"]["id"]]
    assert all(
        "upgrade" not in " ".join(call) and "initialize_database" not in " ".join(call)
        for call in calls
    )


@pytest.mark.parametrize(
    "candidate,error",
    [
        (
            {"expected": "new_head", "packaged_heads": ["new_head"]},
            "candidate_schema_change_not_authorized",
        ),
        (
            {"expected": "20260928_01", "packaged_heads": ["20260928_01", "other"]},
            "migration_head_not_single_or_consistent",
        ),
    ],
)
def test_plan_rejects_schema_change_and_fork_without_upgrade(entry, monkeypatch, candidate, error):
    current, _ = schema_fixture(entry, monkeypatch, candidate=candidate)
    with pytest.raises(RuntimeError, match=error):
        entry.schema_compatibility(current)


def test_matching_packaged_heads_do_not_override_unhealthy_database(entry, monkeypatch):
    current, _ = schema_fixture(entry, monkeypatch)
    value = health()
    value["components"]["migration_schema"]["status"] = "mismatch"
    monkeypatch.setattr(entry, "healthy_queues", lambda: entry.validate_health(value))
    with pytest.raises(RuntimeError, match="database_or_schema_unhealthy"):
        entry.schema_compatibility(current)


@pytest.mark.parametrize("old", [False, True])
def test_completed_start_reemits_lost_receipt_readonly_and_refuses_changed_state(
    entry, monkeypatch, old
):
    initialize(entry, "old_started" if old else "started")
    peer(entry, "rolled_back" if old else "tls_ready")
    current = rows(entry, image=entry.request["baseline_image" if old else "candidate_image"])
    for service in ("worker", "delivery-worker"):
        current[service]["running"] = False
    monkeypatch.setattr(entry, "containers", lambda: current)
    monkeypatch.setattr(entry, "check_rendered", lambda _: None)
    monkeypatch.setattr(entry, "config_mounts", lambda *a, **k: None)
    monkeypatch.setattr(entry, "healthy_queues", lambda: entry.validate_health(health()))
    monkeypatch.setattr(entry, "compose", lambda *a, **k: pytest.fail("must not recreate services"))
    monkeypatch.setattr(entry, "docker", lambda *a, **k: pytest.fail("must not restart containers"))
    first = entry.start(old=old)
    receipt = (entry.state_dir / "peer-receipt.json").read_bytes()
    assert entry.start(old=old) == first
    assert (entry.state_dir / "peer-receipt.json").read_bytes() == receipt
    current["dispatch-worker"]["running"] = False
    with pytest.raises(RuntimeError, match="runtime_receipt_verification_failed"):
        entry.start(old=old)
    assert first["activation_allowed"] is False


def test_fresh_failed_queue_blocks_dispatch_after_api_start(entry, monkeypatch):
    initialize(entry, "applied")
    peer(entry, "tls_ready")
    current = rows(entry, running=False, image=entry.request["candidate_image"])
    fresh = entry.validate_health(health())
    fresh["dispatch"]["failed"] = 1
    monkeypatch.setattr(entry, "containers", lambda: current)
    monkeypatch.setattr(entry, "check_rendered", lambda _: None)
    monkeypatch.setattr(entry, "nginx_test", lambda: None)
    monkeypatch.setattr(entry, "compose", lambda *a, **k: "")
    monkeypatch.setattr(entry, "healthy_queues", lambda: fresh)
    starts = []
    monkeypatch.setattr(entry, "docker", lambda args: starts.append(args))
    with pytest.raises(RuntimeError, match="live_queue_blocks_dispatch_start"):
        entry.start()
    assert starts == [["start", current["gateway"]["id"]]]
