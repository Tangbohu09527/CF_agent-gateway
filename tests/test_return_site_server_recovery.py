"""Interrupted server rollback recovery, with no Docker daemon or site access."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_return_site_server_entry import entry as entry
from test_return_site_server_entry import health, peer, protected, rows
from test_return_site_server_entry import module as module  # Shared isolated loader fixture.
from test_return_site_server_entry import site_request as _original_site_request

from cf_agent_gateway.hermes.return_bridge import enablement as assets
from cf_agent_gateway.hermes.return_bridge import site_server

original_site_request = _original_site_request


@pytest.fixture
def site_request(original_site_request, module):
    value, path = original_site_request
    value["release_commit"] = module.RECOVERY_RELEASE
    protected(path, json.dumps(value).encode())
    return value, path


@pytest.fixture
def restored(entry, module, monkeypatch):
    """Real asset journal already rolled back; outer host state is interrupted."""
    tag = "localhost/cf-agent-gateway:inbound-0ec54bf-build-few9m9fp"
    rendered = {"services": {name: {"image": tag, "environment": {}} for name in module.APPS}}
    compose = protected(entry.root / "docker-compose.prod.yml", json.dumps(rendered).encode())
    original_env = protected(entry.root / ".env", ("CF_GATEWAY_IMAGE=" + tag + "\n").encode())
    nginx_compose = protected(entry.nginx / "compose.yaml", b"services: {https: {image: kept}}\n")
    targets = (entry.root / "config/production.yaml", compose, entry.nginx / "nginx.conf")
    changes = tuple(
        assets.Change(path, ("isolated_fixture",), path.read_bytes(), path.read_bytes() + b"\n")
        for path in targets
    )
    references = (Path(entry.request["hermes_ca_file"]), original_env, nginx_compose)
    plan = assets.Plan(
        "return-site-server",
        changes,
        (
            {
                "kind": "fixed_release",
                "commit": entry.request["release_commit"],
                "image": entry.request["candidate_image"],
                "rollback_image": entry.request["baseline_image"],
            },
        )
        + tuple(
            {
                "kind": "unchanged_reference",
                "path": str(path),
                "sha256": module.digest(path.read_bytes()),
            }
            for path in references
        ),
        request_sha256=site_server._request_hash(entry.request),
    )
    prepared = site_server.prepare(plan, entry.asset_dir)
    plan_hash = prepared["plan_sha256"]
    manifest = Path(prepared["manifest"])
    assets.apply_plan(plan, entry.asset_dir, expected_plan_sha256=plan_hash)
    assets.rollback(manifest)
    proof = {
        "schema": "cf-return-compose-proof/v1",
        "request_sha256": entry.request_hash,
        "plan_sha256": plan_hash,
        "before_file": str(manifest.parent / "1.before"),
        "before_sha256": module.digest(changes[1].before),
        "candidate_file": str(manifest.parent / "1.after"),
        "candidate_sha256": module.digest(changes[1].after),
        "references": {},
        "files": [entry.snapshot_env_file(original_env)],
    }
    proof_file = protected(
        manifest.parent / "compose-environment-proof.json", module.encoded(proof)
    )
    current = rows(entry, running=False)
    entry.persist(
        "rolling_back",
        release_commit=entry.request["release_commit"],
        deployment_id=entry.request["deployment_id"],
        request_sha256=entry.request_hash,
        plan_sha256=plan_hash,
        baseline=copy.deepcopy(current),
        queues_quiesced=entry.validate_health(health()),
        compose_validation={
            "proof_file": str(proof_file),
            "proof_sha256": module.digest(proof_file.read_bytes()),
        },
    )
    image_ids = {tag: entry.request["baseline_image"]}
    image_calls = []
    effects = []

    def image(reference):
        image_calls.append(reference)
        if reference not in image_ids:
            raise module.EntryError("command_failed")
        return {"Id": image_ids[reference]}

    monkeypatch.setattr(entry, "image", image)
    monkeypatch.setattr(entry, "candidate_config", lambda path, **_: copy.deepcopy(rendered))
    live_rendered = copy.deepcopy(rendered)
    monkeypatch.setattr(entry, "compose", lambda *a, **k: json.dumps(live_rendered))
    monkeypatch.setattr(entry, "containers", lambda: current)
    monkeypatch.setattr(entry, "nginx_test", lambda: effects.append("nginx-check"))
    monkeypatch.setattr(entry, "assets", lambda *a, **k: pytest.fail("must not rerun asset helper"))
    monkeypatch.setattr(entry, "quiesce", lambda *a, **k: pytest.fail("must already be stopped"))
    return SimpleNamespace(
        entry=entry,
        module=module,
        tag=tag,
        current=current,
        rendered=live_rendered,
        ids=image_ids,
        image_calls=image_calls,
        manifest=manifest,
        targets=targets,
        references=references,
        effects=effects,
    )


def asset_snapshot(restored):
    paths = [
        *restored.targets,
        *restored.references,
        *(path for path in restored.entry.asset_dir.rglob("*") if path.is_file()),
    ]
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def test_baseline_local_tag_resolves_to_exact_reviewed_image_id(restored):
    entry = restored.entry
    result = entry.check_rendered(entry.request["baseline_image"], baseline=True)
    assert result == restored.rendered
    assert restored.image_calls and set(restored.image_calls) == {restored.tag}
    with pytest.raises(RuntimeError, match="^rendered_four_images_mismatch$"):
        entry.check_rendered(entry.request["candidate_image"])


def test_candidate_still_requires_literal_immutable_image_even_when_tag_resolves_to_it(restored):
    restored.ids[restored.tag] = restored.entry.request["candidate_image"]
    with pytest.raises(RuntimeError, match="^rendered_four_images_mismatch$"):
        restored.entry.check_rendered(restored.entry.request["candidate_image"])
    assert not restored.image_calls


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_restored_live_files_keep_only_their_own_service_identity(
    restored, monkeypatch, wrong_owner
):
    # Model POSIX metadata without changing real file owners on any platform.
    proof = restored.entry.verify_compose_proof()
    monkeypatch.setattr(restored.entry, "verify_compose_proof", lambda: proof)
    original_lstat = Path.lstat
    owners = {restored.targets[0]: 10001, restored.targets[2]: 101}
    if wrong_owner:
        owners[restored.targets[2]] = 10001

    def metadata(path):
        actual = original_lstat(path)
        return SimpleNamespace(
            st_mode=actual.st_mode & ~0o022,
            st_uid=owners.get(path, 0),
            st_nlink=actual.st_nlink,
        )

    # Journal files must remain mode 0600, even on a Windows test host.
    private_paths = set(restored.manifest.parent.iterdir())

    def private_metadata(path):
        actual = metadata(path)
        if path in private_paths:
            actual.st_mode = (actual.st_mode & ~0o777) | 0o600
        return actual

    monkeypatch.setattr(Path, "lstat", private_metadata)
    monkeypatch.setattr(restored.module, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    if wrong_owner:
        with pytest.raises(RuntimeError, match="^path_owner_mismatch$"):
            restored.entry.verify_restored_assets()
    else:
        assert restored.entry.verify_restored_assets()["files_match_original"] is True


@pytest.mark.parametrize(
    "fault,error",
    [
        ("wrong-id", "baseline_image_identity_mismatch"),
        ("reference-drift", "baseline_image_reference_changed"),
        ("missing-local-image", "command_failed"),
        ("wrong-baseline-record", "baseline_record_mismatch"),
    ],
)
def test_baseline_reference_and_identity_fail_before_recovery_writes(restored, fault, error):
    if fault == "wrong-id":
        restored.ids[restored.tag] = restored.entry.request["candidate_image"]
    elif fault == "reference-drift":
        restored.rendered["services"]["gateway"]["image"] = "same-image:unreviewed-alias"
        restored.ids["same-image:unreviewed-alias"] = restored.entry.request["baseline_image"]
    elif fault == "missing-local-image":
        del restored.ids[restored.tag]
    else:
        restored.entry.state["baseline"]["gateway"]["image"] = restored.entry.request[
            "candidate_image"
        ]
    before = asset_snapshot(restored)
    state = restored.entry.state_path.read_bytes()
    with pytest.raises(RuntimeError, match="^" + error + "$"):
        restored.entry.complete_rollback()
    assert asset_snapshot(restored) == before
    assert restored.entry.state_path.read_bytes() == state
    assert restored.entry.state["phase"] == "rolling_back"


def test_interrupted_outer_rollback_finishes_without_rewriting_restored_assets(restored):
    before = asset_snapshot(restored)
    old_state = restored.entry.state_path.read_bytes()
    peer_receipt = protected(restored.entry.state_dir / "peer-receipt.json", b'{"old":true}\n')
    old_peer_receipt = peer_receipt.read_bytes()
    result = restored.entry.complete_rollback()
    assert restored.entry.state["phase"] == "rolled_back"
    assert json.loads(restored.manifest.read_bytes())["state"] == "rolled_back"
    assert asset_snapshot(restored) == before
    assert result["state"] == "rolled_back" and result["activation_allowed"] is False
    assert result["restart"] == "await_hermes_rolled_back_peer"
    assert all(not row["running"] for row in restored.current.values())
    archives = list(restored.entry.state_dir.glob("rollback-before-*.json"))
    assert sorted(path.read_bytes() for path in archives) == sorted([old_state, old_peer_receipt])
    assert restored.entry.complete_rollback() == result
    assert asset_snapshot(restored) == before


def test_ordinary_rollback_also_uses_baseline_identity_after_asset_helper(restored, monkeypatch):
    calls = []

    def asset_rollback(stage, **kwargs):
        calls.append((stage, kwargs))
        return assets.rollback(restored.manifest)

    monkeypatch.setattr(restored.entry, "assets", asset_rollback)
    result = restored.entry.rollback()
    assert result["state"] == "rolled_back"
    assert calls == [
        ("rollback", {"writable": True, "plan_sha": restored.entry.state["plan_sha256"]})
    ]
    assert restored.image_calls == [restored.tag]


@pytest.mark.parametrize("phase", ["rolled_back", "old_starting", "old_started"])
def test_start_old_rejects_tag_retarget_after_completion_before_any_start(
    restored, monkeypatch, phase
):
    restored.entry.complete_rollback()
    peer(restored.entry, "rolled_back")
    restored.entry.persist(phase)
    restored.ids[restored.tag] = restored.entry.request["candidate_image"]
    monkeypatch.setattr(
        restored.entry, "docker", lambda *a, **k: pytest.fail("must not start containers")
    )
    with pytest.raises(RuntimeError, match="^baseline_image_identity_mismatch$"):
        restored.entry.start(old=True)
    assert all(not row["running"] for row in restored.current.values())


@pytest.mark.parametrize(
    "fault,error",
    [
        ("asset-state", "rollback_journal_not_restored"),
        ("live-asset", "rollback_asset_mismatch"),
        ("backup", "rollback_asset_mismatch"),
        ("candidate-backup", "rollback_asset_mismatch"),
        ("reference", "rollback_reference_changed"),
        ("plan-hash", "rollback_plan_mismatch"),
    ],
)
def test_readonly_restored_asset_proof_rejects_damage_without_repairing_it(restored, fault, error):
    if fault in {"asset-state", "plan-hash"}:
        data = json.loads(restored.manifest.read_bytes())
        if fault == "asset-state":
            data["state"] = "rolling_back"
        else:
            data["plan"]["request_sha256"] = "0" * 64
        restored.manifest.write_bytes(json.dumps(data).encode())
    else:
        path = {
            "live-asset": restored.targets[0],
            "backup": restored.manifest.parent / "0.before",
            "candidate-backup": restored.manifest.parent / "0.after",
            "reference": restored.references[0],
        }[fault]
        path.write_bytes(path.read_bytes() + b"operator-change-must-survive\n")
    before = asset_snapshot(restored)
    with pytest.raises(RuntimeError, match="^" + error + "$"):
        restored.entry.verify_restored_assets()
    assert asset_snapshot(restored) == before


@pytest.mark.parametrize(
    "fault,error",
    [
        ("running", "apps_not_quiesced"),
        ("wrong-image", "baseline_four_images_mismatch"),
        ("nginx", "command_failed"),
        ("container-drift", "container_identity_changed"),
        ("late-asset-drift", "rollback_asset_mismatch"),
        ("phase", "stage_order_invalid"),
    ],
)
def test_completion_cannot_issue_success_before_all_checks_pass(
    restored, monkeypatch, fault, error
):
    entry = restored.entry
    if fault == "running":
        restored.current["gateway"]["running"] = True
    elif fault == "wrong-image":
        restored.current["gateway"]["image"] = entry.request["candidate_image"]
    elif fault == "phase":
        entry.persist("applied")
    else:
        monkeypatch.setattr(entry, "containers", lambda: copy.deepcopy(restored.current))

        def changed_check():
            if fault == "nginx":
                raise restored.module.EntryError("command_failed")
            if fault == "container-drift":
                restored.current["gateway"]["id"] = "8" * 64
            else:
                restored.targets[0].write_bytes(b"external-drift-must-not-be-repaired\n")

        monkeypatch.setattr(entry, "nginx_test", changed_check)
    old_state = entry.state_path.read_bytes()
    before = asset_snapshot(restored)
    with pytest.raises(RuntimeError, match="^" + error + "$"):
        entry.complete_rollback()
    assert entry.state_path.read_bytes() == old_state
    assert not (entry.state_dir / "peer-receipt.json").exists()
    if fault == "late-asset-drift":
        assert restored.targets[0].read_bytes() == b"external-drift-must-not-be-repaired\n"
    else:
        assert asset_snapshot(restored) == before


def test_finished_recovery_can_start_old_with_gate_closed(restored, monkeypatch):
    entry = restored.entry
    entry.complete_rollback()
    peer(entry, "rolled_back")
    effects = []

    def compose(arguments, **kwargs):
        effects.append(("compose", arguments, kwargs))
        if arguments[0] == "config":
            return json.dumps(restored.rendered)
        return "9" * 64 if arguments[0] == "ps" else ""

    def docker(arguments, **kwargs):
        effects.append(("docker", arguments, kwargs))
        assert arguments[0] == "start"
        match = [row for row in restored.current.values() if row["id"] == arguments[1]]
        assert len(match) == 1
        match[0]["running"] = True

    monkeypatch.setattr(entry, "compose", compose)
    monkeypatch.setattr(entry, "docker", docker)
    monkeypatch.setattr(entry, "nginx_definition", lambda: ("https", {}))
    monkeypatch.setattr(entry, "inspect", lambda _: {"Image": entry.request["nginx_image"]})
    monkeypatch.setattr(entry, "healthy_queues", lambda: entry.validate_health(health()))
    monkeypatch.setattr(entry, "config_mounts", lambda *a, **k: None)
    before = asset_snapshot(restored)
    result = entry.start(old=True)
    assert entry.state["phase"] == "old_started"
    assert result["activation_allowed"] is False
    assert [row[1] for row in effects if row[0] == "docker"] == [
        ["start", restored.current["gateway"]["id"]],
        ["start", restored.current["dispatch-worker"]["id"]],
    ]
    assert all(not restored.current[service]["running"] for service in restored.module.GATED)
    assert asset_snapshot(restored) == before
    count = len(effects)
    assert entry.start(old=True) == result
    assert all(row[1][0] == "config" for row in effects[count:])


def test_command_runner_disconnects_stdin_and_does_not_inherit_credentials(module, monkeypatch):
    calls = []
    monkeypatch.setenv("RECOVERY_SYNTHETIC_SECRET", "must-not-inherit")

    def spawn(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, communicate=lambda **_: ("{}", ""))

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    assert module.Commands().run(["docker", "image", "inspect", "isolated:baseline"]) == "{}"
    assert calls[0][1]["stdin"] is module.subprocess.DEVNULL
    assert "RECOVERY_SYNTHETIC_SECRET" not in calls[0][1]["env"]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["env"]["GIT_OPTIONAL_LOCKS"] == "0"


def test_temporary_docker_run_never_pulls_without_using_unsupported_compose_flag(
    entry, monkeypatch
):
    calls = []
    monkeypatch.setattr(entry, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    entry.docker(["run", "--rm", entry.request["nginx_image"], "nginx", "-t"])
    assert calls[0][0] == [
        "docker",
        "--context",
        "default",
        "run",
        "--pull=never",
        "--rm",
        entry.request["nginx_image"],
        "nginx",
        "-t",
    ]
    entry.compose(["run", "--rm", "gateway", "python", "--version"])
    assert not any(argument.startswith("--pull") for argument in calls[1][0])


@pytest.fixture
def recovery_controller(restored, monkeypatch):
    entry, module = restored.entry, restored.module
    commit = "1" * 40
    script = Path(module.__file__).resolve()
    calls = []
    preflight = []
    outputs = {
        "rev-parse": commit,
        "status": "",
        "show": script.read_text(encoding="utf-8").strip(),
        "merge-base": "",
    }

    def run(arguments, **kwargs):
        assert arguments[:3] == ["git", "-C", str(script.parent.parent)]
        calls.append(arguments)
        outcome = outputs[arguments[3]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def old_preflight():
        preflight.append(
            (entry.request["release_commit"], entry.request["candidate_image"], entry.release)
        )

    monkeypatch.setattr(entry, "run", run)
    monkeypatch.setattr(entry, "preflight", old_preflight)
    return SimpleNamespace(
        restored=restored,
        commit=commit,
        script=script,
        calls=calls,
        preflight=preflight,
        outputs=outputs,
    )


def test_fixed_new_controller_verifies_unchanged_old_release_then_original_assets(
    recovery_controller,
):
    probe = recovery_controller
    restored, entry = probe.restored, probe.restored.entry
    before = asset_snapshot(restored)
    request_bytes = entry.request_path.read_bytes()
    state_bytes = entry.state_path.read_bytes()
    entry.recovery_preflight(probe.commit, entry.state["plan_sha256"], "complete-rollback")
    assert probe.preflight == [
        (restored.module.RECOVERY_RELEASE, entry.request["candidate_image"], entry.release)
    ]
    assert probe.calls[-1][3:] == [
        "merge-base",
        "--is-ancestor",
        restored.module.RECOVERY_RELEASE,
        probe.commit,
    ]
    assert entry.recovery_controller == {
        "commit": probe.commit,
        "script_sha256": restored.module.digest(probe.script.read_bytes()),
    }
    assert asset_snapshot(restored) == before
    assert entry.request_path.read_bytes() == request_bytes
    assert entry.state_path.read_bytes() == state_bytes


@pytest.mark.parametrize(
    "fault,error",
    [
        ("stage", "recovery_stage_not_supported"),
        ("unsupported-old-release", "recovery_binding_required"),
        ("old-controller", "recovery_binding_required"),
        ("short-controller", "recovery_binding_required"),
        ("wrong-plan", "recovery_binding_mismatch"),
        ("state-release", "recovery_binding_mismatch"),
        ("state-deployment", "recovery_binding_mismatch"),
        ("same-checkout", "separate_recovery_checkout_required"),
        ("controller-head", "recovery_controller_not_fixed"),
        ("controller-dirty", "recovery_controller_not_fixed"),
        ("controller-script", "recovery_controller_not_fixed"),
        ("not-descendant", "command_failed"),
    ],
)
def test_recovery_controller_rejects_binding_drift_without_writes(
    recovery_controller, fault, error
):
    probe = recovery_controller
    restored, entry = probe.restored, probe.restored.entry
    commit, plan, stage = probe.commit, entry.state["plan_sha256"], "complete-rollback"
    if fault == "stage":
        stage = "apply"
    elif fault == "unsupported-old-release":
        entry.request["release_commit"] = "2" * 40
    elif fault == "old-controller":
        commit = restored.module.RECOVERY_RELEASE
    elif fault == "short-controller":
        commit = "1234567"
    elif fault == "wrong-plan":
        plan = "0" * 64
    elif fault == "state-release":
        entry.state["release_commit"] = "2" * 40
    elif fault == "state-deployment":
        entry.state["deployment_id"] = "another-isolated-deployment"
    elif fault == "same-checkout":
        entry.release = probe.script.parent.parent
    elif fault == "controller-head":
        probe.outputs["rev-parse"] = "2" * 40
    elif fault == "controller-dirty":
        probe.outputs["status"] = " M deploy/prepare-return-server.py"
    elif fault == "controller-script":
        probe.outputs["show"] += "\nchanged source"
    else:
        probe.outputs["merge-base"] = restored.module.EntryError("command_failed")
    before = asset_snapshot(restored)
    state_bytes = entry.state_path.read_bytes()
    with pytest.raises(RuntimeError, match="^" + error + "$"):
        entry.recovery_preflight(commit, plan, stage)
    assert not probe.preflight
    assert asset_snapshot(restored) == before
    assert entry.state_path.read_bytes() == state_bytes


def test_start_old_requires_completion_by_the_same_verified_new_controller(recovery_controller):
    probe = recovery_controller
    entry = probe.restored.entry
    plan = entry.state["plan_sha256"]
    with pytest.raises(RuntimeError, match="^recovery_completion_required$"):
        entry.recovery_preflight(probe.commit, plan, "start-old")
    entry.recovery_preflight(probe.commit, plan, "complete-rollback")
    entry.complete_rollback()
    entry.recovery_preflight(probe.commit, plan, "start-old")
    entry.state["rollback_completion"]["controller"]["commit"] = "3" * 40
    with pytest.raises(RuntimeError, match="^recovery_completion_required$"):
        entry.recovery_preflight(probe.commit, plan, "start-old")


def test_new_controller_does_not_bypass_old_release_preflight(recovery_controller, monkeypatch):
    probe = recovery_controller
    entry = probe.restored.entry

    def rejected():
        raise probe.restored.module.EntryError("candidate_revision_label_mismatch")

    monkeypatch.setattr(entry, "preflight", rejected)
    with pytest.raises(RuntimeError, match="^candidate_revision_label_mismatch$"):
        entry.recovery_preflight(probe.commit, entry.state["plan_sha256"], "complete-rollback")
    assert entry.state["phase"] == "rolling_back"
