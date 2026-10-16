"""Pure validation of a deployment's effective configuration selection and delta.

No filesystem, Docker or credential access happens here. Callers supply protected
observations. Configuration data permissions never apply to executable or secret
inputs; those retain the caller's separate private-file policy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import PurePosixPath

SERVICES = ("gateway", "worker", "dispatch-worker", "delivery-worker")
DEPLOYMENT_IMAGE_SERVICES = (*SERVICES, "migration", "heartbeat-init")
TARGET = "/app/config/production.yaml"


class ConfigurationProofError(ValueError):
    """A fixed, content-free rejection code."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise ConfigurationProofError(code)


def normalized_path(value: object) -> PurePosixPath:
    require(isinstance(value, str) and len(value) <= 512, "path_invalid")
    path = PurePosixPath(value)
    require(path.is_absolute() and str(path) == value and ".." not in path.parts, "path_invalid")
    require(not any(ord(c) < 32 for c in value), "path_invalid")
    return path


def select_configuration(
    rendered: dict,
    containers: list[dict],
    *,
    project_directory: str,
    compose_files: list[str],
    approved_source: dict,
) -> str:
    """Require agreement between formal Compose, all mounts and a protected audit."""
    project = normalized_path(project_directory)
    require(
        compose_files and all(normalized_path(x) for x in compose_files), "compose_files_invalid"
    )
    definitions = rendered.get("services", {})
    require(isinstance(definitions, dict) and set(SERVICES) <= set(definitions), "services_missing")
    sources = {}
    for name in SERVICES:
        mounts = [x for x in definitions[name].get("volumes", []) if x.get("target") == TARGET]
        require(len(mounts) == 1, "rendered_config_mount_ambiguous")
        mount = mounts[0]
        require(
            mount.get("type") == "bind" and mount.get("read_only") is True,
            "rendered_config_mount_unsafe",
        )
        source = normalized_path(mount.get("source"))
        require(
            project / "config" in source.parents and source.suffix in {".yaml", ".yml", ".json"},
            "config_scope_invalid",
        )
        sources[name] = str(source)
    require(len(set(sources.values())) == 1, "rendered_config_sources_differ")
    require(len(containers) == len(SERVICES), "container_inventory_changed")
    seen = set()
    for row in containers:
        labels = row.get("Config", {}).get("Labels", {})
        name = labels.get("com.docker.compose.service")
        require(name in SERVICES and name not in seen, "container_inventory_changed")
        seen.add(name)
        require(
            labels.get("com.docker.compose.project.working_dir") == project_directory,
            "compose_working_directory_changed",
        )
        require(
            labels.get("com.docker.compose.project.config_files", "").split(",") == compose_files,
            "compose_files_changed",
        )
        mounts = [x for x in row.get("Mounts", []) if x.get("Destination") == TARGET]
        require(len(mounts) == 1, "container_config_mount_ambiguous")
        mount = mounts[0]
        require(
            mount.get("Type") == "bind"
            and mount.get("RW") is False
            and mount.get("Source") == sources[name],
            "container_config_mount_changed",
        )
    require(seen == set(SERVICES), "container_inventory_changed")
    source = next(iter(sources.values()))
    require(
        isinstance(approved_source, dict) and approved_source.get("path") == source,
        "unapproved_config_source",
    )
    return source


