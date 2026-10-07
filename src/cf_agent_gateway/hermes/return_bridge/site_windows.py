"""Windows site preparation; plan is read-only and every mutation is explicit.

This controller reuses the installed official Hermes gateway stop/start commands.
It neither installs a service nor runs a model. Peer receipts are operator-carried
coordination records, not authentication tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cf_agent_gateway.hermes.return_bridge import enablement as en
from cf_agent_gateway.hermes.tls import verified_ssl_context

PEER_SCHEMA = "cf-artifact-return/site-peer/v1"
OBSERVATION_SECONDS = 30
OBSERVATION_INTERVAL = 0.5
SOURCE_FILES = (
    "gateway/platforms/api_server.py",
    "hermes_cli/tools_config.py",
    "hermes_cli/gateway_windows.py",
    "hermes_cli/config.py",
)
RELEASE_FILES = (
    "deploy/prepare-return-windows.ps1",
    "src/cf_agent_gateway/hermes/return_bridge/site_windows.py",
    "src/cf_agent_gateway/hermes/return_bridge/enablement.py",
    *("src/cf_agent_gateway/" + path for path in en.BUNDLE_FILES),
)
REQUEST_FIELDS = frozenset(
    {
        "deployment_id",
        "release_commit",
        "hermes_home",
        "source",
        "facts",
        "legacy_origin",
        "hermes_origin",
        "gateway_origin",
        "gateway_ca_file",
        "hermes_ca_file",
        "tls_cert_file",
        "tls_key_file",
        "work_root",
        "state_directory",
        "profile_reference",
        "profile_revision",
    }
)


class SiteError(en.EnablementError):
    pass


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha(value) -> str:
    return _sha(json.dumps(value, sort_keys=True).encode())


def _origin(value: str, *, https: bool = True):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != ("https" if https else "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or "%" in parsed.netloc
            or "\\" in parsed.netloc
            or not 1 <= (parsed.port or (443 if https else 80)) <= 65535
        ):
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise SiteError("invalid_site_origin") from None


def request_defaults(request: dict) -> dict:
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise SiteError("unknown_site_request_field")
    value = dict(request)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value.get("deployment_id", "")):
        raise SiteError("deployment_id_required")
    if not re.fullmatch(r"[0-9a-f]{40}", value.get("release_commit", "")):
        raise SiteError("immutable_release_required")
    home = en._path(value["hermes_home"])
    value.setdefault("source", str(home / "hermes-agent"))
    value.setdefault("facts", str(home / "installs/902a4fe5d10abbac/facts.json"))
    value.setdefault("state_directory", str(home / "cf-artifact-return-site"))
    value.setdefault("work_root", str(home / "cf-artifact-return-work"))
    value.setdefault("profile_reference", "default")
    value.setdefault("profile_revision", 1)
    if value["profile_reference"] != "default":
        raise SiteError("only_observed_default_profile_supported")
    for name in ("source", "facts", "state_directory", "work_root"):
        path = en._path(value[name])
        if not path.is_relative_to(home):
            raise SiteError("site_asset_outside_hermes_home")
    _origin(value["legacy_origin"], https=False)
    _origin(value["gateway_origin"])
    if value.get("hermes_origin"):
        _origin(value["hermes_origin"])
    for name in ("gateway_ca_file", "hermes_ca_file", "tls_cert_file", "tls_key_file"):
        if value.get(name):
            en._path(value[name])
    return value


def _run(command: list[str], *, timeout=30, env=None):
    result = subprocess.run(
        command,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise SiteError("official_management_command_failed")
    return result.stdout


def source_proof(request: dict) -> dict:
    source = en._path(request["source"])
    head = _run(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
    if head != en.OFFICIAL_COMMIT:
        raise SiteError("official_source_version_changed")
    proof = {}
    for relative in SOURCE_FILES:
        actual = en._read(source / relative)
        expected = _run(["git", "-C", str(source), "show", f"{head}:{relative}"])
        if actual != expected:
            raise SiteError("official_source_file_changed")
        proof[relative] = _sha(actual)
    return {"commit": head, "files": proof}


def release_proof(request):
    root = Path(__file__).resolve().parents[4]
    head = _run(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    if head != request["release_commit"]:
        raise SiteError("gateway_release_head_changed")
    if _run(["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=no"]).strip():
        raise SiteError("gateway_release_has_tracked_changes")
    proof = {}
    for relative in RELEASE_FILES:
        content = en._read(root / relative)
        committed = _run(["git", "-C", str(root), "show", f"{head}:{relative}"])
        if content != committed:
            raise SiteError("gateway_release_asset_changed")
        proof[relative] = _sha(content)
    return {"root": str(root), "commit": head, "files": proof}


def installation_proof(request):
    home = en._path(request["hermes_home"])
    facts_path = en._path(request["facts"])
    facts_raw = en._read(facts_path)
    facts = json.loads(facts_raw)
    environment = en._path(facts["packages"]["venv"]["environment"])
    executable = en._path(home / "bin/hermes.exe")
    before = executable.stat()
    if not executable.is_file() or before.st_nlink != 1 or before.st_size > 256 * 1024 * 1024:
        raise SiteError("official_manager_asset_invalid")
    hasher = hashlib.sha256()
    with executable.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
    after = executable.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise SiteError("official_manager_changed_during_read")
    return {
        "facts_sha256": _sha(facts_raw),
        "pyvenv_sha256": _sha(en._read(environment / "pyvenv.cfg")),
        "python": executor(request),
        "management_executable": str(executable),
        "management_sha256": hasher.hexdigest(),
    }


def _auth_reference(home: Path) -> tuple[str, dict]:
    raw = en._read(home / ".env")
    if raw is None:
        raise SiteError("existing_http_auth_reference_missing")
    selected = {}
    for line in raw.decode("utf-8-sig").splitlines():
        name, equals, value = line.partition("=")
        name = name.strip()
        if equals and name in {"API_SERVER_KEY", "API_SERVER_HOST", "API_SERVER_PORT"}:
            if name in selected:
                raise SiteError("ambiguous_http_auth_reference")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if "${" in value or "\n" in value or "\r" in value:
                raise SiteError("unsupported_http_auth_reference")
            selected[name] = value
    key = selected.pop("API_SERVER_KEY", "")
    if len(key) < 16:
        raise SiteError("existing_http_auth_reference_missing")
    return key, selected


def _get_json(origin: str, endpoint: str, key: str, ca_file=None):
    verify = verified_ssl_context(ca_file) if origin.startswith("https://") else True
    with httpx.Client(verify=verify, trust_env=False, follow_redirects=False, timeout=10) as client:
        response = client.get(
            origin.rstrip("/") + endpoint, headers={"Authorization": "Bearer " + key}
        )
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise SiteError("read_only_http_probe_failed")
        return response.json()


def toolset_snapshot(request: dict, config: dict) -> dict:
    # The official endpoint deliberately excludes default MCP servers. Do not
    # silently remove them when replacing a missing explicit platform list.
    if config.get("mcp_servers"):
        raise SiteError("mcp_toolset_preservation_requires_review")
    key, env = _auth_reference(en._path(request["hermes_home"]))
    origin = _origin(request["legacy_origin"], https=False)
    api = config.get("platforms", {}).get("api_server", {})
    extra = api.get("extra", {})
    host = extra.get("host", api.get("host", env.get("API_SERVER_HOST", "127.0.0.1")))
    port = int(extra.get("port", api.get("port", env.get("API_SERVER_PORT", 8642))))
    if (origin.hostname, origin.port or 80) != (host, port):
        raise SiteError("legacy_origin_differs_from_current_configuration")
    response = _get_json(request["legacy_origin"], "/v1/toolsets", key)
    if response.get("platform") != "api_server" or not isinstance(response.get("data"), list):
        raise SiteError("official_toolset_response_invalid")
    rows = []
    for row in response["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise SiteError("official_toolset_response_invalid")
        tools = row.get("tools")
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            raise SiteError("official_toolset_response_invalid")
        rows.append(
            {"name": row["name"], "enabled": row.get("enabled") is True, "tools": sorted(tools)}
        )
    if len({r["name"] for r in rows}) != len(rows):
        raise SiteError("official_toolset_response_invalid")
    rows.sort(key=lambda r: r["name"])
    return {"enabled": [r["name"] for r in rows if r["enabled"]], "rows_sha256": _json_sha(rows)}


def process_snapshot(request: dict) -> dict:
    if os.name != "nt":
        raise SiteError("windows_host_required")
    # The script is constant; no command line (which might contain secrets) is returned.
    script = r"""$r = [Console]::In.ReadToEnd() | ConvertFrom-Json
