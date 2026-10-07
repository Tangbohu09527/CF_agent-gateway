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


class _ComposeLoader(yaml.SafeLoader):
    pass


def _compose_mapping(loader, node):
    # Duplicate explicit keys are ambiguous; YAML merge keys from the existing
    # x-runtime anchor are intentionally supported and retain normal precedence.
    explicit = [key.value for key, _ in node.value if key.tag != "tag:yaml.org,2002:merge"]
    if len(set(explicit)) != len(explicit):
        raise assets.EnablementError("duplicate_compose_key")
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


def _env_files(service):
    value = service.get("env_file", [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise assets.EnablementError("compose_env_shape_unsupported")
    return list(value)


def _env_path(value, root):
    if isinstance(value, dict) and set(value) <= {"path", "required", "format"}:
        value = value.get("path")
    if not isinstance(value, str) or not value:
        raise assets.EnablementError("compose_env_shape_unsupported")
    if "$" in value:
        # The outer entry validates effective Docker Compose environment values.
        return value
    path = Path(value)
    return str(path if path.is_absolute() else (root / path).resolve())


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "rollback", "verify-assets"))
    parser.add_argument("--request", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--expected-plan-sha256")
    options = parser.parse_args(argv)
    try:
        request = json.loads(assets._read(assets._path(options.request)))
        validate_request(request)
        if options.action == "plan":
            result = prepare(plan_server(request), options.state_directory)
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
        print(json.dumps({"error": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
