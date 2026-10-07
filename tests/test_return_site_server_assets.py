"""Only deployment candidates in tmp_path; no site, services or model requests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cf_agent_gateway.hermes.return_bridge import enablement as assets
from cf_agent_gateway.hermes.return_bridge import site_server as site


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "gateway"
    (root / "config").mkdir(parents=True)
    compose = Path(__file__).parents[1] / "docker-compose.prod.yml"
    (root / "docker-compose.prod.yml").write_bytes(compose.read_bytes())
    (root / ".env").write_text("CF_GATEWAY_API_TOKEN=synthetic-original-keep-private\n")
    config = {
        "hermes": {"enabled": True, "base_url": "http://192.0.2.2:8642", "model": "keep"},
        "artifact": {"storage_root": "/var/lib/cf-agent-gateway/artifacts"},
        "api": {"max_request_body_bytes": 16384},
        "wechat": {"legacy_runtime_confirmed": False},
        "unknown": {"preserve": [1, 2]},
    }
    (root / "config/production.yaml").write_text(yaml.safe_dump(config))
    nginx = tmp_path / "nginx"
    nginx.mkdir()
    (nginx / "compose.yaml").write_text("services: {https: {image: preserved}}\n")
    (nginx / "nginx.conf").write_text("synthetic config, mocked parser in asset tests\n")
    from test_hermes_tls import _test_certificates

    ca, _, _ = _test_certificates(tmp_path)
    request = {
        "schema": site.SCHEMA,
        "release_commit": "a" * 40,
        "candidate_image": "sha256:" + "b" * 64,
        "baseline_image": "sha256:" + "c" * 64,
        "gateway_root": str(root),
        "nginx_root": str(nginx),
        "hermes_ca_file": str(ca),
        "signing_env_file": str(root / "secrets/artifact-return.env"),
        "hermes_origin": "https://hermes.example:9443",
        "gateway_origin": "https://gateway.example:18444",
        "profile_reference": "existing-profile",
    }
    return request


def mock_ingress(monkeypatch):
    from cf_agent_gateway.hermes.return_bridge import site_nginx

    def planner(path, **_):
        return assets.Plan(
            "nginx", (assets.Change(path, ("route",), path.read_bytes(), b"new mock route\n"),), ()
        )

    monkeypatch.setattr(site_nginx, "plan_nginx", planner)


def test_compose_preserves_shared_env_and_all_unmanaged_fields(deployment):
    path = Path(deployment["gateway_root"]) / "docker-compose.prod.yml"
    old = yaml.safe_load(path.read_bytes())
    change = site.plan_compose(path, deployment)
    new = yaml.safe_load(change.after)
    assert new["x-runtime"] == old["x-runtime"]
    assert new["networks"] == old["networks"]
    assert new["services"]["migration"] == old["services"]["migration"]
    assert new["services"]["heartbeat-init"] == old["services"]["heartbeat-init"]
    for name in site.APPS:
        service, prior = new["services"][name], old["services"][name]
        assert service["image"] == deployment["candidate_image"]
        assert service["environment"] == prior["environment"]
        if name in {"gateway", "dispatch-worker"}:
            assert service["env_file"] == [*prior["env_file"], deployment["signing_env_file"]]
            assert service["volumes"][-1]["read_only"] is True
            assert service["volumes"][-1]["target"] == site.CA_TARGET
        else:
            assert service["env_file"] == prior["env_file"]
            assert service["volumes"] == prior["volumes"]


@pytest.mark.parametrize("fault", ["different-volume", "read-only", "wrong-user", "signing-inline"])
def test_unsafe_site_shapes_rejected(deployment, fault):
    path = Path(deployment["gateway_root"]) / "docker-compose.prod.yml"
    config = yaml.safe_load(path.read_bytes())
    service = config["services"]["delivery-worker"]
    if fault == "different-volume":
        service["volumes"][1] = "business-files:/var/lib/cf-agent-gateway"
    elif fault == "read-only":
        service["volumes"][1] = "gateway-state:/var/lib/cf-agent-gateway:ro"
    elif fault == "wrong-user":
        service["user"] = "0:0"
    else:
        service["environment"][site.SIGNING_ENV] = "never-log-this"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(assets.EnablementError):
        site.plan_compose(path, deployment)


def test_plan_does_not_apply_and_apply_resume_rollback_are_exact(deployment, tmp_path, monkeypatch):
    mock_ingress(monkeypatch)
    plan = site.plan_server(deployment)
    raw = {item.path: item.before for item in plan.changes}
    state = tmp_path / "private-state"
    public = site.prepare(plan, state)
    assert all(path.read_bytes() == before for path, before in raw.items())
    assert not Path(deployment["signing_env_file"]).exists()
    assert "synthetic-original-keep-private" not in json.dumps(public)
    assert not any("api_key" in str(row) for row in public["changes"])
    config = yaml.safe_load(plan.changes[0].after)
    assert (
        config["api"]["max_request_body_bytes"]
        == config["artifact_return"]["max_bytes"]
        == site.LIMIT
    )
    assert config["wechat"]["legacy_runtime_confirmed"] is False
    assert config["unknown"] == {"preserve": [1, 2]}
    assert config["hermes"]["model"] == "keep"
    resumed, manifest = site._restore_plan(deployment, state, public["plan_sha256"])
    # Crash simulation after one known candidate is installed; original journal
    # remains the authority, not a fresh plan made from partially changed files.
    assets._replace(resumed.changes[0].path, resumed.changes[0].after)
    result = assets.apply_plan(resumed, state, expected_plan_sha256=public["plan_sha256"])
    assert result["state"] == "applied"
    assert assets.apply_plan(resumed, state, expected_plan_sha256=public["plan_sha256"]) == result
    assert assets.rollback(manifest)["state"] == "rolled_back"
    assert all(path.read_bytes() == before for path, before in raw.items())
    assert assets.rollback(manifest)["state"] == "rolled_back"


@pytest.mark.parametrize("changed", ["request", "ca", "env", "target", "backup"])
def test_changed_site_material_fails_closed(deployment, tmp_path, monkeypatch, changed):
    mock_ingress(monkeypatch)
    plan = site.plan_server(deployment)
    state = tmp_path / "private-state"
    public = site.prepare(plan, state)
    if changed == "request":
        deployment["profile_reference"] = "different"
    elif changed == "ca":
        Path(deployment["hermes_ca_file"]).write_text("changed")
    elif changed == "env":
        (Path(deployment["gateway_root"]) / ".env").write_text("changed")
    elif changed == "target":
        plan.changes[1].path.write_text("operator edit")
    else:
        (Path(public["manifest"]).parent / "0.after").write_text("damaged backup")
    with pytest.raises(assets.EnablementError):
        restored, _ = site._restore_plan(deployment, state, public["plan_sha256"])
        assets.apply_plan(restored, state, expected_plan_sha256=public["plan_sha256"])


def test_compose_duplicate_explicit_key_rejected(deployment):
    path = Path(deployment["gateway_root"]) / "docker-compose.prod.yml"
    path.write_text("services: {}\nservices: {}\n")
    with pytest.raises(assets.EnablementError, match="duplicate_compose_key"):
        site.plan_compose(path, deployment)


def test_long_form_signing_env_reference_is_not_silently_inherited(deployment):
    path = Path(deployment["gateway_root"]) / "docker-compose.prod.yml"
    config = yaml.safe_load(path.read_bytes())
    config["services"]["delivery-worker"]["env_file"] = [
        {"path": "./secrets/artifact-return.env", "required": True}
    ]
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(assets.EnablementError, match="site_already_has_signing_reference"):
        site.plan_compose(path, deployment)


def test_larger_existing_global_request_limit_requires_review(deployment, monkeypatch):
    mock_ingress(monkeypatch)
    path = Path(deployment["gateway_root"]) / "config/production.yaml"
    config = yaml.safe_load(path.read_bytes())
    config["api"]["max_request_body_bytes"] = site.LIMIT * 2
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(assets.EnablementError, match="larger_global_body_limit"):
        site.plan_server(deployment)


def test_private_compose_validation_preserves_original_references_and_overrides(
    deployment, tmp_path, monkeypatch
):
    mock_ingress(monkeypatch)
    path = Path(deployment["gateway_root"]) / "docker-compose.prod.yml"
    original = yaml.safe_load(path.read_bytes())
    sequence = [
        "${CF_GATEWAY_ENV_FILE:-.env}",
        {"path": "./optional.env", "required": False},
        {"path": "${LAST_ENV:?required}", "required": True},
    ]
    original["services"]["gateway"]["env_file"] = sequence
    original["services"]["isolated-extra"] = {
        "image": "kept",
        "profiles": ["unrelated"],
        "env_file": "${EXTRA_ENV:-extra.env}",
    }
    path.write_text(yaml.safe_dump(original, sort_keys=False))
    original_bytes = path.read_bytes()
    plan = site.plan_server(deployment)
    state = tmp_path / "private-state"
    prepared = site.prepare(plan, state)
    result = site.prepare_compose_validation(deployment, state, prepared["plan_sha256"])
    assert result["purpose"] == "compose_validation_only_not_deployment_assets"
    assert result["production_changed"] is False
    assert result["plan_sha256"] == prepared["plan_sha256"]
    probe = yaml.safe_load(Path(result["reference_candidate"]).read_bytes())
    resolved = yaml.safe_load(Path(result["validation_candidate"]).read_bytes())
    candidate = yaml.safe_load(plan.changes[1].after)
    assert probe["x-runtime"] == candidate["x-runtime"]
    assert probe[site.VALIDATION_ENV]["gateway"] == [
        *sequence,
        deployment["signing_env_file"],
    ]
    assert probe[site.VALIDATION_ENV]["isolated-extra"] == ["${EXTRA_ENV:-extra.env}"]
    for name, service in candidate["services"].items():
        assert probe["services"][name]["env_file"] == []
        assert site._env_files(resolved["services"][name]) == site._env_files(
            original["services"][name]
        )
        restored = dict(resolved["services"][name])
        if name in {"gateway", "dispatch-worker"}:
            restored["env_file"] = service["env_file"]
        assert restored == service
    assert site.VALIDATION_ENV not in resolved
    assert path.read_bytes() == original_bytes
    assert not Path(deployment["signing_env_file"]).exists()
    assert "synthetic-original-keep-private" not in json.dumps(result)
    assert all(
        Path(result[key]).parent == Path(prepared["manifest"]).parent
        for key in ("reference_candidate", "validation_candidate")
    )
    assert site.prepare_compose_validation(deployment, state, prepared["plan_sha256"]) == result


@pytest.mark.parametrize("changed", ["original", "reviewed-candidate", "validation-copy"])
def test_private_compose_validation_cannot_replace_existing_evidence(
    deployment, tmp_path, monkeypatch, changed
):
    mock_ingress(monkeypatch)
    plan = site.plan_server(deployment)
    state = tmp_path / "private-state"
    prepared = site.prepare(plan, state)
    result = site.prepare_compose_validation(deployment, state, prepared["plan_sha256"])
    if changed == "original":
        path = Path(deployment["gateway_root"]) / "docker-compose.prod.yml"
        expected = "asset_changed_since_plan"
    elif changed == "reviewed-candidate":
        path = Path(prepared["manifest"]).parent / "1.after"
        expected = "resume_backup_integrity_failed"
    else:
        path = Path(result["validation_candidate"])
        expected = "compose_validation_candidate_conflict"
    path.write_bytes(b"operator or interrupted write; preserve this evidence\n")
    with pytest.raises(assets.EnablementError, match=expected):
        site.prepare_compose_validation(deployment, state, prepared["plan_sha256"])
    assert path.read_bytes() == b"operator or interrupted write; preserve this evidence\n"


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ("include", "compose_include_requires_review"),
        ("extends", "compose_extends_requires_review"),
        ("extension", "compose_validation_extension_conflict"),
        ("required-string", "compose_env_shape_unsupported"),
        ("unknown-env-key", "compose_env_shape_unsupported"),
        ("format-list", "compose_env_shape_unsupported"),
    ],
)
def test_unprovable_compose_validation_shapes_fail_specifically(deployment, fault, error):
    path = Path(deployment["gateway_root"]) / "docker-compose.prod.yml"
    config = yaml.safe_load(path.read_bytes())
    if fault == "include":
        config["include"] = ["uninspected-compose.yaml"]
    elif fault == "extends":
        config["services"]["gateway"]["extends"] = {"file": "other.yaml", "service": "api"}
    elif fault == "extension":
        config[site.VALIDATION_ENV] = {}
    else:
        entry = {"path": "./original.env"}
        entry.update(
            {
                "required-string": {"required": "${REQUIRED}"},
                "unknown-env-key": {"unknown": "unprovable"},
                "format-list": {"format": ["raw"]},
            }[fault]
        )
        config["services"]["gateway"]["env_file"] = [entry]
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(assets.EnablementError, match=error):
        site.plan_compose(path, deployment)


def test_compose_standard_merge_precedence_preserved_and_duplicate_merge_rejected(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text(
        "x-first: &first {env_file: first.env, image: first}\n"
        "x-second: &second {env_file: second.env, image: second}\n"
        "services:\n  test:\n    <<: [*first, *second]\n    image: explicit\n"
    )
    _, parsed = site._yaml(path)
    assert parsed["services"]["test"] == {"env_file": "first.env", "image": "explicit"}
    path.write_text(
        "x-first: &first {env_file: first.env}\n"
        "x-second: &second {env_file: second.env}\n"
        "services:\n  test:\n    <<: *first\n    <<: *second\n"
    )
    with pytest.raises(assets.EnablementError, match="duplicate_compose_merge_key"):
        site._yaml(path)