$rows = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -eq $r.python -and $_.CommandLine -match 'hermes_cli[.]main.*gateway.*run'
} | ForEach-Object {
    [pscustomobject]@{
        pid=$_.ProcessId; executable=$_.ExecutablePath
        created=$_.CreationDate.ToUniversalTime().ToString('o')
    }
})
$ports = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
    $_.LocalPort -in @($r.legacy_port,$r.tls_port)
} | ForEach-Object {
    [pscustomobject]@{host=$_.LocalAddress; port=$_.LocalPort; pid=$_.OwningProcess}
})
@{processes=$rows;listeners=$ports} | ConvertTo-Json -Depth 4 -Compress"""
    payload = {
        "python": executor(request),
        "legacy_port": _origin(request["legacy_origin"], https=False).port or 80,
        "tls_port": (_origin(request["hermes_origin"]).port or 443)
        if request.get("hermes_origin")
        else 0,
    }
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise SiteError("process_observation_failed")
    return json.loads(result.stdout.decode("utf-8-sig"))


def executor(request: dict) -> str:
    facts = json.loads(en._read(en._path(request["facts"])))
    environment = en._path(facts["packages"]["venv"]["environment"])
    home = en._path(request["hermes_home"])
    if not environment.is_relative_to(home / "installs"):
        raise SiteError("selected_generation_outside_install")
    # pyvenv.cfg records the selected base runtime without launching PM/update.
    fields = {}
    for line in (environment / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        fields[name.strip()] = value.strip()
    if fields.get("version_info", fields.get("version")) != "3.14.7":
        raise SiteError("selected_python_version_changed")
    base = en._path(fields.get("home", "")) / "python.exe"
    if not base.is_relative_to(home / "tools") or not base.is_file():
        raise SiteError("selected_python_reference_invalid")
    return str(base)


def _pending_tls(request: dict) -> list[str]:
    pending = []
    for name in (
        "hermes_origin",
        "gateway_ca_file",
        "hermes_ca_file",
        "tls_cert_file",
        "tls_key_file",
    ):
        if not request.get(name) or (name != "hermes_origin" and not Path(request[name]).is_file()):
            pending.append(name)
    return pending


def _tls_assets(request):
    from cryptography import x509

    result = {}
    certificates = {}
    for name in ("gateway_ca_file", "hermes_ca_file", "tls_cert_file"):
        raw = en._read(en._path(request[name]))
        certificate = x509.load_pem_x509_certificate(raw)
        if (
            not certificate.not_valid_before_utc
            <= datetime.now(UTC)
            <= certificate.not_valid_after_utc
        ):
            raise SiteError("tls_certificate_not_current")
        certificates[name] = certificate
        result[name] = _sha(raw)
    leaf = certificates["tls_cert_file"]
    ca = certificates["hermes_ca_file"]
    if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise SiteError("hermes_ca_certificate_required")
    try:
        leaf.verify_directly_issued_by(ca)
        host = _origin(request["hermes_origin"]).hostname
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        try:
            valid = ipaddress.ip_address(host) in san.get_values_for_type(x509.IPAddress)
        except ValueError:
            valid = host in san.get_values_for_type(x509.DNSName)
        if not valid:
            raise ValueError()
    except (ValueError, TypeError, x509.ExtensionNotFound):
        raise SiteError("hermes_certificate_chain_or_hostname_invalid") from None
    # Never read private-key bytes while planning; pairing is checked at apply.
    key = en._path(request["tls_key_file"]).stat()
    result["private_key_metadata"] = {"size": key.st_size, "mtime_ns": key.st_mtime_ns}
    return result


def plan(request: dict, *, snapshot: dict | None = None) -> tuple[dict, en.Plan | None]:
    request = request_defaults(request)
    home = en._path(request["hermes_home"])
    proof = source_proof(request)
    raw, config = en._configuration(home / "config.yaml")
    effective = snapshot if snapshot is not None else toolset_snapshot(request, config)
    pending = _pending_tls(request)
    summary = {
        "schema": "cf-artifact-return/windows-site-plan/v1",
        "deployment_id": request["deployment_id"],
        "release_commit": request["release_commit"],
        "request_sha256": _json_sha(request),
        "source": proof,
        "release": release_proof(request),
        "installation": installation_proof(request),
        "config_sha256": _sha(raw),
        "executor": executor(request),
        "existing_toolsets": effective,
        "pending": pending,
        "activation_allowed": False,
        "management": {
            "executable": str(home / "bin/hermes.exe"),
            "commands": ["gateway stop", "gateway start"],
        },
        "stages": [
            "plan",
            "export-public",
            "csr-plan",
            "prepare-csr",
            "stop",
            "apply",
            "start",
            "quiesce",
            "rollback",
            "start-legacy",
        ],
    }
    material = None
    if not pending:
        summary["tls_assets"] = _tls_assets(request)
        origin = _origin(request["hermes_origin"])
        legacy = _origin(request["legacy_origin"], https=False)
        if (origin.port or 443) == (legacy.port or 80):
            raise SiteError("https_must_not_reuse_current_http_port")
        settings = {
            "gateway_origin": request["gateway_origin"],
            "gateway_ca_file": request["gateway_ca_file"],
            "work_root": request["work_root"],
            "profile_reference": request["profile_reference"],
            "profile_revision": request["profile_revision"],
            "tls_host": origin.hostname,
            "tls_port": origin.port or 443,
            "tls_cert_file": request["tls_cert_file"],
            "tls_key_file": request["tls_key_file"],
            "request_timeout_seconds": 600,
        }
        material = en.plan_hermes(
            home / "config.yaml",
            home / "plugins" / en.PLUGIN_NAME,
            settings,
            inherited_api_toolsets=effective["enabled"],
        )
        summary["enablement"] = material.summary()
    summary["plan_sha256"] = _json_sha(summary)
    return summary, material


def _require_peer(request, peer, allowed):
    if (
        not isinstance(peer, dict)
        or any(
            peer.get(key) != value
            for key, value in {
                "schema": PEER_SCHEMA,
                "role": "server",
                "deployment_id": request["deployment_id"],
                "release_commit": request["release_commit"],
                "activation_allowed": False,
            }.items()
        )
        or peer.get("state") not in allowed
        or not re.fullmatch(r"[0-9a-f]{64}", peer.get("plan_sha256", ""))
        or any(
            peer.get("public_references", {}).get(name) != value
            for name, value in _public_references(request).items()
        )
    ):
        raise SiteError("server_phase_receipt_required")


def _public_references(request):
    return {
        "hermes_origin": request.get("hermes_origin"),
        "gateway_origin": request["gateway_origin"],
        "profile_reference": request.get("profile_reference", "default"),
        "profile_revision": request.get("profile_revision", 1),
        **{
            name.removesuffix("_file") + "_sha256": (
                _sha(en._read(en._path(request[name]))) if request.get(name) else None
            )
            for name in ("hermes_ca_file", "gateway_ca_file")
        },
    }


def _bind_peer(request, peer, *, first=False):
    proof = {"plan_sha256": peer["plan_sha256"], "public_references": _public_references(request)}
    saved = _load_optional(request, "server-plan.json")
    if saved is None and first:
        _save(request, "server-plan.json", proof)
    elif saved != proof:
        raise SiteError("server_plan_changed")


def _receipt(request, state, summary, **details):
    value = {
        "schema": PEER_SCHEMA,
        "role": "hermes",
        "deployment_id": request["deployment_id"],
        "release_commit": request["release_commit"],
        "state": state,
        "activation_allowed": False,
        "plan_sha256": summary["plan_sha256"],
        "source": summary["source"],
        "config_before_sha256": summary["config_sha256"],
        "created_at": datetime.now(UTC).isoformat(),
        "public_references": _public_references(request),
        **details,
    }
    return value


def _save(request, name, value):
    state = en._path(request["state_directory"]) / request["deployment_id"]
    state.mkdir(parents=True, exist_ok=True)
    en._private(state)
    en._replace(state / name, json.dumps(value, indent=2).encode(), private=True)
    return value


def _load(request, name):
    value = _load_optional(request, name)
    if value is None:
        raise SiteError("prior_local_phase_required")
    return value


def _load_optional(request, name):
    path = en._path(request["state_directory"]) / request["deployment_id"] / name
    raw = en._read(path)
    if raw is None:
        return None
    return json.loads(raw)


def _reviewed(request, summary, expected_plan_sha256):
    content = dict(summary)
    actual = content.pop("plan_sha256", None)
    if (
        actual != expected_plan_sha256
        or actual != _json_sha(content)
        or summary.get("request_sha256") != _json_sha(request)
    ):
        raise SiteError("reviewed_plan_changed")
    _recheck_installation(request, summary)


def _journal_path(request, summary):
    path = (
        en._path(request["state_directory"])
        / "journal"
        / summary["enablement"]["plan_sha256"]
        / "manifest.json"
    )
    if path.exists() and en._manifest_read(path)["plan"] != summary["enablement"]:
        raise SiteError("local_phase_receipt_changed")
    return path


def _verified_applied(request, summary):
    applied = _load(request, "applied.json")
    if applied.get("plan_sha256") != summary["plan_sha256"]:
        raise SiteError("local_phase_receipt_changed")
    manifest = en._manifest_read(en._path(applied["journal"]["manifest"]))
    if manifest["state"] != "applied" or manifest["plan"] != summary["enablement"]:
        raise SiteError("local_phase_receipt_changed")
    for asset in manifest["plan"]["changes"]:
        if en._digest(en._read(en._path(asset["path"]))) != asset["after_sha256"]:
            raise SiteError("applied_asset_changed")
    return applied


def _recheck_installation(request, summary):
    if source_proof(request) != summary["source"]:
        raise SiteError("official_source_file_changed")
    if release_proof(request) != summary["release"]:
        raise SiteError("gateway_release_asset_changed")
    if installation_proof(request) != summary["installation"]:
        raise SiteError("official_installation_changed")


def _management(request, action):
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": request["hermes_home"],
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_GATEWAY_INSTALL_START_ON_LOGIN": "0",
        }
    )
    _run(
        [
            str(en._path(request["hermes_home"]) / "bin/hermes.exe"),
            "--profile",
            "default",
            "gateway",
            action,
        ],
        timeout=90,
        env=env,
    )


def _process_identity(process):
    if (
        not isinstance(process, dict)
        or not isinstance(process.get("pid"), int)
        or process["pid"] <= 0
        or not isinstance(process.get("created"), str)
        or not process["created"]
        or not isinstance(process.get("executable"), str)
        or not process["executable"]
    ):
        raise SiteError("process_identity_incomplete")
    return {name: process[name] for name in ("pid", "created", "executable")}


def _observe_until(check):
    deadline = time.monotonic() + OBSERVATION_SECONDS
    while True:
        result = check()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise SiteError("management_result_uncertain_observation_only")
        time.sleep(OBSERVATION_INTERVAL)


def _management_once(request, action, intent_name, intent):
    # This record precedes invocation. A crash in the tiny gap is uncertain too;
    # retrying the CLI only observes and never issues a second management command.
    _save(request, intent_name, intent)
    try:
        _management(request, action)
    except (OSError, subprocess.TimeoutExpired, en.EnablementError):
        intent["management_result"] = "uncertain"
    else:
        intent["management_result"] = "returned"
    _save(request, intent_name, intent)


def _intent(request, summary, name, action):
    value = _load_optional(request, name)
    if value is not None and (
        value.get("plan_sha256") != summary["plan_sha256"] or value.get("action") != action
    ):
        raise SiteError("management_intent_changed")
    return value


def _stop_once(request, summary, *, intent_name, expected_process=None):
    intent = _intent(request, summary, intent_name, "stop")
    if intent is None:
        state = process_snapshot(request)
        if not state["processes"] and expected_process is not None:
            _save(
                request,
                intent_name,
                {
                    "plan_sha256": summary["plan_sha256"],
                    "action": "stop",
                    "created_at": datetime.now(UTC).isoformat(),
                    "process": expected_process,
                    "management_result": "not_invoked_already_stopped",
                },
            )
            return {"observed_stopped": True}
        if len(state["processes"]) != 1:
            raise SiteError("unique_current_gateway_required")
        identity = _process_identity(state["processes"][0])
        if expected_process is not None and identity != expected_process:
            raise SiteError("management_process_identity_changed")
        intent = {
            "plan_sha256": summary["plan_sha256"],
            "action": "stop",
            "created_at": datetime.now(UTC).isoformat(),
            "process": identity,
        }
        _management_once(request, "stop", intent_name, intent)

    def stopped():
        processes = process_snapshot(request)["processes"]
        if not processes:
            return {"observed_stopped": True}
        if len(processes) != 1 or _process_identity(processes[0]) != intent["process"]:
            raise SiteError("management_process_identity_changed")
        return None

    return _observe_until(stopped)


def _gateway_tls(request):
    """Server certificate AND hostname validation, without credentials or HTTP writes."""
    origin = _origin(request["gateway_origin"])
    context = verified_ssl_context(request["gateway_ca_file"])
    with (
        socket.create_connection((origin.hostname, origin.port or 443), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=origin.hostname) as secured,
    ):
        return {
            "origin": request["gateway_origin"],
            "ca_sha256": _sha(en._read(en._path(request["gateway_ca_file"]))),
            "peer_certificate_der_sha256": _sha(secured.getpeercert(binary_form=True)),
            "certificate_and_hostname_verified": True,
        }


def _start_once(request, summary, *, legacy=False):
    intent_name = "start-legacy-intent.json" if legacy else "start-intent.json"
    intent = _intent(request, summary, intent_name, "start")
    if intent is None:
        if process_snapshot(request)["processes"]:
            raise SiteError("gateway_must_remain_stopped")
        intent = {
            "plan_sha256": summary["plan_sha256"],
            "action": "start",
            "created_at": datetime.now(UTC).isoformat(),
            "process": None,
        }
        _management_once(request, "start", intent_name, intent)

    def ready():
        state = process_snapshot(request)
        processes = state["processes"]
        if not processes:
            return None
        if len(processes) != 1:
            raise SiteError("management_process_identity_changed")
        identity = _process_identity(processes[0])
        if intent["process"] is None:
            # Only an observed newly started process may complete this intent.
            # PID plus creation timestamp prevents reuse by a later process.
            if datetime.fromisoformat(identity["created"]) < datetime.fromisoformat(
                intent["created_at"]
            ):
                raise SiteError("management_process_predates_intent")
            intent["process"] = identity
            _save(request, intent_name, intent)
        elif identity != intent["process"]:
            raise SiteError("management_process_identity_changed")
        if any(row["pid"] != identity["pid"] for row in state["listeners"]):
            raise SiteError("management_listener_identity_changed")
        if not legacy and any(
            row["port"] == (_origin(request["legacy_origin"], https=False).port or 80)
            and row["host"] not in {"127.0.0.1", "::1"}
            for row in state["listeners"]
        ):
            raise SiteError("native_http_not_loopback")
        key, _ = _auth_reference(en._path(request["hermes_home"]))
        try:
            response = _get_json(
                request["legacy_origin"] if legacy else request["hermes_origin"],
                "/v1/models",
                key,
                None if legacy else request["hermes_ca_file"],
            )
            if response.get("object") != "list":
                return None
            gateway = None if legacy else _gateway_tls(request)
        except (httpx.HTTPError, OSError, SiteError):
            return None
        after = process_snapshot(request)
        if len(after["processes"]) != 1 or _process_identity(after["processes"][0]) != identity:
            raise SiteError("management_process_identity_changed")
        return {"process": identity, "gateway_tls": gateway}

    return _observe_until(ready)


def execute(request, phase, *, authorized=False, expected_plan_sha256=None, peer=None):
    request = request_defaults(request)
    if phase == "plan":
        summary, _ = plan(request)
        summary["observation"] = process_snapshot(request)
        return summary
    if phase == "export-public":
        summary, material = plan(request)
        if material is None:
            raise SiteError("tls_material_pending_no_public_handoff")
        return _receipt(request, "prepared", summary)
    if phase == "csr-plan":
        return csr_plan(request)
    if not authorized:
        raise SiteError("explicit_maintenance_authorization_required")
    # Serialize intents and phase receipts across concurrent invocations. The
    # separate enablement journal retains its own lock and recovery rules.
    with en._lock(en._path(request["state_directory"]) / request["deployment_id"]):
        return _execute_mutation(request, phase, expected_plan_sha256, peer)


def _execute_mutation(request, phase, expected_plan_sha256, peer):
    if phase == "prepare-csr":
        return prepare_csr(request)
    if phase == "stop":
        summary = _load_optional(request, "reviewed-plan.json")
        if summary is None:
            summary, material = plan(request)
            if material is None:
                raise SiteError("tls_material_pending_no_service_change")
            _reviewed(request, summary, expected_plan_sha256)
            _require_peer(request, peer, {"quiesced"})
            _save(request, "reviewed-plan.json", summary)
        else:
            _reviewed(request, summary, expected_plan_sha256)
            _require_peer(request, peer, {"quiesced"})
        if (
            _sha(en._read(en._path(request["hermes_home"]) / "config.yaml"))
            != summary["config_sha256"]
        ):
            raise SiteError("configuration_changed_since_review")
        _bind_peer(request, peer, first=True)
        _stop_once(request, summary, intent_name="stop-intent.json")
        return _save(request, "quiesced.json", _receipt(request, "quiesced", summary))
    summary = _load(request, "reviewed-plan.json")
    _reviewed(request, summary, expected_plan_sha256)
    _require_peer(request, peer, {"quiesced", "applied", "rolled_back"})
    _bind_peer(request, peer)
    if phase == "apply":
        _require_peer(request, peer, {"quiesced", "applied"})
        _load(request, "quiesced.json")
        if process_snapshot(request)["processes"]:
            raise SiteError("gateway_must_remain_stopped")
        journal_path = _journal_path(request, summary)
        if journal_path.exists():
            material = en._resume_plan(journal_path, request_sha256=None)
            if (
                material.summary() != summary["enablement"]
                or source_proof(request) != summary["source"]
            ):
                raise SiteError("site_changed_since_stop")
        else:
            current, material = plan(request, snapshot=summary["existing_toolsets"])
            if current != summary or material is None:
                raise SiteError("site_changed_since_stop")
        # This validates cert/key pairing without printing or copying key bytes.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(request["tls_cert_file"], request["tls_key_file"])
        work = en._path(request["work_root"])
        work.mkdir(parents=True, exist_ok=True)
        en._private(work)
        journal = en.apply_plan(
            material,
            en._path(request["state_directory"]) / "journal",
            expected_plan_sha256=material.summary()["plan_sha256"],
        )
        return _save(
            request, "applied.json", _receipt(request, "applied", summary, journal=journal)
        )
    if phase == "start":
        _require_peer(request, peer, {"applied"})
        _verified_applied(request, summary)
        if source_proof(request) != summary["source"]:
            raise SiteError("official_source_file_changed")
        if _tls_assets(request) != summary["tls_assets"]:
            raise SiteError("tls_material_changed")
        observation = _start_once(request, summary)
        return _save(
            request,
            "tls-ready.json",
            _receipt(
                request,
                "tls_ready",
                summary,
                https_origin=request["hermes_origin"],
                ca_sha256=_sha(en._read(en._path(request["hermes_ca_file"]))),
                **observation,
            ),
        )
    if phase == "quiesce":
        _require_peer(request, peer, {"quiesced"})
        _verified_applied(request, summary)
        start_intent = _intent(request, summary, "start-intent.json", "start")
        if not start_intent or not start_intent.get("process"):
            if process_snapshot(request)["processes"]:
                raise SiteError("started_process_not_yet_bound_verify_start_first")
        else:
            _stop_once(
                request,
                summary,
                intent_name="stop-upgraded-intent.json",
                expected_process=start_intent["process"],
            )
        return _save(request, "requiesced.json", _receipt(request, "quiesced", summary))
    if phase == "rollback":
        _require_peer(request, peer, {"quiesced", "rolled_back"})
        if process_snapshot(request)["processes"]:
            raise SiteError("gateway_must_remain_stopped")
        journal_path = _journal_path(request, summary)
        if journal_path.exists():
            en.rollback(journal_path)
        elif any(
            en._digest(en._read(en._path(asset["path"]))) != asset["before_sha256"]
            for asset in summary["enablement"]["changes"]
        ):
            raise SiteError("missing_journal_with_changed_assets")
        return _save(request, "rolled-back.json", _receipt(request, "rolled_back", summary))
    if phase == "start-legacy":
        _require_peer(request, peer, {"rolled_back"})
        _load(request, "rolled-back.json")
        if (
            _sha(en._read(en._path(request["hermes_home"]) / "config.yaml"))
            != summary["config_sha256"]
        ):
            raise SiteError("restored_configuration_changed")
        observation = _start_once(request, summary, legacy=True)
        return _save(
            request,
            "legacy-restored.json",
            _receipt(request, "rolled_back", summary, legacy_process_started=True, **observation),
        )
    raise SiteError("unknown_site_phase")


def csr_plan(request):
    if not request.get("hermes_origin"):
        raise SiteError("approved_hermes_https_origin_required")
    host = _origin(request["hermes_origin"]).hostname
    directory = en._path(request["state_directory"]) / request["deployment_id"] / "tls"
    return {
        "state": "certificate_pending",
        "hostname": host,
        "directory": str(directory),
        "key": str(directory / "hermes.key"),
        "csr": str(directory / "hermes.csr"),
        "issuer": "operator-approved CFserver CA; private CA key stays on issuer",
        "activation_allowed": False,
    }


def prepare_csr(request):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    result = csr_plan(request)
    directory = en._path(result["directory"])
    if directory.exists():
        raise SiteError("existing_certificate_stage_preserved")
    directory.mkdir(parents=True, mode=0o700)
    en._private(directory)
    host = result["hostname"]
    try:
        name = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        name = x509.DNSName(host)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .add_extension(x509.SubjectAlternativeName([name]), critical=False)
        .sign(key, hashes.SHA256())
    )
    en._replace(
        Path(result["key"]),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        private=True,
    )
    en._replace(Path(result["csr"]), csr.public_bytes(serialization.Encoding.PEM), private=True)
    return {**result, "state": "csr_prepared", "csr_sha256": _sha(en._read(Path(result["csr"])))}


def initial_request(deployment_id, *, hermes_home=None, hermes_origin=None):
    home = en._path(hermes_home or Path(os.environ["LOCALAPPDATA"]) / "hermes")
    _, refs = _auth_reference(home)
    host, port = refs.get("API_SERVER_HOST"), refs.get("API_SERVER_PORT")
    if not host or not port or not port.isdecimal():
        raise SiteError("existing_http_endpoint_reference_missing")
    root = Path(__file__).resolve().parents[4]
    release = _run(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    return request_defaults(
        {
            "deployment_id": deployment_id,
            "release_commit": release,
            "hermes_home": str(home),
            "legacy_origin": f"http://{host}:{port}",
            "gateway_origin": "https://192.168.1.233:18444",
            "hermes_origin": hermes_origin,
            "gateway_ca_file": None,
            "hermes_ca_file": None,
            "tls_cert_file": None,
            "tls_key_file": None,
        }
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        nargs="?",
        default="plan",
        choices=(
            "init-request",
            "bind-tls",
            "plan",
            "export-public",
            "csr-plan",
            "prepare-csr",
            "stop",
            "apply",
            "start",
            "quiesce",
            "rollback",
            "start-legacy",
        ),
    )
    parser.add_argument("--request")
    parser.add_argument("--deployment-id")
    parser.add_argument("--hermes-home")
    parser.add_argument("--hermes-origin")
    for name in ("gateway-ca-file", "hermes-ca-file", "tls-cert-file", "tls-key-file"):
        parser.add_argument("--" + name)
    parser.add_argument("--authorize-maintenance", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--peer-receipt")
    args = parser.parse_args(argv)
    try:
        if args.phase == "init-request":
            result = initial_request(
                args.deployment_id, hermes_home=args.hermes_home, hermes_origin=args.hermes_origin
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        request = json.loads(en._read(en._path(args.request)))
        if args.phase == "bind-tls":
            for name in (
                "hermes_origin",
                "gateway_ca_file",
                "hermes_ca_file",
                "tls_cert_file",
                "tls_key_file",
            ):
                if value := getattr(args, name):
                    request[name] = value
            print(json.dumps(request_defaults(request), ensure_ascii=False, indent=2))
            return 0
        peer = json.loads(en._read(en._path(args.peer_receipt))) if args.peer_receipt else None
        result = execute(
            request,
            args.phase,
            authorized=args.authorize_maintenance,
            expected_plan_sha256=args.expected_plan_sha256,
            peer=peer,
        )
    except en.EnablementError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": "windows_site_operation_failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
