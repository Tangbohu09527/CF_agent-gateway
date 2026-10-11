"""Real pinned Compose config execution, without a Docker daemon or site access.

CI supplies checksum-verified standalone binaries in a private runner directory.
Only the Nginx invocation and Docker image boundary are substituted; candidate
generation, protected journals, host validation and Compose itself execute.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from cf_agent_gateway.hermes.return_bridge import enablement as assets
from cf_agent_gateway.hermes.return_bridge import site_server as site

VERSIONS = ("2.26.1", "2.39.4")
CHECKSUMS = {
    "linux": {
        "2.26.1": "2f61856d1b8c9de29ffdaedaa1c6d0a5fc5c79da45068f1f4310feed8d3a3f61",
        "2.39.4": "7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4",
    },
    "windows": {
        "2.26.1": "d8a386d375ef26a77be0bee97516b0287d93acafb3976806f42e2b76c6904125",
        "2.39.4": "6b3bccfabcdd172e1d9e15d011b54c9b5b13b93b1153148108f55e4349055955",
    },
}


def private_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


@pytest.fixture(params=VERSIONS)
def compose(request):
    directory = os.environ.get("CF_RETURN_COMPOSE_DIRECTORY")
    if not directory:
        if os.environ.get("CF_RETURN_REQUIRE_COMPOSE") == "1":
            pytest.fail("checksum-verified official Compose binaries are required")
        pytest.skip("set CF_RETURN_COMPOSE_DIRECTORY for real pinned Compose validation")
    platform = "windows" if os.name == "nt" else "linux"
    suffix = ".exe" if os.name == "nt" else ""
    binary = Path(directory) / f"docker-compose-v{request.param}{suffix}"
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == CHECKSUMS[platform][request.param]
    return binary, request.param


@pytest.fixture
def exercise(tmp_path, compose, monkeypatch):
    binary, version = compose
    source = Path(__file__).parents[1] / "deploy/prepare-return-server.py"
    spec = importlib.util.spec_from_file_location("compose_real_server_entry", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, nginx, release, state = [
        tmp_path / part for part in ("gateway", "nginx", "release", "state")
    ]
    for directory in (root, nginx, release, state):
        directory.mkdir(mode=0o700)
    request = {
        "schema": site.SCHEMA,
        "release_commit": "a" * 40,
        "candidate_image": "sha256:" + "b" * 64,
        "baseline_image": "sha256:" + "c" * 64,
        "gateway_root": str(root),
        "nginx_root": str(nginx),
        "hermes_origin": "https://hermes.example.invalid:8643",
        "hermes_ca_file": str(private_file(tmp_path / "hermes-ca.pem", b"not used by compose")),
        "gateway_origin": "https://gateway.example.invalid:18444",
        "gateway_ca_file": str(private_file(tmp_path / "gateway-ca.pem", b"not used by compose")),
        "profile_reference": "isolated-profile",
        "profile_revision": 1,
        "signing_env_file": str(root / "secrets/artifact-return.env"),
        "release_root": str(release),
        "nginx_image": "sha256:" + "d" * 64,
        "state_directory": str(state),
        "deployment_id": "real-compose-offline-validation",
        "gateway_health_url": "http://127.0.0.1:18999",
        "peer_state_file": str(tmp_path / "peer.json"),
    }
    compose_path = root / "docker-compose.prod.yml"
    private_file(
        compose_path,
        (
            b"name: cf-compose-validation\n"
            b"x-runtime: &runtime\n"
            b"  image: ${CF_GATEWAY_IMAGE:?image required}\n"
            b'  user: "10001:10001"\n'
            b"  read_only: true\n"
            b"  cap_drop: [ALL]\n"
            b"  env_file: &files\n"
            b"    - ${ORIGINAL_ENV_PATH:?path required}\n"
            b"    - path: ./override.env\n"
            b"      required: true\n"
            b"    - path: ./optional-absent.env\n"
            b"      required: false\n"
            b"  environment:\n"
            b"    EXPLICIT_OVERRIDE: ${SELECTED:?selection required}\n"
            b"  volumes: [gateway-state:/var/lib/cf-agent-gateway]\n"
            b"services:\n"
            b"  gateway: {<<: *runtime}\n"
            b"  worker: {<<: *runtime, profiles: [worker]}\n"
            b"  dispatch-worker: {<<: *runtime, profiles: [worker]}\n"
            b"  delivery-worker: {<<: *runtime, profiles: [worker]}\n"
            b"  maintenance: {<<: *runtime, profiles: [maintenance]}\n"
            b"volumes: {gateway-state: {}}\n"
        ),
    )
    private_file(
        root / ".env",
        (
            f"CF_GATEWAY_IMAGE={request['baseline_image']}\n"
            "ORIGINAL_ENV_PATH=./base.env\nSELECTED=explicit-selection\n"
        ).encode(),
    )
    private_file(root / "base.env", b"ORDERED_OVERRIDE=first\nEXPLICIT_OVERRIDE=file-value\n")
    private_file(root / "override.env", b"ORDERED_OVERRIDE=last\n")
    nginx_path = private_file(nginx / "nginx.conf", b"http {}\n")
    request_path = private_file(tmp_path / "request.json", json.dumps(request).encode())
    entry = module.ServerEntry(
        request_path,
        release_commit=request["release_commit"],
        candidate_image=request["candidate_image"],
    )
    calls = []
    outputs = []

    def execute(arguments, *, cwd=None, timeout=60):
        # Keep test interpolation controlled; no shell/global credentials are
        # inherited by the actual Compose process. Config needs no daemon.
        environment = {
            key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ
        }
        return subprocess.run(
            [str(binary), *arguments],
            cwd=cwd or root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def docker(arguments, **kwargs):
        assert arguments[0] == "compose", "test must not execute a Docker operation"
        calls.append(list(arguments[1:]))
        result = execute(arguments[1:], **kwargs)
        if result.returncode:
            raise module.EntryError("command_failed")
        if "--format" in arguments:
            outputs.append(json.loads(result.stdout))
        return result.stdout.strip()

    def image_assets(action, *, plan_sha=None, **_):
        assert action == "compose-validation"
        return site.prepare_compose_validation(request, entry.asset_dir, plan_sha)

    monkeypatch.setattr(entry, "docker", docker)
    monkeypatch.setattr(entry, "assets", image_assets)
    nginx_checks = []
    monkeypatch.setattr(entry, "nginx_test", lambda **kw: nginx_checks.append(kw))

    def prepare():
        plan = assets.Plan(
            "return-site-server",
            (
                site.plan_compose(compose_path, request),
                assets.Change(nginx_path, ("test-ingress",), nginx_path.read_bytes(), b"http {}\n"),
            ),
            (),
        )
        return site.prepare(
            replace(plan, request_sha256=site._request_hash(request)), entry.asset_dir
        )

    return {
        "entry": entry,
        "module": module,
        "request": request,
        "root": root,
        "compose_path": compose_path,
        "prepare": prepare,
        "execute": execute,
        "calls": calls,
        "outputs": outputs,
        "nginx_checks": nginx_checks,
        "version": version,
        "binary": binary,
    }


def test_pinned_compose_original_failure_and_preserved_candidate_semantics(exercise, tmp_path):
    entry = exercise["entry"]
    result = exercise["prepare"]()
    original = {path: path.read_bytes() for path in exercise["root"].rglob("*") if path.is_file()}
    arguments = [
        "--project-directory",
        str(exercise["root"]),
        "--env-file",
        str(exercise["root"] / ".env"),
        "--file",
        result["candidates"][0]["candidate_file"],
        "--profile",
        "*",
        "config",
        "--no-env-resolution",
        "--format",
        "json",
    ]
    prior = exercise["execute"](arguments)
    if exercise["version"] == "2.26.1":
        assert prior.returncode != 0 and "unknown flag: --no-env-resolution" in prior.stderr
    else:
        # Even a release with the flag checks that required env_file paths
        # exist. Planning precedes signing-file creation on both releases.
        assert prior.returncode != 0 and "artifact-return.env" in prior.stderr
    proof = entry.validate_candidates(result)
    assert proof["version"] == exercise["version"]
    assert proof["original_env_files_resolved"] is True
    assert proof["all_profiles_checked"] is True
    assert proof["references"] == "interpolated_reference_probe"
    assert proof["original_environment_equal"] is True
    assert proof["native_no_env_resolution_checked"] is (exercise["version"] == "2.39.4")
    resolved = next(
        value
        for value in reversed(exercise["outputs"])
        if all(
            value["services"][name]["image"] == exercise["request"]["candidate_image"]
            for name in site.APPS
        )
    )
    assert set(resolved["services"]) == {*site.APPS, "maintenance"}
    for name, service in resolved["services"].items():
        assert service["environment"]["ORDERED_OVERRIDE"] == "last"
        assert service["environment"]["EXPLICIT_OVERRIDE"] == "explicit-selection"
        assert site.SIGNING_ENV not in service["environment"]
        if name in site.APPS:
            assert service["image"] == exercise["request"]["candidate_image"]
    assert exercise["nginx_checks"]
    assert all(path.read_bytes() == before for path, before in original.items())
    assert not Path(exercise["request"]["signing_env_file"]).exists()
    help_checks = {
        "up": ("--no-start", "--no-deps", "--no-build", "--pull", "--detach", "--force-recreate"),
        "ps": ("--all", "--quiet"),
        "exec": ("-T",),
    }
    for verb, flags in help_checks.items():
        help_result = exercise["execute"]([verb, "--help"])
        assert help_result.returncode == 0
        assert all(
            re.search(r"(?m)^\s+(?:-\w, )?" + re.escape(flag) + r"(?:\s|,|$)", help_result.stdout)
            for flag in flags
        )
    create = exercise["execute"](["create", "--help"])
    assert "--no-deps" not in create.stdout
    evidence = {
        "scope": "isolated actual Compose config; no daemon/site/model/WeChat operations",
        "version": proof["version"],
        "binary_sha256": hashlib.sha256(exercise["binary"].read_bytes()).hexdigest(),
        "original_command_failed_before_signing_file_exists": prior.returncode != 0,
        "original_failure": (
            "unknown_no_env_resolution_flag"
            if exercise["version"] == "2.26.1"
            else "required_signing_env_file_not_created_during_plan"
        ),
        "validation": proof,
        "ordered_and_explicit_overrides_preserved": True,
        "long_optional_env_file_supported": True,
        "start_flags_checked_against_real_help": True,
        "production_changed": False,
        "nginx_receiver": "substitute; separately tested in existing real Nginx regression",
    }
    (tmp_path / "compose-compatibility-evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )


@pytest.mark.parametrize(
    "fault",
    (
        "missing-required-env",
        "invalid-compose",
        "variable-signing",
        "duplicate-path",
        "duplicate-yaml",
        "signing-poll",
        "signing-delivery",
        "signing-hidden-profile",
        "original-signing-value",
        "wrong-candidate-image",
        "variable-missing",
        "environment-yaml-ambiguity",
        "tilde-env-path",
        "extension-signing-environment",
    ),
)
def test_real_compose_rejects_unsafe_or_unequivalent_inputs(exercise, fault):
    root, compose_path = exercise["root"], exercise["compose_path"]
    request, entry = exercise["request"], exercise["entry"]
    if fault == "missing-required-env":
        (root / "base.env").unlink()
    elif fault == "variable-missing":
        (root / ".env").write_text(
            f"CF_GATEWAY_IMAGE={request['baseline_image']}\nSELECTED=selection\n"
        )
    elif fault == "variable-signing":
        (root / ".env").write_text(
            f"CF_GATEWAY_IMAGE={request['baseline_image']}\nSELECTED=selection\n"
            "ORIGINAL_ENV_PATH=./secrets/artifact-return.env\n"
        )
    elif fault == "original-signing-value":
        (root / "override.env").write_text(f"{site.SIGNING_ENV}=synthetic-unapproved-test-value\n")
    elif fault == "duplicate-yaml":
        compose_path.write_text(compose_path.read_text() + "services: {}\n")
    elif fault == "environment-yaml-ambiguity":
        compose_path.write_text(
            compose_path.read_text().replace("${SELECTED:?selection required}", "yes")
        )
    else:
        value = yaml.safe_load(compose_path.read_bytes())
        if fault == "invalid-compose":
            value["services"]["maintenance"]["unrecognized_property"] = True
        elif fault == "duplicate-path":
            value["services"]["worker"]["env_file"] = ["base.env", "./base.env"]
        elif fault == "tilde-env-path":
            value["services"]["worker"]["env_file"] = ["~/private.env"]
        elif fault == "extension-signing-environment":
            value["x-runtime"]["environment"] = [f"{site.SIGNING_ENV}=synthetic-unapproved-value"]
        elif fault.startswith("signing-"):
            name = {
                "signing-poll": "worker",
                "signing-delivery": "delivery-worker",
                "signing-hidden-profile": "maintenance",
            }[fault]
            value["services"][name]["env_file"] = [request["signing_env_file"]]
        compose_path.write_text(yaml.safe_dump(value, sort_keys=False))
    with pytest.raises(
        (assets.EnablementError, exercise["module"].EntryError),
        match="candidate_compose_environment_changed"
        if fault == "environment-yaml-ambiguity"
        else None,
    ):
        result = exercise["prepare"]()
        if fault == "wrong-candidate-image":
            candidate = Path(result["candidates"][0]["candidate_file"])
            candidate.write_bytes(
                candidate.read_bytes().replace(
                    request["candidate_image"].encode(), request["baseline_image"].encode()
                )
            )
        entry.validate_candidates(result)
    assert not exercise["nginx_checks"]
    assert not Path(request["signing_env_file"]).exists()
    if fault in {"missing-required-env", "invalid-compose", "variable-missing"}:
        # A config failure propagates; capability selection is not retried via
        # --no-interpolate, a different binary, or a weaker validation path.
        assert not any("--no-interpolate" in call for call in exercise["calls"])
        assert sum("--help" in call for call in exercise["calls"]) == 1


def existing_external_reference(exercise):
    """An existing restricted host-binding file, outside the Gateway tree."""
    root = exercise["root"]
    external = private_file(
        root.parent / "etc/cf-gateway-host-binding/runtime.env",
        b"ORDERED_OVERRIDE=host-binding-last\nEXTERNAL_SYNTHETIC=existing-value\n",
    )
    with (root / ".env").open("a", encoding="utf-8") as stream:
        stream.write(f"HOST_BINDING_ENV={external.as_posix()}\n")
    value = yaml.safe_load(exercise["compose_path"].read_bytes())
    for name in ("gateway", "dispatch-worker"):
        value["services"][name]["env_file"] = [
            *value["services"][name]["env_file"],
            {"path": "${HOST_BINDING_ENV:?host binding required}", "required": True},
        ]
    # This reproduces the deployed heartbeat-init override and also exercises
    # the different empty-extension JSON representations in the real versions.
    value["services"]["heartbeat-init"] = {
        "image": exercise["request"]["baseline_image"],
        "env_file": [],
        "environment": {},
    }
    exercise["compose_path"].write_text(yaml.safe_dump(value, sort_keys=False))
    return external


def test_existing_restricted_external_reference_is_preserved_and_frozen(exercise, tmp_path):
    external = existing_external_reference(exercise)
    protected = {
        path: path.read_bytes()
        for path in (exercise["compose_path"], exercise["root"] / ".env", external)
    }
    result = exercise["prepare"]()
    report = exercise["entry"].validate_candidates(result)
    assert report["original_environment_equal"] is True
    assert report["original_env_files_resolved"] is True
    assert len(report["proof_sha256"]) == 64
    proof_path = Path(report["proof_file"])
    assert proof_path.is_relative_to(exercise["entry"].asset_dir)
    proof_before = proof_path.read_bytes()
    assert b"existing-value" not in proof_before
    assert b"host-binding-last" not in proof_before
    resolved_models = 0
    for model in exercise["outputs"]:
        services = model.get("services", {})
        if "EXTERNAL_SYNTHETIC" not in services.get("gateway", {}).get("environment", {}):
            continue
        resolved_models += 1
        for name in ("gateway", "dispatch-worker"):
            assert services[name]["environment"]["ORDERED_OVERRIDE"] == "host-binding-last"
            assert services[name]["environment"]["EXPLICIT_OVERRIDE"] == "explicit-selection"
        for name in ("worker", "delivery-worker", "maintenance", "heartbeat-init"):
            assert "EXTERNAL_SYNTHETIC" not in services[name].get("environment", {})
    assert resolved_models >= 2  # Actual original and candidate config both loaded the file.
    assert all(path.read_bytes() == before for path, before in protected.items())
    assert not Path(exercise["request"]["signing_env_file"]).exists()
    exercise["entry"].state = {"plan_sha256": result["plan_sha256"], "compose_validation": report}
    exercise["entry"].verify_compose_proof()
    repeated = exercise["entry"].validate_candidates(result)
    assert repeated["proof_sha256"] == report["proof_sha256"]
    assert proof_path.read_bytes() == proof_before
    (tmp_path / "compose-compatibility-evidence.json").write_text(
        json.dumps(
            {
                "scope": "synthetic existing external env_file; actual Compose; no site access",
                "version": exercise["version"],
                "external_reference_scope": ["gateway", "dispatch-worker"],
                "external_reference_was_in_live_and_protected_original": True,
                "validation": report,
                "production_files_unchanged": True,
                "original_values_not_in_proof": True,
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.parametrize(
    "fault",
    (
        "missing",
        "optional-missing",
        "world-readable",
        "symlink",
        "parent-symlink",
        "hardlink",
        "parent-traversal",
    ),
)
def test_real_compose_rejects_unsafe_original_external_file(exercise, fault):
    if os.name == "nt" and fault in {"world-readable", "symlink", "parent-symlink"}:
        pytest.skip("POSIX ownership/mode and symlink checks execute in required Linux CI")
    external = existing_external_reference(exercise)
    if fault in {"missing", "optional-missing"}:
        external.unlink()
        if fault == "optional-missing":
            value = yaml.safe_load(exercise["compose_path"].read_bytes())
            for name in ("gateway", "dispatch-worker"):
                value["services"][name]["env_file"][-1]["required"] = False
            exercise["compose_path"].write_text(yaml.safe_dump(value, sort_keys=False))
    elif fault == "world-readable":
        external.chmod(0o644)
    elif fault == "symlink":
        target = external.with_name("synthetic-target.env")
        external.rename(target)
        external.symlink_to(target)
    elif fault == "parent-symlink":
        directory = external.parent
        destination = directory.with_name("existing-target")
        directory.rename(destination)
        directory.symlink_to(destination, target_is_directory=True)
    elif fault == "hardlink":
        os.link(external, external.with_name("synthetic-alias.env"))
    elif fault == "parent-traversal":
        with (exercise["root"] / ".env").open("a", encoding="utf-8") as stream:
            stream.write(f"HOST_BINDING_ENV={external.parent.as_posix()}/../runtime.env\n")
    expected_error = {
        "missing": "compose_environment_file_missing",
        "optional-missing": "compose_environment_file_missing",
        "world-readable": "external_env_file_permissions_invalid",
        "symlink": "linked_path",
        "parent-symlink": "linked_path",
        "hardlink": "not_private_regular_file",
        "parent-traversal": "compose_env_path_not_normalized",
    }[fault]
    with pytest.raises(
        (assets.EnablementError, exercise["module"].EntryError), match=expected_error
    ):
        exercise["entry"].validate_candidates(exercise["prepare"]())
    assert not exercise["nginx_checks"]
    assert not Path(exercise["request"]["signing_env_file"]).exists()


@pytest.mark.parametrize("fault", ("content", "inode", "mode", "original-compose"))
def test_real_compose_external_proof_rejects_change_without_overwriting_journal(exercise, fault):
    if fault == "mode" and os.name == "nt":
        pytest.skip("POSIX mode checks execute in required Linux CI")
    external = existing_external_reference(exercise)
    result = exercise["prepare"]()
    report = exercise["entry"].validate_candidates(result)
    exercise["entry"].state = {"plan_sha256": result["plan_sha256"], "compose_validation": report}
    preserved = {
        path: path.read_bytes()
        for path in Path(result["manifest"]).parent.rglob("*")
        if path.is_file()
    }
    if fault == "content":
        external.write_bytes(b"CHANGED_SYNTHETIC=changed\n")
    elif fault == "inode":
        replacement = private_file(external.with_name("replacement.env"), external.read_bytes())
        os.replace(replacement, external)
    elif fault == "mode":
        external.chmod(0o640)
    else:
        exercise["compose_path"].write_bytes(exercise["compose_path"].read_bytes() + b"\n")
    with pytest.raises(
        exercise["module"].EntryError,
        match="asset_changed_since_plan"
        if fault == "original-compose"
        else "compose_environment_file_changed",
    ):
        exercise["entry"].verify_compose_proof()
    with pytest.raises((assets.EnablementError, exercise["module"].EntryError)):
        exercise["entry"].validate_candidates(result)
    assert all(path.read_bytes() == before for path, before in preserved.items())


@pytest.mark.parametrize(
    "fault", ("unknown-path", "poll", "delivery", "hidden-profile", "order", "required", "format")
)
def test_real_compose_candidate_cannot_expand_original_external_scope(exercise, fault):
    external = existing_external_reference(exercise)
    result = exercise["prepare"]()
    candidate = Path(result["candidates"][0]["candidate_file"])
    protected_original = candidate.with_suffix(".before").read_bytes()
    value = yaml.safe_load(candidate.read_bytes())
    if fault == "unknown-path":
        unknown = private_file(external.parent / "unknown.env", b"UNKNOWN_SYNTHETIC=value\n")
        value["services"]["gateway"]["env_file"].insert(0, str(unknown))
    elif fault == "order":
        files = value["services"]["gateway"]["env_file"]
        files[0], files[-2] = files[-2], files[0]
    elif fault in {"required", "format"}:
        reference = value["services"]["gateway"]["env_file"][-2]
        reference[fault] = False if fault == "required" else "raw"
    else:
        name = {"poll": "worker", "delivery": "delivery-worker", "hidden-profile": "maintenance"}[
            fault
        ]
        value["services"][name]["env_file"] = [
            *value["services"][name]["env_file"],
            str(external),
        ]
    candidate.write_text(yaml.safe_dump(value, sort_keys=False))
    failed_candidate = candidate.read_bytes()
    with pytest.raises((assets.EnablementError, exercise["module"].EntryError)):
        exercise["entry"].validate_candidates(result)
    assert candidate.with_suffix(".before").read_bytes() == protected_original
    assert candidate.read_bytes() == failed_candidate
    assert not exercise["nginx_checks"]


@pytest.mark.parametrize("shape", ("empty", "absent", "short", "long", "extension-empty"))
def test_real_compose_empty_and_supported_env_shapes_keep_original_semantics(exercise, shape):
    path = exercise["compose_path"]
    value = yaml.safe_load(path.read_bytes())
    service = {"image": exercise["request"]["baseline_image"]}
    if shape in {"empty", "extension-empty"}:
        service["env_file"] = []
    elif shape == "short":
        service["env_file"] = "./base.env"
    elif shape == "long":
        service["env_file"] = [{"path": "./base.env", "required": True}]
    value["services"]["heartbeat-init"] = service
    if shape == "extension-empty":
        value["x-offline-notes"] = {"env_file": []}
        service["x-offline-notes"] = {"env_file": []}
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    result = exercise["prepare"]()
    report = exercise["entry"].validate_candidates(result)
    assert report["original_environment_equal"] is True
    probe = next(
        model[site.VALIDATION_ENV]["heartbeat-init"]
        for model in exercise["outputs"]
        if site.VALIDATION_ENV in model
    )
    if shape in {"empty", "absent", "extension-empty"}:
        assert probe == (None if exercise["version"] == "2.39.4" else [])
    elif shape == "short":
        # The source helper deliberately turns the one short reference into a
        # list before constructing the probe, preserving the same meaning.
        assert probe == ["./base.env"]
    else:
        assert probe == [{"path": "./base.env", "required": True}]


@pytest.mark.parametrize(
    "invalid",
    (
        None,
        False,
        7,
        {"path": "./base.env"},
        [{"path": "./base.env", "x-extra": 1}],
        [{"path": "./base.env", "required": "yes"}],
        [{"path": "./base.env", "format": False}],
    ),
)
def test_real_compose_invalid_env_shape_is_rejected_with_safe_location(exercise, invalid):
    path = exercise["compose_path"]
    value = yaml.safe_load(path.read_bytes())
    value["services"]["heartbeat-init"] = {
        "image": exercise["request"]["baseline_image"],
        "env_file": copy.deepcopy(invalid),
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    actual = exercise["execute"](
        [
            "--file",
            str(path),
            "--env-file",
            str(exercise["root"] / ".env"),
            "config",
            "--format",
            "json",
        ]
    )
    if exercise["version"] == "2.39.4" and invalid == [{"path": "./base.env", "required": "yes"}]:
        # This release coerces the YAML 1.1-looking string with a warning. The
        # source validator still rejects ambiguity instead of treating it as
        # a proven boolean option (or an empty reference).
        assert actual.returncode == 0 and "YAML 1.2" in actual.stderr
    else:
        assert actual.returncode != 0
    with pytest.raises((assets.EnablementError, exercise["module"].EntryError)) as caught:
        exercise["entry"].validate_candidates(exercise["prepare"]())
    assert "heartbeat-init" in str(caught.value) or "heartbeat-init" in str(
        getattr(caught.value, "details", {})
    )
    assert "env_file" in str(caught.value) or "env_file" in str(
        getattr(caught.value, "details", {})
    )
    assert getattr(caught.value, "details", {}).get("type")
    assert not exercise["nginx_checks"]


def test_real_compose_raw_format_is_preserved_or_rejected_by_actual_capability(exercise):
    external = existing_external_reference(exercise)
    external.write_bytes(b"QUOTED_SYNTHETIC='keep these quotes'\nDOLLAR_SYNTHETIC=$UNSET_LITERAL\n")
    path = exercise["compose_path"]
    value = yaml.safe_load(path.read_bytes())
    for name in ("gateway", "dispatch-worker"):
        value["services"][name]["env_file"][-1]["format"] = "raw"
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    result = exercise["prepare"]()
    if exercise["version"] == "2.26.1":
        with pytest.raises(exercise["module"].EntryError, match="command_failed"):
            exercise["entry"].validate_candidates(result)
        assert not exercise["nginx_checks"]
        assert not any("--no-interpolate" in call for call in exercise["calls"])
    else:
        report = exercise["entry"].validate_candidates(result)
        assert report["original_environment_equal"] is True
        observed = [
            model["services"]["gateway"]["environment"]
            for model in exercise["outputs"]
            if "QUOTED_SYNTHETIC"
            in model.get("services", {}).get("gateway", {}).get("environment", {})
        ]
        assert len(observed) >= 2
        assert all(env["QUOTED_SYNTHETIC"] == "'keep these quotes'" for env in observed)
        assert all(env["DOLLAR_SYNTHETIC"] == "$$UNSET_LITERAL" for env in observed)
