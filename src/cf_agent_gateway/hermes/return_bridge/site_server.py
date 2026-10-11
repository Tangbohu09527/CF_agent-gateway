"""Protected deployment assets, run by prepare-return-server.py in the candidate image.

No Docker socket, network, services, database writes or secret generation here.
The host entry owns the maintenance window; this module reuses enablement's
private journal and exact rollback. Public output is references and digests only.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from cf_agent_gateway.hermes.return_bridge import enablement as assets

SCHEMA = "cf-return-site/v1"
LIMIT = 1_048_576
APPS = ("gateway", "dispatch-worker", "worker", "delivery-worker")
SIGNING_ENV = "CF_GATEWAY_ARTIFACT_RETURN_KEY"
CA_TARGET = "/run/cf-gateway/ca/hermes-ca.pem"
STATE_TARGET = "/var/lib/cf-agent-gateway"
VALIDATION_ENV = "x-cf-return-validation-env"
_MISSING = object()


class ComposeShapeError(assets.EnablementError):
    """A locatable structural error, without serializing any configuration value."""

    def __init__(self, location, value):
        super().__init__("compose_env_shape_unsupported")
        if not re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,240}", location):
            location = "compose.env_file"
        if value is _MISSING:
            kind = "missing"
        elif value is None:
            kind = "null"
        elif isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, str):
            kind = "string"
        elif isinstance(value, list):
            kind = "array"
        elif isinstance(value, dict):
            kind = "object"
        elif isinstance(value, (int, float)):
            kind = "number"
        else:
            kind = "other"
        self.details = {"location": location, "type": kind}


class _ComposeLoader(yaml.SafeLoader):
    pass


def _compose_mapping(loader, node):
    # Duplicate explicit keys are ambiguous; YAML merge keys from the existing
    # x-runtime anchor are intentionally supported and retain normal precedence.
    explicit = [key.value for key, _ in node.value if key.tag != "tag:yaml.org,2002:merge"]
    if len(set(explicit)) != len(explicit):
        raise assets.EnablementError("duplicate_compose_key")
    if sum(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value) > 1:
        raise assets.EnablementError("duplicate_compose_merge_key")
    loader.flatten_mapping(node)
    return dict(loader.construct_pairs(node, deep=True))


_ComposeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _compose_mapping)


def _yaml(path):
    content = assets._read(path)
    if content is None:
        raise assets.EnablementError("existing_site_asset_required")
    try:
        value = yaml.load(content, Loader=_ComposeLoader)
        if not isinstance(value, dict):
            raise ValueError()
    except assets.EnablementError:
        raise
    except (ValueError, TypeError, yaml.YAMLError):
        raise assets.EnablementError("invalid_site_asset") from None
    return content, value


def _origin(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or url.username
            or url.password
            or url.port == 0
            or any(c.isspace() for c in value)
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise assets.EnablementError("verified_https_origin_required") from None
    return value.rstrip("/")


def validate_request(request):
    if request.get("schema") != SCHEMA:
        raise assets.EnablementError("site_schema_mismatch")
    for name, pattern in (
        ("release_commit", r"[0-9a-f]{40}"),
        ("candidate_image", r"sha256:[0-9a-f]{64}"),
        ("baseline_image", r"sha256:[0-9a-f]{64}"),
    ):
        if not re.fullmatch(pattern, str(request.get(name, ""))):
            raise assets.EnablementError("fixed_release_identity_required")
    for name in ("gateway_root", "nginx_root", "hermes_ca_file", "signing_env_file"):
        assets._path(request[name])
    _origin(request["hermes_origin"])
    _origin(request["gateway_origin"])
    signing = Path(request["signing_env_file"])
    root = Path(request["gateway_root"])
    if signing.parent != root / "secrets" or signing.name != "artifact-return.env":
        raise assets.EnablementError("dedicated_local_signing_reference_required")
    if request["candidate_image"] == request["baseline_image"]:
        raise assets.EnablementError("candidate_must_include_new_return_assets")


def _env_files(service, location="env_file"):
    value = service.get("env_file", [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ComposeShapeError(location, value)
    return list(value)


def _env_path(value, root, location="env_file"):
    if isinstance(value, dict):
        if not set(value) <= {"path", "required", "format"}:
            raise ComposeShapeError(location, value)
        if "required" in value and type(value["required"]) is not bool:
            raise ComposeShapeError(location + ".required", value["required"])
        if "format" in value and not isinstance(value["format"], str):
            raise ComposeShapeError(location + ".format", value["format"])
        value = value.get("path", _MISSING)
        location += ".path"
    if not isinstance(value, str) or not value:
        raise ComposeShapeError(location, value)
    if "$" in value:
        # The outer entry validates effective Docker Compose environment values.
        return value
    path = Path(value)
    return str(path if path.is_absolute() else (root / path).resolve())


def _extension_env_nodes(value, prefix=""):
    """Visit extension env_file fields without interpreting their other data."""
    if isinstance(value, dict):
        if "env_file" in value:
            yield prefix + ".env_file", value
        for key, child in value.items():
            yield from _extension_env_nodes(child, f"{prefix}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _extension_env_nodes(child, f"{prefix}[{index}]")


def _compose_env_nodes(value):
    # Missing service env_file means an empty reference list. In arbitrary
    # extensions, only a present env_file field has reference semantics here.
    for name, service in value["services"].items():
        yield f"services.{name}.env_file", service
        for key, child in service.items():
            if isinstance(key, str) and key.startswith("x-"):
                yield from _extension_env_nodes(child, f"services.{name}.{key}")
    for key, child in value.items():
        if isinstance(key, str) and key.startswith("x-"):
            yield from _extension_env_nodes(child, key)


def _compose_structure(value, root):
    # External Compose sources cannot be proven equivalent by examining this
    # one protected asset; do not silently skip their env_file references.
    if "include" in value:
        raise assets.EnablementError("compose_include_requires_review")
    if VALIDATION_ENV in value:
        raise assets.EnablementError("compose_validation_extension_conflict")
    services = value.get("services")
    if not isinstance(services, dict):
        raise assets.EnablementError("compose_services_shape_unsupported")
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            raise assets.EnablementError("compose_services_shape_unsupported")
        if "extends" in service:
            raise assets.EnablementError("compose_extends_requires_review")
    for location, node in _compose_env_nodes(value):
        for index, item in enumerate(_env_files(node, location)):
            _env_path(item, root, f"{location}[{index}]")


def _volume(value):
    if isinstance(value, str):
        match = re.fullmatch(r"(.+):(/[^:]+)(?::([^:]+))?", value)
        if not match:
            raise assets.EnablementError("compose_volume_shape_unsupported")
        source, target, mode = match.groups()
        return source, target, bool(mode and "ro" in mode.split(","))
    if isinstance(value, dict):
        return value.get("source"), value.get("target"), value.get("read_only", False)
    raise assets.EnablementError("compose_volume_shape_unsupported")


def plan_compose(path, request):
    before, original = _yaml(path)
    _compose_structure(original, path.parent)
    revised = copy.deepcopy(original)
    services = revised.get("services", {})
    state_sources = []
    for name in APPS:
        service = services.get(name)
        if not isinstance(service, dict) or service.get("user") != "10001:10001":
            raise assets.EnablementError("application_service_identity_changed")
        if service.get("read_only") is not True or "ALL" not in service.get("cap_drop", []):
            raise assets.EnablementError("application_hardening_missing")
        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            raise assets.EnablementError("compose_volume_shape_unsupported")
        states = [_volume(item) for item in volumes if _volume(item)[1] == STATE_TARGET]
        if len(states) != 1 or states[0][2] or not states[0][0]:
            raise assets.EnablementError("shared_state_read_write_mount_required")
        state_sources.append(states[0][0])
        service["image"] = request["candidate_image"]
        environment = service.get("environment", {})
        if not isinstance(environment, dict) or SIGNING_ENV in environment:
            raise assets.EnablementError("signing_key_must_use_dedicated_env_file")
        env_files = _env_files(service)
        if request["signing_env_file"] in [_env_path(item, path.parent) for item in env_files]:
            raise assets.EnablementError("site_already_has_signing_reference")
        if name in {"gateway", "dispatch-worker"}:
            env_files.append(request["signing_env_file"])
            service["env_file"] = env_files
            if any(_volume(item)[1] == CA_TARGET for item in volumes):
                raise assets.EnablementError("existing_ca_mount_conflict")
            service["volumes"] = [
                *volumes,
                {
                    "type": "bind",
                    "source": request["hermes_ca_file"],
                    "target": CA_TARGET,
                    "read_only": True,
                    "bind": {"create_host_path": False},
                },
            ]
    if len(set(state_sources)) != 1 or state_sources[0] != "gateway-state":
        raise assets.EnablementError("existing_named_artifact_volume_required")
    for name, service in services.items():
        if name in {"gateway", "dispatch-worker"}:
            continue
        if request["signing_env_file"] in [
            _env_path(item, path.parent) for item in _env_files(service)
        ]:
            raise assets.EnablementError("signing_reference_in_unapproved_service")
    # Only these fields may differ, including after anchor expansion. Unknown
    # Compose fields, networks, runtime login container and database stay intact.
    expected = copy.deepcopy(revised)
    for name in APPS:
        expected["services"][name] = copy.deepcopy(original["services"][name])
    if expected != original:
        raise assets.EnablementError("unmanaged_compose_change")
    return assets.Change(
        path,
        ("four_application_images", "api_dispatch_ca_mount", "api_dispatch_signing_env_file"),
        before,
        yaml.safe_dump(revised, sort_keys=False, allow_unicode=True).encode(),
    )


def plan_server(request):
    """Read full local files; never reconstruct a deployment from site summaries."""
    from cf_agent_gateway.hermes.return_bridge.site_nginx import plan_nginx

    validate_request(request)
    root, nginx = Path(request["gateway_root"]), Path(request["nginx_root"])
    config_path = root / "config/production.yaml"
    _, config = _yaml(config_path)
    existing_limit = config.get("api", {}).get("max_request_body_bytes", LIMIT)
    if type(existing_limit) is not int or existing_limit > LIMIT:
        raise assets.EnablementError("existing_larger_global_body_limit_requires_review")
    storage = config.get("artifact", {}).get("storage_root")
    if storage != STATE_TARGET + "/artifacts":
        raise assets.EnablementError("existing_private_artifact_root_required")
    ca = assets._read(Path(request["hermes_ca_file"]))
    if ca is None or b"PRIVATE KEY" in ca:
        raise assets.EnablementError("public_hermes_ca_required")
    import ssl

    try:
        ssl.create_default_context(cafile=request["hermes_ca_file"])
    except (OSError, ValueError):
        raise assets.EnablementError("public_hermes_ca_invalid") from None
    delta = {
        "hermes.base_url": _origin(request["hermes_origin"]),
        "hermes.ca_file": CA_TARGET,
        "api.max_request_body_bytes": LIMIT,
        "artifact_return.enabled": True,
        "artifact_return.host_contract_confirmed": True,
        "artifact_return.public_base_url": _origin(request["gateway_origin"]),
        "artifact_return.profile_reference": request["profile_reference"],
        "artifact_return.profile_revision": request.get("profile_revision", 1),
        "artifact_return.signing_key_env": SIGNING_ENV,
        "artifact_return.max_bytes": LIMIT,
    }
    gateway_plan = assets.plan_gateway(config_path, delta)
    ingress_plan = plan_nginx(nginx / "nginx.conf", listen_port=8443)
    compose_change = plan_compose(root / "docker-compose.prod.yml", request)
    requirements = (
        *gateway_plan.requirements,
        *ingress_plan.requirements,
        {"kind": "maintenance_gate_closed", "services": list(APPS)},
        {
            "kind": "fixed_release",
            "commit": request["release_commit"],
            "image": request["candidate_image"],
            "rollback_image": request["baseline_image"],
        },
        {"kind": "peer_windows_tls_ready_before_start", "origin": request["hermes_origin"]},
        {"kind": "legitimate_new_dispatch_ack_requires_separate_model_authorization"},
        *(
            {
                "kind": "unchanged_reference",
                "path": str(path),
                "sha256": assets._digest(assets._read(path)),
            }
            for path in (Path(request["hermes_ca_file"]), root / ".env", nginx / "compose.yaml")
        ),
    )
    plan = assets.Plan(
        "return-site-server",
        (*gateway_plan.changes, compose_change, *ingress_plan.changes),
        requirements,
    )
    return replace(plan, request_sha256=_request_hash(request))


def _request_hash(request):
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def prepare(plan, state_directory):
    """Save reviewed candidates and exact originals privately without changing targets."""
    state = assets._path(state_directory)
    summary = plan.summary()
    directory = state / summary["plan_sha256"]
    with assets._lock(state):
        if directory.exists():
            existing = assets._manifest_read(directory / "manifest.json")
            if existing != {"plan": summary, "state": "prepared"}:
                raise assets.EnablementError("site_plan_journal_conflict")
            assets._resume_plan(directory / "manifest.json", request_sha256=plan.request_sha256)
        else:
            directory.mkdir(mode=0o700)
            assets._private(directory)
            for index, change in enumerate(plan.changes):
                if assets._read(change.path) != change.before:
                    raise assets.EnablementError("asset_changed_since_plan")
                if change.before is not None:
                    assets._replace(directory / f"{index}.before", change.before, private=True)
                assets._replace(directory / f"{index}.after", change.after, private=True)
            assets._manifest_write(directory, {"plan": summary, "state": "prepared"})
    return {
        **summary,
        "manifest": str(directory / "manifest.json"),
        "candidates": [
            {"path": str(change.path), "candidate_file": str(directory / f"{i}.after")}
            for i, change in enumerate(plan.changes)
        ],
        "production_changed": False,
    }


def _restore_plan(request, state, digest):
    if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise assets.EnablementError("reviewed_plan_required")
    manifest = assets._path(state) / digest / "manifest.json"
    plan = assets._resume_plan(manifest, request_sha256=_request_hash(request))
    if plan.side != "return-site-server":
        raise assets.EnablementError("wrong_site_plan")
    for reference in plan.requirements:
        if (
            reference.get("kind") == "unchanged_reference"
            and assets._digest(assets._read(assets._path(reference["path"]))) != reference["sha256"]
        ):
            raise assets.EnablementError("site_reference_changed_since_plan")
    return plan, manifest


def prepare_compose_validation(request, state, digest):
    """Private, non-deployable inputs for real Compose interpolation and loading.

    The two reference probes retain original and candidate env_file values in an
    extension while avoiding env-file reads. The resolved validation copy removes
    only the two newly appended signing references: all existing files still
    undergo Compose's normal required-file, interpolation and override processing.
    None of these copies introduces a secret or a production placeholder.
    """
    plan, manifest = _restore_plan(request, state, digest)
    target = Path(request["gateway_root"]) / "docker-compose.prod.yml"
    changes = [change for change in plan.changes if change.path == target]
    if len(changes) != 1:
        raise assets.EnablementError("site_compose_candidate_missing")
    change = changes[0]
    if assets._read(target) != change.before:
        raise assets.EnablementError("asset_changed_since_plan")
    if plan_compose(target, request).after != change.after:
        raise assets.EnablementError("compose_candidate_differs_from_plan")
    # Read the protected candidate itself, rather than reconstructing it from a
    # Compose export (which could expose expanded environment secrets).
    index = plan.changes.index(change)
    original_file = manifest.parent / f"{index}.before"
    original_bytes, original = _yaml(original_file)
    candidate_file = manifest.parent / f"{index}.after"
    _, candidate = _yaml(candidate_file)
    _compose_structure(original, target.parent)
    _compose_structure(candidate, target.parent)
    original_probe, probe, validation = (
        copy.deepcopy(original),
        copy.deepcopy(candidate),
        copy.deepcopy(candidate),
    )
    empty_locations = {}
    for label, source, reference_probe in (
        ("original", original, original_probe),
        ("candidate", candidate, probe),
    ):
        empty_locations[label] = [
            location
            for location, node in _compose_env_nodes(source)
            if "env_file" not in node or node["env_file"] == []
        ]
        reference_probe[VALIDATION_ENV] = {
            name: copy.deepcopy(_env_files(service, f"services.{name}.env_file"))
            for name, service in source["services"].items()
        }
        for service in reference_probe["services"].values():
            service["env_file"] = []
    signing = request["signing_env_file"]
    for name, service in candidate["services"].items():
        env_files = _env_files(service, f"services.{name}.env_file")
        if name in {"gateway", "dispatch-worker"}:
            if not env_files or env_files[-1] != signing or env_files.count(signing) != 1:
                raise assets.EnablementError("compose_signing_reference_order_changed")
            validation["services"][name]["env_file"] = copy.deepcopy(env_files[:-1])
        elif signing in [_env_path(item, target.parent) for item in env_files]:
            raise assets.EnablementError("signing_reference_in_unapproved_service")
    paths = {
        "original_reference_candidate": manifest.parent
        / "compose-original-reference-validation-only.yaml",
        "reference_candidate": manifest.parent / "compose-reference-validation-only.yaml",
        "validation_candidate": manifest.parent / "compose-resolved-validation-only.yaml",
    }
    with assets._lock(assets._path(state)):
        for label, content in (
            ("original_reference_candidate", original_probe),
            ("reference_candidate", probe),
            ("validation_candidate", validation),
        ):
            data = yaml.safe_dump(content, sort_keys=False, allow_unicode=True).encode()
            existing = assets._read(paths[label])
            if existing is not None and existing != data:
                raise assets.EnablementError("compose_validation_candidate_conflict")
            if existing is None:
                assets._replace(paths[label], data, private=True)
    return {
        "plan_sha256": digest,
        "original_sha256": assets._digest(original_bytes),
        "candidate_sha256": assets._digest(change.after),
        "empty_locations": empty_locations,
        **{name: str(path) for name, path in paths.items()},
        "purpose": "compose_validation_only_not_deployment_assets",
        "production_changed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("plan", "compose-validation", "apply", "rollback", "verify-assets")
    )
    parser.add_argument("--request", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--expected-plan-sha256")
    options = parser.parse_args(argv)
    try:
        request = json.loads(assets._read(assets._path(options.request)))
        validate_request(request)
        if options.action == "plan":
            result = prepare(plan_server(request), options.state_directory)
        elif options.action == "compose-validation":
            result = prepare_compose_validation(
                request, options.state_directory, options.expected_plan_sha256
            )
        elif options.action == "rollback":
            digest = options.expected_plan_sha256
            if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
                raise assets.EnablementError("reviewed_plan_required")
            manifest = assets._path(options.state_directory) / digest / "manifest.json"
            value = assets._manifest_read(manifest)
            if value["plan"].get("request_sha256") != _request_hash(request):
                raise assets.EnablementError("reviewed_request_changed")
            result = assets.rollback(manifest)
        else:
            plan, _ = _restore_plan(request, options.state_directory, options.expected_plan_sha256)
            if options.action == "apply":
                result = assets.apply_plan(
                    plan,
                    options.state_directory,
                    expected_plan_sha256=options.expected_plan_sha256,
                )
            else:
                if any(assets._read(change.path) != change.after for change in plan.changes):
                    raise assets.EnablementError("site_files_differ_from_reviewed_candidate")
                result = {
                    "files_exact": True,
                    "plan_sha256": plan.summary()["plan_sha256"],
                    "ack": "pending_legitimate_new_task",
                    "gate_may_open": False,
                }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (assets.EnablementError, OSError, KeyError, ValueError, TypeError) as error:
        code = (
            str(error)
            if isinstance(error, assets.EnablementError)
            else "site_asset_operation_failed"
        )
        print(json.dumps({"error": code, **getattr(error, "details", {})}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
