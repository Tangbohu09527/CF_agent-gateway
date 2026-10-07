"""Configuration-only artifact-return activation, callable by a deployment controller.

No services, installers, secrets, network or other repositories are opened here.
Planning keeps existing configuration bytes in memory; public plans contain only
paths, field names and hashes. Apply saves original bytes in a private journal.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from cf_agent_gateway.hermes.return_bridge.protocol import SCHEMA, TOOLSET

PLUGIN_NAME = "cf-artifact-return"
OFFICIAL_COMMIT = "0f4a98f87c17007b81500239d0bd5b9574027b73"
GATEWAY_FIELDS = frozenset(
    {
        "hermes.base_url",
        "hermes.ca_file",
        "artifact.storage_root",
        "artifact_return.enabled",
        "artifact_return.host_contract_confirmed",
        "artifact_return.public_base_url",
        "artifact_return.profile_reference",
        "artifact_return.profile_revision",
        "artifact_return.signing_key_env",
        "artifact_return.max_bytes",
        "artifact_return.max_artifacts",
        "artifact_return.ttl_seconds",
    }
)
BUNDLE_FILES = (
    "__init__.py",
    "hermes/__init__.py",
    "hermes/tls.py",
    "hermes/return_bridge/__init__.py",
    "hermes/return_bridge/files.py",
    "hermes/return_bridge/scope.py",
    "hermes/return_bridge/transport.py",
    "hermes/return_bridge/protocol.py",
    "hermes/return_bridge/plugin.py",
)
SHIM = b'''"""Official Hermes plugin entry; no setup/install code."""
import sys
from pathlib import Path

_lib = Path(__file__).resolve().parent / "lib"
_loaded = sys.modules.get("cf_agent_gateway")
if (_loaded is not None
        and Path(_loaded.__file__).resolve() != _lib / "cf_agent_gateway/__init__.py"):
    raise RuntimeError("artifact return package namespace already loaded from another source")
sys.path.insert(0, str(_lib))
from cf_agent_gateway.hermes.return_bridge.plugin import setup

def register(ctx):
    setup(ctx)
'''


class EnablementError(ValueError):
    """Only stable codes are safe to print; never include YAML or secret values."""


def _digest(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


@dataclass(frozen=True)
class Change:
    path: Path
    fields: tuple[str, ...]
    before: bytes | None = field(repr=False)
    after: bytes = field(repr=False)

    def summary(self):
        return {
            "path": str(self.path),
            "fields": list(self.fields),
            "before_sha256": _digest(self.before),
            "after_sha256": _digest(self.after),
        }


@dataclass(frozen=True)
class Plan:
    side: str
    changes: tuple[Change, ...] = field(repr=False)
    requirements: tuple[dict, ...]
    request_sha256: str | None = None

    def summary(self):
        value = {
            "schema": SCHEMA,
            "official_commit": OFFICIAL_COMMIT,
            "side": self.side,
            "changes": [change.summary() for change in self.changes],
            "requirements": list(self.requirements),
        }
        if self.request_sha256 is not None:
            value["request_sha256"] = self.request_sha256
        value["plan_sha256"] = _digest(json.dumps(value, sort_keys=True).encode())
        return value


def _path(value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise EnablementError("absolute_normalized_path_required")
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or part.is_junction():
            raise EnablementError("linked_path_rejected")
    return candidate


def _read(path: Path) -> bytes | None:
    _path(path)
    if not path.exists():
        return None
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4 * 1024 * 1024:
        raise EnablementError("invalid_configuration_asset")
    content = path.read_bytes()
    after = path.stat()

    def signature(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_nlink,
        )

    if signature(before) != signature(after) or len(content) != after.st_size:
        raise EnablementError("asset_changed_during_read")
    return content


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise EnablementError("duplicate_or_nonstring_configuration_key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _configuration(path: Path):
    before = _read(path)
    if before is None:
        raise EnablementError("existing_configuration_required")
    try:
        value = yaml.load(before, Loader=_UniqueLoader)
    except (ValueError, TypeError, yaml.YAMLError):
        raise EnablementError("configuration_parse_failed") from None
    if not isinstance(value, dict):
        raise EnablementError("configuration_mapping_required")
    return before, value


def _section(config: dict, name: str) -> dict:
    value = config.setdefault(name, {})
    if not isinstance(value, dict):
        raise EnablementError("configuration_shape_conflict")
    return value


def _change(path, before, after, fields):
    encoded = yaml.safe_dump(after, allow_unicode=True, sort_keys=False).encode("utf-8")
    # Preserve exact old bytes for no-op planning (including comments/layout).
    if yaml.safe_load(before) == after:
        encoded = before
    return Change(path, tuple(fields), before, encoded)


def plan_gateway(config_path: str | Path, delta: dict[str, object]) -> Plan:
    """Only managed Gateway fields; existing model/auth/database values are inherited."""
    if not isinstance(delta, dict) or not delta or set(delta) - GATEWAY_FIELDS:
        raise EnablementError("unmanaged_gateway_field")
    path = _path(config_path)
    before, config = _configuration(path)
    revised = copy.deepcopy(config)
    for name, value in delta.items():
        section, key = name.split(".")
        _section(revised, section)[key] = value
    try:
        # Gateway-only validation; these dependencies are not in the host bundle.
        from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
        from cf_agent_gateway.config import APISettings, HermesSettings, Settings, WechatSettings
        from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings

        result = ArtifactReturnSettings(**revised.get("artifact_return", {}))
        hermes = revised.get("hermes", {})
        api = revised.get("api", {})
        Settings(
            artifact_return=result,
            api=APISettings(
                **{
                    name: api[name]
                    for name in ("token_env", "admin_token_env", "max_request_body_bytes")
                    if name in api
                }
            ),
            hermes=HermesSettings(
                enabled=True,
                base_url=hermes.get("base_url"),
                ca_file=hermes.get("ca_file"),
                api_key_env=hermes.get("api_key_env", "HERMES_API_KEY"),
            ),
            wechat=WechatSettings(
                token_env=revised.get("wechat", {}).get("token_env", "CF_AGENT_WECHAT_TOKEN")
            ),
            host_binding=HostBindingSettings(**revised.get("host_binding", {})),
        )
        if (
            not result.enabled
            or not str(hermes.get("base_url", "")).startswith("https://")
            or not result.public_base_url.startswith("https://")
        ):
            raise ValueError()
        if not Path(revised.get("artifact", {}).get("storage_root", "")).is_absolute():
            raise ValueError()
    except (TypeError, ValueError):
        raise EnablementError("invalid_gateway_enablement") from None
    requirements = (
        {
            "kind": "environment_reference",
            "name": result.signing_key_env,
            "only_services": ["gateway", "dispatch-worker"],
            "instruction": "inject protected reference only here, never a shared runtime env_file",
        },
        {
            "kind": "read_only_ca_mount",
            "container_path": hermes.get("ca_file"),
            "services": ["gateway", "dispatch-worker"],
            "instruction": "deployment maps an approved existing CA; no mount is changed here",
        },
        {
            "kind": "private_artifact_storage",
            "path": revised["artifact"]["storage_root"],
            "services": ["gateway", "dispatch-worker", "delivery-worker"],
        },
        {
            "kind": "gateway_https_ingress",
            "origin": result.public_base_url,
            "instruction": "verify existing TLS ingress and CA/hostname before activation",
        },
    )
    return Plan("gateway", (_change(path, before, revised, sorted(delta)),), requirements)


def plan_hermes(
    config_path: str | Path,
    plugin_directory: str | Path,
    settings: dict[str, object],
    *,
    package_source: str | Path | None = None,
) -> Plan:
    """Plan the fixed official plugin bundle and config, preserving existing tools/model/auth.

    Official PlatformConfig promotes the top-level host into extra, with an
    explicit extra.host taking precedence. Both are pinned when both exist;
    the resulting explicit setting overrides the API_SERVER_HOST fallback.
    """
    from cf_agent_gateway.hermes.return_bridge.plugin import BridgeSettings

    try:
        BridgeSettings(**settings)
        if settings["tls_port"] == 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise EnablementError("invalid_hermes_enablement") from None
    config_path, target = _path(config_path), _path(plugin_directory)
    if target.name != PLUGIN_NAME:
        raise EnablementError("dedicated_plugin_directory_required")
    before, config = _configuration(config_path)
    revised = copy.deepcopy(config)
    platforms = revised.get("platforms")
    api = platforms.get("api_server") if isinstance(platforms, dict) else None
    if not isinstance(api, dict):
        raise EnablementError("existing_native_http_platform_required")
    extra = api.get("extra", {})
    if not isinstance(extra, dict):
        raise EnablementError("configuration_shape_conflict")
    managed_hosts = []
    if "host" in extra:
        extra["host"] = "127.0.0.1"
        managed_hosts.append("platforms.api_server.extra.host")
    if "host" in api or "host" not in extra:
        api["host"] = "127.0.0.1"
        managed_hosts.append("platforms.api_server.host")
    plugins = _section(revised, "plugins")
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
        raise EnablementError("plugin_configuration_conflict")
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
    entries = _section(plugins, "entries")
    entry = entries.get(PLUGIN_NAME)
    expected = {"settings": {"enabled": True, **settings}}
    if entry is not None and entry != expected:
        raise EnablementError("existing_plugin_configuration_conflict")
    entries[PLUGIN_NAME] = expected
    toolsets = _section(revised, "platform_toolsets")
    tools = toolsets.get("api_server")
    if not isinstance(tools, list) or any(not isinstance(item, str) for item in tools):
        raise EnablementError("explicit_existing_api_toolsets_required")
    if TOOLSET not in tools:
        tools.append(TOOLSET)
    source = _path(package_source) if package_source else Path(__file__).resolve().parents[2]
    assets = {
        "plugin.yaml": f'name: {PLUGIN_NAME}\nversion: "1.0.0"\nkind: standalone\n'.encode(),
        "__init__.py": SHIM,
    }
    for relative in BUNDLE_FILES:
        content = _read(source / relative)
        if content is None:
            raise EnablementError("reviewed_bundle_source_missing")
        assets["lib/cf_agent_gateway/" + relative] = content
    changes = []
    for relative, content in assets.items():
        destination = target / relative
        old = _read(destination)
        if old is not None and old != content:
            raise EnablementError("existing_bundle_asset_conflict")
        changes.append(Change(destination, ("reviewed_plugin_bundle",), old, content))
    changes.append(
        _change(
            config_path,
            before,
            revised,
            (
                *managed_hosts,
                "plugins.enabled",
                f"plugins.entries.{PLUGIN_NAME}",
                "platform_toolsets.api_server",
            ),
        )
    )
    return Plan(
        "hermes",
        tuple(changes),
        (
            {
                "kind": "existing_official_source",
                "commit": OFFICIAL_COMMIT,
                "instruction": "verify installed source/executor before loading plugin",
            },
            {
                "kind": "native_http_loopback",
                "instruction": "native host pinned to loopback; verify effective service config",
            },
            {
                "kind": "existing_auth_and_model",
                "instruction": "inherit unchanged; no secret is requested",
            },
            {"kind": "private_work_root", "path": settings["work_root"]},
            {
                "kind": "tls_references",
                "certificate": settings["tls_cert_file"],
                "private_key": settings["tls_key_file"],
                "gateway_ca": settings.get("gateway_ca_file"),
                "instruction": "protected references only; no certificate is created or installed",
            },
        ),
    )


def _private(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    descriptor = wintypes.LPVOID()
    if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise EnablementError("private_journal_permissions_failed")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise EnablementError("private_journal_permissions_failed")
    finally:
        kernel.LocalFree(descriptor)


def _replace(path: Path, content: bytes, *, private=False):
    _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    old_mode = stat.S_IMODE(previous.st_mode) if previous else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=".cf-return-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if private:
            _private(temporary)
        elif os.name != "nt":
            if previous:
                os.chown(temporary, previous.st_uid, previous.st_gid)
            temporary.chmod(old_mode)
        if os.name == "nt" and path.exists():
            # ReplaceFile preserves the original target's Windows DACL/metadata.
            import ctypes
            from ctypes import wintypes

            replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            replace_file.argtypes = [
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.LPVOID,
            ]
            replace_file.restype = wintypes.BOOL
            if not replace_file(str(path), str(temporary), None, 0, None, None):
                raise EnablementError("atomic_asset_replace_failed")
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def _lock(state: Path):
    state = _path(state)
    state.mkdir(parents=True, exist_ok=True)
    lock = state / ".operation.lock"
    marker = b"cf-artifact-return/enablement-lock-v1\n"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        # Never clear an unknown/legacy lock. Recognized advisory locks survive
        # as evidence; the OS releases ownership if a process dies mid-apply.
        _path(lock)
        descriptor = os.open(lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    else:
        os.write(descriptor, marker)
        os.fsync(descriptor)
    held = False
    try:
        info = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or os.read(descriptor, len(marker) + 1) != marker
        ):
            raise EnablementError("enablement_operation_busy")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise EnablementError("enablement_operation_busy") from None
        held = True
        yield
    finally:
        if held:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _manifest_write(directory, manifest):
    _replace(directory / "manifest.json", json.dumps(manifest, indent=2).encode(), private=True)


def _manifest_read(path):
    try:
        manifest = json.loads(_read(path))
        summary = dict(manifest["plan"])
        expected = summary.pop("plan_sha256")
        if (
            expected != path.parent.name
            or _digest(json.dumps(summary, sort_keys=True).encode()) != expected
            or summary["schema"] != SCHEMA
            or summary["official_commit"] != OFFICIAL_COMMIT
            or manifest["state"] not in {"prepared", "applied", "rolling_back", "rolled_back"}
        ):
            raise ValueError()
        return manifest
    except (KeyError, TypeError, ValueError):
        raise EnablementError("journal_integrity_failed") from None


def _resume_plan(manifest_path, *, request_sha256):
    """Rehydrate the exact reviewed plan, not a new plan based on partial outputs."""
    manifest = _manifest_read(manifest_path)
    summary = manifest["plan"]
    if summary.get("request_sha256") != request_sha256:
        raise EnablementError("reviewed_request_changed")
    if manifest["state"] not in {"prepared", "applied"}:
        raise EnablementError("journal_conflict")
    changes = []
    for index, saved in enumerate(summary["changes"]):
        before = _read(manifest_path.parent / f"{index}.before")
        after = _read(manifest_path.parent / f"{index}.after")
        if (
            _digest(before) != saved["before_sha256"]
            or after is None
            or _digest(after) != saved["after_sha256"]
        ):
            raise EnablementError("resume_backup_integrity_failed")
        changes.append(Change(_path(saved["path"]), tuple(saved["fields"]), before, after))
    plan = Plan(summary["side"], tuple(changes), tuple(summary["requirements"]), request_sha256)
    if plan.summary() != summary:
        raise EnablementError("journal_integrity_failed")
    return plan


def apply_plan(plan: Plan, state_directory: str | Path, *, expected_plan_sha256: str) -> dict:
    """Apply reviewed files only, under an exclusive deployment window.

    A journal precedes all writes. Repeating the same plan resumes only assets
    whose current bytes equal the recorded before/after hash. External writers
    must honor the deployment window; there is no cross-process filesystem CAS.
    """
    if not isinstance(expected_plan_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_plan_sha256
    ):
        raise EnablementError("reviewed_plan_changed")
    summary = plan.summary()
    if summary["plan_sha256"] != expected_plan_sha256:
        raise EnablementError("reviewed_plan_changed")
    state = _path(state_directory)
    with _lock(state):
        directory = state / expected_plan_sha256
        manifest_path = directory / "manifest.json"
        if directory.exists():
            if not manifest_path.is_file():
                raise EnablementError("incomplete_private_journal")
            manifest = _manifest_read(manifest_path)
            if manifest["plan"] != summary or manifest["state"] not in {"prepared", "applied"}:
                raise EnablementError("journal_conflict")
        else:
            for change in plan.changes:
                if _read(change.path) != change.before:
                    raise EnablementError("asset_changed_since_plan")
            directory.mkdir(mode=0o700)
            _private(directory)
            for index, change in enumerate(plan.changes):
                if change.before is not None:
                    _replace(directory / f"{index}.before", change.before, private=True)
                _replace(directory / f"{index}.after", change.after, private=True)
            manifest = {"plan": summary, "state": "prepared"}
            _manifest_write(directory, manifest)
        # Validate both private originals and ALL targets before the first write.
        for index, change in enumerate(plan.changes):
            if (
                _read(directory / f"{index}.before") != change.before
                or _read(directory / f"{index}.after") != change.after
            ):
                raise EnablementError("resume_backup_integrity_failed")
            if _read(change.path) not in (change.before, change.after):
                raise EnablementError("asset_changed_since_plan")
        for change in plan.changes:
            current = _read(change.path)
            if current == change.after:
                continue
            if current != change.before:
                raise EnablementError("asset_changed_during_apply")
            _replace(change.path, change.after)
        manifest["state"] = "applied"
        _manifest_write(directory, manifest)
    return {"state": "applied", "manifest": str(manifest_path), "plan_sha256": expected_plan_sha256}


def rollback(manifest_path: str | Path) -> dict:
    """Restore original bytes; refuse to overwrite any unrecognized later edit."""
    manifest_path = _path(manifest_path)
    with _lock(manifest_path.parent.parent):
        manifest = _manifest_read(manifest_path)
        if manifest["state"] == "rolled_back":
            return {"state": "rolled_back", "manifest": str(manifest_path)}
        changes = manifest["plan"]["changes"]
        restore = []
        for index, change in enumerate(changes):
            path = _path(change["path"])
            content = _read(path)
            actual = _digest(content)
            allowed = {change["after_sha256"]}
            if manifest["state"] in {"prepared", "rolling_back"}:
                allowed.add(change["before_sha256"])
            if actual not in allowed:
                raise EnablementError("rollback_conflicts_with_later_change")
            before = _read(manifest_path.parent / f"{index}.before")
            if _digest(before) != change["before_sha256"]:
                raise EnablementError("rollback_backup_integrity_failed")
            restore.append((path, actual, before))
        manifest["state"] = "rolling_back"
        _manifest_write(manifest_path.parent, manifest)
        for path, expected, before in reversed(restore):
            if _digest(_read(path)) != expected:
                raise EnablementError("rollback_conflicts_with_later_change")
            if before is None:
                if path.exists():
                    path.unlink()  # Only an exact manifest-owned new file, never recursive.
            else:
                _replace(path, before)
        manifest["state"] = "rolled_back"
        _manifest_write(manifest_path.parent, manifest)
    return {"state": "rolled_back", "manifest": str(manifest_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "rollback"))
    parser.add_argument("--request", help="Non-secret reference-only JSON request")
    parser.add_argument("--state-directory")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--manifest")
    args = parser.parse_args(argv)
    try:
        if args.action == "rollback":
            result = rollback(args.manifest)
        else:
            if args.action == "apply" and (
                not args.expected_plan_sha256
                or not re.fullmatch(r"[0-9a-f]{64}", args.expected_plan_sha256)
            ):
                raise EnablementError("reviewed_plan_changed")
            request = json.loads(_read(_path(args.request)))
            request_sha256 = _digest(json.dumps(request, sort_keys=True).encode())
            side = request.pop("side")
            if side not in {"gateway", "hermes"}:
                raise EnablementError("invalid_side")
            saved = (
                _path(args.state_directory) / args.expected_plan_sha256 / "manifest.json"
                if args.action == "apply" and args.state_directory and args.expected_plan_sha256
                else None
            )
            if saved is not None and saved.exists():
                plan = _resume_plan(saved, request_sha256=request_sha256)
            else:
                plan = replace(
                    (plan_gateway if side == "gateway" else plan_hermes)(**request),
                    request_sha256=request_sha256,
                )
            result = (
                plan.summary()
                if args.action == "plan"
                else apply_plan(
                    plan,
                    args.state_directory,
                    expected_plan_sha256=args.expected_plan_sha256,
                )
            )
    except EnablementError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": "enablement_operation_failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
