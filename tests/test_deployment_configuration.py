from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

SOURCE = Path(__file__).parents[1] / "deploy" / "continuation_config.py"
spec = importlib.util.spec_from_file_location("deployment_configuration", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def observations(path="/opt/test-gateway/config/production.yaml"):
    project = "/opt/test-gateway"
    files = [project + "/docker-compose.prod.yml"]
    rendered = {"services": {}}
    containers = []
    for name in module.SERVICES:
        rendered["services"][name] = {
            "volumes": [
                {"type": "bind", "source": path, "target": module.TARGET, "read_only": True}
            ]
        }
        containers.append(
            {
                "Config": {
                    "Labels": {
                        "com.docker.compose.service": name,
                        "com.docker.compose.project.working_dir": project,
                        "com.docker.compose.project.config_files": files[0],
                    }
                },
                "Mounts": [
                    {"Type": "bind", "Source": path, "Destination": module.TARGET, "RW": False}
                ],
            }
        )
    return (
        rendered,
        containers,
        {"project_directory": project, "compose_files": files, "approved_source": {"path": path}},
    )


@pytest.mark.parametrize("name", ["production.yaml", "production.local.yaml", "custom.json"])
def test_actual_formal_selection_not_default_path(name):
    path = "/opt/test-gateway/config/" + name
    rendered, containers, arguments = observations(path)
    assert module.select_configuration(rendered, containers, **arguments) == path


@pytest.mark.parametrize(
    "drift", ["render", "mount", "readwrite", "working_dir", "compose_files", "approval"]
)
def test_selection_needs_every_independent_observation(drift):
    rendered, containers, arguments = observations()
    if drift == "render":
        rendered["services"]["gateway"]["volumes"][0]["source"] = (
            "/opt/test-gateway/config/other.yaml"
        )
    elif drift == "mount":
        containers[0]["Mounts"][0]["Source"] = "/opt/test-gateway/config/other.yaml"
    elif drift == "readwrite":
        containers[0]["Mounts"][0]["RW"] = True
    elif drift == "working_dir":
        containers[0]["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/opt/other"
    elif drift == "compose_files":
        containers[0]["Config"]["Labels"]["com.docker.compose.project.config_files"] += (
            ",/tmp/extra.yml"
        )
    else:
        arguments["approved_source"]["path"] = "/opt/test-gateway/config/other.yaml"
    with pytest.raises(module.ConfigurationProofError):
        module.select_configuration(rendered, containers, **arguments)


def approved_data():
    return {
        "path": "/opt/test-gateway/config/production.yaml",
        "uid": 0,
        "gid": 0,
        "mode": 0o644,
        "nlink": 1,
        "size": 10,
        "sha256": "a" * 64,
        "ancestors_safe": True,
        "acl_absent": True,
        "regular_no_symlink": True,
    }


def test_audited_config_0644_is_data_without_weakening_protection():
    observed = approved_data()
    module.verify_configuration_data(observed, copy.deepcopy(observed))
    for key, value in (
        ("sha256", "b" * 64),
        ("mode", 0o600),
        ("uid", 1000),
        ("nlink", 2),
        ("acl_absent", False),
    ):
        changed = {**observed, key: value}
        with pytest.raises(module.ConfigurationProofError):
            module.verify_configuration_data(changed, observed)


@pytest.mark.parametrize("change", ["path", "required", "format", "order", "unprotected"])
def test_external_environment_references_stay_protected_and_exact(change):
    before = {
        "services.gateway.env_file": [
            {"path": "/etc/test-gateway/runtime.env", "required": True, "format": "raw"},
            {"path": "/opt/test-gateway/secrets/return.env", "required": True},
        ]
    }
    paths = {x["path"] for x in before["services.gateway.env_file"]}
    module.require_preserved_references(before, copy.deepcopy(before), paths)
    after = copy.deepcopy(before)
    if change == "order":
        after["services.gateway.env_file"].reverse()
    elif change == "unprotected":
        paths.remove("/etc/test-gateway/runtime.env")
    else:
        after["services.gateway.env_file"][0][change] = {
            "path": "/tmp/unreviewed.env",
            "required": False,
            "format": "unknown",
        }[change]
    with pytest.raises(module.ConfigurationProofError):
        module.require_preserved_references(before, after, paths)


@pytest.mark.parametrize("style", ["yaml", "crlf", "json", "flow"])
def test_exact_semantic_two_key_delta(style):
    data = {
        "other": {"keep": [1, "value", False]},
        "host_binding": {
            "enabled": True,
            "legacy_runtime_confirmed": False,
            "runtime_model": "local",
        },
        "tail": "keep",
    }
    if style == "json":
        raw = json.dumps(data).encode()
    elif style == "flow":
        raw = yaml.safe_dump(data, default_flow_style=True, sort_keys=False).encode()
    else:
        raw = ("# preserve comment\n" + yaml.safe_dump(data, sort_keys=False)).encode()
        if style == "crlf":
            raw = raw.replace(b"\n", b"\r\n")
    before = hashlib.sha256(raw).hexdigest()
    after = module.patch_configuration(raw, "approved-thread")
    expected = copy.deepcopy(data)
    expected["host_binding"].update(
        fresh_session_thread_ids=["approved-thread"], runtime_fallback_disabled_confirmed=True
    )
    assert yaml.safe_load(after) == expected
    assert hashlib.sha256(raw).hexdigest() == before
    if style in {"yaml", "crlf"}:
        ending = b"\r\n" if style == "crlf" else b"\n"
        insertion = (
            b'  fresh_session_thread_ids: ["approved-thread"]'
            + ending
            + b"  runtime_fallback_disabled_confirmed: true"
            + ending
        )
        assert after.replace(insertion, b"") == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"host_binding: {}\nhost_binding: {}\n",
        b'{"host_binding":{},"host_binding":{}}',
        (
            b"host_binding: &binding {enabled: true, legacy_runtime_confirmed: false}\n"
            b"other: *binding\n"
        ),
    ],
)
def test_duplicate_keys_and_cross_section_aliases_refuse(raw):
    with pytest.raises(module.ConfigurationProofError):
        module.patch_configuration(raw, "approved-thread")