def verify_configuration_data(observed: dict, approved: dict) -> None:
    """Accept only the exact audited data file; never broaden private-file reads."""
    keys = {"path", "uid", "gid", "mode", "nlink", "size", "sha256"}
    require(keys <= set(observed) and keys <= set(approved), "config_proof_incomplete")
    require(
        all(type(observed[k]) is int for k in ("uid", "gid", "mode", "nlink", "size")),
        "config_metadata_invalid",
    )
    require(
        (observed["uid"], observed["gid"]) in {(0, 0), (0, 10001), (10001, 10001)},
        "config_owner_unapproved",
    )
    require(0 <= observed["mode"] <= 0o777 and not observed["mode"] & 0o022, "config_writable")
    require(observed["nlink"] == 1 and 0 < observed["size"] <= 2_097_152, "config_shape_invalid")
    require(
        isinstance(observed["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", observed["sha256"]),
        "config_digest_invalid",
    )
    require(all(observed[k] == approved[k] for k in keys), "audited_config_changed")
    require(
        observed.get("ancestors_safe") is True
        and observed.get("acl_absent") is True
        and observed.get("regular_no_symlink") is True,
        "config_path_unprotected",
    )


def require_preserved_references(before: dict, after: dict, protected_paths: set[str]) -> None:
    """Keep original env-file ordering, required flags and format options exact."""
    require(before == after, "compose_env_references_changed")
    for references in before.values():
        require(isinstance(references, list), "compose_env_references_invalid")
        for reference in references:
            require(
                isinstance(reference, dict)
                and {"path", "required"} <= set(reference) <= {"path", "required", "format"},
                "compose_env_reference_invalid",
            )
            path = str(normalized_path(reference["path"]))
            require(
                type(reference["required"]) is bool and reference.get("format", "raw") == "raw",
                "compose_env_options_invalid",
            )
            require(path in protected_paths, "compose_env_input_unprotected")


def compose_image_candidate(original: dict, candidate_image: str) -> dict:
    """Return six explicit image overrides; preserve extensions and other data.

    This pure transformation can operate on a protected parsed Compose file or
    its rendered model. It does not render interpolation, read env files or write
    an asset. The caller must independently validate real Compose semantics.
    """
    require(
        isinstance(candidate_image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", candidate_image),
        "candidate_image_invalid",
    )
    require(isinstance(original, dict), "compose_shape_invalid")
    services = original.get("services")
    require(isinstance(services, dict), "compose_services_invalid")
    require("include" not in original, "compose_include_requires_review")
    for name in DEPLOYMENT_IMAGE_SERVICES:
        require(isinstance(services.get(name), dict), "deployment_image_service_missing")
        require("extends" not in services[name], "compose_extends_requires_review")
    candidate = copy.deepcopy(original)
    candidate["services"] = copy.deepcopy(services)
    for name in DEPLOYMENT_IMAGE_SERVICES:
        # Break YAML aliases before editing, so x-runtime and unrelated services
        # cannot inherit an unintended in-memory mutation.
        candidate["services"][name] = copy.deepcopy(services[name])
        candidate["services"][name]["image"] = candidate_image
    return candidate


def verify_compose_image_transition(
    before: dict,
    after: dict,
    *,
    approved_before_images: dict,
    resolved_before_images: dict,
    runtime_images: dict,
    expected_runtime_image: str,
    candidate_image: str,
) -> None:
    """Validate audited mixed image references without conflating them with IDs.

    Each approved entry is {reference_sha256, image_id}. Resolved observations
    must come from the caller's protected, current local Docker inspection; this
    function never resolves or pulls an image. Four running-app image identities
    are checked separately from the two deployment helpers and x-runtime.
    """
    expected = compose_image_candidate(before, candidate_image)
    require(
        isinstance(expected_runtime_image, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", expected_runtime_image),
        "runtime_image_invalid",
    )
    references = {name: before["services"][name].get("image") for name in DEPLOYMENT_IMAGE_SERVICES}
    if "x-runtime" in before:
        require(isinstance(before["x-runtime"], dict), "compose_runtime_extension_invalid")
        if "image" in before["x-runtime"]:
            references["x-runtime"] = before["x-runtime"]["image"]
    require(
        isinstance(approved_before_images, dict)
        and isinstance(resolved_before_images, dict)
        and set(approved_before_images) == set(resolved_before_images) == set(references),
        "compose_image_inventory_changed",
    )
    for name, reference in references.items():
        require(
            isinstance(reference, str)
            and 0 < len(reference) <= 512
            and "$" not in reference
            and all(33 <= ord(c) <= 126 for c in reference),
            "compose_image_reference_invalid",
        )
        approved = approved_before_images[name]
        require(
            isinstance(approved, dict)
            and set(approved) == {"reference_sha256", "image_id"}
            and isinstance(approved["image_id"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", approved["image_id"]),
            "compose_image_approval_invalid",
        )
        require(
            approved["reference_sha256"] == hashlib.sha256(reference.encode("ascii")).hexdigest()
            and resolved_before_images[name] == approved["image_id"],
            "compose_image_reference_changed",
        )
    require(
        isinstance(runtime_images, dict)
        and set(runtime_images) == set(SERVICES)
        and all(
            runtime_images[name] == expected_runtime_image == resolved_before_images[name]
            for name in SERVICES
        ),
        "runtime_image_changed",
    )
    require(
        isinstance(after, dict)
        and isinstance(after.get("services"), dict)
        and all(
            isinstance(after["services"].get(name), dict)
            and after["services"][name].get("image") == candidate_image
            for name in DEPLOYMENT_IMAGE_SERVICES
        ),
        "candidate_deployment_images_mismatch",
    )
    try:
        expected_bytes = json.dumps(
            expected, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        after_bytes = json.dumps(after, sort_keys=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ConfigurationProofError("compose_semantic_shape_invalid") from None
    require(after_bytes == expected_bytes, "compose_non_image_semantics_changed")


def verify_failed_prepare_state(
    package_names: set[str],
    evidence_names: set[str],
    result: dict,
    *,
    expected_package_names: set[str],
    expected_evidence_names: set[str],
) -> None:
    """Verify an audited failure ended before extraction or service operations.

    The caller separately pins each old file's digest and identity, and supplies
    the exact inventories from its protected audit. This function performs no IO.
    """
    require(package_names == expected_package_names, "prior_package_not_partial")
    require(evidence_names == expected_evidence_names, "prior_evidence_not_partial")
    require(
        not {"source", "deployment-plan.json", "future-runtime-proof.json"} & package_names
        and not any(name.startswith(("quiesce", "activat")) for name in evidence_names),
        "prior_phase_advanced",
    )
    require(
        result.get("action") == "prepare"
        and result.get("status") == "stopped"
        and result.get("stage") == "read_only_preflight"
        and result.get("facts") == {}
        and isinstance(result.get("error"), dict)
        and result["error"].get("code") == "unsafe_file",
        "prior_result_changed",
    )


def patch_configuration(raw: bytes, thread_id: str) -> bytes:
    """Add exactly the two opt-in keys, retaining other semantic values.

    PyYAML is imported only for this transformation. Deployment callers can run this
    function inside the already-built candidate image without installing host tools.
    """
    import yaml

    require(isinstance(raw, bytes) and len(raw) <= 2_097_152, "config_size_invalid")
    require(
        isinstance(thread_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,36}", thread_id),
        "thread_invalid",
    )

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        require(len(keys) == len(set(keys)), "duplicate_config_key")
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)

    def load_unique(value):
        try:
            return yaml.load(value, Loader=UniqueLoader)
        except yaml.YAMLError:
            raise ConfigurationProofError("config_yaml_invalid") from None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigurationProofError("config_encoding_invalid") from None
    before = load_unique(text)
    require(
        isinstance(before, dict) and isinstance(before.get("host_binding"), dict),
        "host_binding_missing",
    )
    binding = before["host_binding"]
    require(
        binding.get("enabled") is True and binding.get("legacy_runtime_confirmed") is False,
        "host_binding_not_eligible",
    )
    additions = {
        "fresh_session_thread_ids": [thread_id],
        "runtime_fallback_disabled_confirmed": True,
    }
    require(not set(additions) & set(binding), "continuation_already_configured")
    expected = {key: copy.deepcopy(value) for key, value in before.items()}
    expected["host_binding"].update(additions)
    try:
        json.loads(text)
        is_json = True
    except ValueError:
        is_json = False
    if is_json:
        result = (json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    else:
        document = yaml.compose(text, Loader=UniqueLoader)
        nodes = [value for key, value in document.value if key.value == "host_binding"]
        require(
            len(nodes) == 1 and isinstance(nodes[0], yaml.MappingNode),
            "host_binding_layout_invalid",
        )
        node = nodes[0]
        if node.flow_style:
            replacement = yaml.safe_dump(
                expected["host_binding"], default_flow_style=True, sort_keys=False
            ).strip()
            changed = text[: node.start_mark.index] + replacement + text[node.end_mark.index :]
        else:
            ending = "\r\n" if "\r\n" in text else "\n"
            indent = " " * node.start_mark.column
            insertion = indent + 'fresh_session_thread_ids: ["' + thread_id + '"]' + ending
            insertion += indent + "runtime_fallback_disabled_confirmed: true" + ending
            prefix = text[: node.end_mark.index]
            if not prefix.endswith(("\n", "\r")):
                prefix += ending
            changed = prefix + insertion + text[node.end_mark.index :]
        result = changed.encode("utf-8")
    require(load_unique(result) == expected, "config_semantic_delta_invalid")
    require(hashlib.sha256(result).digest() != hashlib.sha256(raw).digest(), "config_delta_missing")
    return result