def failed_prepare_observation():
    package = {"source.tar.gz", "inputs.json", "deploy.py", "manifest.json"}
    evidence = {"prepare-result.json", "prepare-running.json", "event-initial.json"}
    result = {
        "action": "prepare",
        "status": "stopped",
        "stage": "read_only_preflight",
        "facts": {},
        "error": {"code": "unsafe_file"},
    }
    return package, evidence, result


def test_exact_preserved_pre_extraction_failure_can_prepare_new_package():
    package, evidence, result = failed_prepare_observation()
    module.verify_failed_prepare_state(
        package,
        evidence,
        result,
        expected_package_names=set(package),
        expected_evidence_names=set(evidence),
    )


@pytest.mark.parametrize(
    "extra", ["source", "deployment-plan.json", "future-runtime-proof.json", "unknown"]
)
def test_partial_failure_unknown_or_advanced_package_refuses(extra):
    package, evidence, result = failed_prepare_observation()
    approved = set(package)
    package.add(extra)
    with pytest.raises(module.ConfigurationProofError):
        module.verify_failed_prepare_state(
            package,
            evidence,
            result,
            expected_package_names=approved,
            expected_evidence_names=evidence,
        )


@pytest.mark.parametrize("extra", ["quiesced.json", "activate-result.json", "unknown"])
def test_partial_failure_unknown_or_service_evidence_refuses(extra):
    package, evidence, result = failed_prepare_observation()
    approved = set(evidence)
    evidence.add(extra)
    with pytest.raises(module.ConfigurationProofError):
        module.verify_failed_prepare_state(
            package,
            evidence,
            result,
            expected_package_names=package,
            expected_evidence_names=approved,
        )


@pytest.mark.parametrize(
    "key,value", [("status", "complete"), ("stage", "image_build"), ("action", "activate")]
)
def test_partial_failure_result_is_specific_preflight_stop(key, value):
    package, evidence, result = failed_prepare_observation()
    result[key] = value
    with pytest.raises(module.ConfigurationProofError):
        module.verify_failed_prepare_state(
            package,
            evidence,
            result,
            expected_package_names=package,
            expected_evidence_names=evidence,
        )
