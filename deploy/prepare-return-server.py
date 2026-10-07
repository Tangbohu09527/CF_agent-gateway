#!/usr/bin/env python3
"""Opt-in, gate-closed attachment-return deployment using the existing Compose owner.

No SSH, installer, migration, model call, WeChat send, or automatic traffic opening.
The host needs only stdlib Python, Git and its existing rootful Docker CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "cf-return-site/v1"
PEER_SCHEMA = "cf-artifact-return/site-peer/v1"
APPS = ("gateway", "worker", "dispatch-worker", "delivery-worker")
GATED = ("worker", "delivery-worker")
SIGNING_ENV = "CF_GATEWAY_ARTIFACT_RETURN_KEY"
STAGES = (
    "init-request",
    "plan",
    "certificate-status",
    "quiesce",
    "apply",
    "start",
    "verify",
    "rollback",
    "start-old",
)
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
CONTAINER = re.compile(r"[0-9a-f]{64}\Z")


class EntryError(RuntimeError):
    """Stable error codes only; command output and configuration stay private."""


def fail(code: str):
    raise EntryError(code)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def safe_path(
    value: str | Path, *, file: bool = False, private: bool = False, service_uid: int | None = None
) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(c in str(path) for c in "\r\n\0,"):
        fail("unsafe_path")
    for component in (*reversed(path.parents), path):
        if not component.exists() and not component.is_symlink():
            continue
        item = component.lstat()
        if stat.S_ISLNK(item.st_mode) or (
            hasattr(component, "is_junction") and component.is_junction()
        ):
            fail("linked_path")
        if os.name == "posix" and item.st_uid not in {os.geteuid(), service_uid}:
            # System-owned ancestors are valid for an unprivileged isolated test.
            if component != path and item.st_uid == 0:
                continue
            fail("path_owner_mismatch")
        if os.name == "posix" and item.st_mode & 0o022:
            if component != path and item.st_uid == 0 and item.st_mode & stat.S_ISVTX:
                continue
            fail("writable_path")
    if file:
        item = path.lstat()
        if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            fail("not_private_regular_file")
        if private and os.name == "posix" and stat.S_IMODE(item.st_mode) != 0o600:
            fail("private_file_mode_required")
    return path


def read_json(path: Path, *, private: bool = False):
    safe_path(path, file=True, private=private)
    try:
        if path.stat().st_size > 2_097_152:
            fail("json_limit")
        result = json.loads(path.read_bytes())
    except (UnicodeError, ValueError):
        fail("invalid_json")
    if not isinstance(result, dict):
        fail("json_object_required")
    return result


def private_directory(path: Path):
    safe_path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        safe_path(directory)
    if not path.is_dir() or (os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != 0o700):
        fail("private_state_directory_required")


def save_json(path: Path, value):
    safe_path(path)
    temp = path.with_name(path.name + ".pending-" + secrets.token_hex(8))
    descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class Commands:
    def run(self, arguments, *, timeout=60, cwd=None):
        environment = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
        }
        process = subprocess.Popen(
            [str(value) for value in arguments],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # This terminates only our CLI process, never a Docker container.
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            fail("command_result_unknown")
        if process.returncode:
            fail("command_failed")
        return output.strip()


class ServerEntry:
    def __init__(self, request_path, *, release_commit, candidate_image, commands=None):
        self.request_path = safe_path(request_path, file=True)
        self.request = read_json(self.request_path)
        self.commands = commands or Commands()
        self.validate_request(release_commit, candidate_image)
        self.root = safe_path(self.request["gateway_root"])
        self.release = safe_path(self.request["release_root"])
        self.nginx = safe_path(self.request["nginx_root"])
        self.state_dir = safe_path(self.request["state_directory"])
        self.asset_dir = self.state_dir / "assets"
        self.state_path = self.state_dir / "server.json"
        self.state = read_json(self.state_path, private=True) if self.state_path.exists() else None
        self.request_hash = digest(encoded(self.request))
        if self.state and self.state.get("request_sha256") != self.request_hash:
            fail("request_changed")

    def validate_request(self, release_commit, candidate_image):
        request = self.request
        required = {
            "schema",
            "release_commit",
            "candidate_image",
            "baseline_image",
            "gateway_root",
            "nginx_root",
            "hermes_origin",
            "hermes_ca_file",
            "gateway_origin",
            "gateway_ca_file",
            "profile_reference",
            "profile_revision",
            "signing_env_file",
            "release_root",
            "nginx_image",
            "state_directory",
            "deployment_id",
            "gateway_health_url",
            "peer_state_file",
        }
        optional = {"approved_ca_cert", "approved_ca_key", "windows_csr", "signed_output"}
        if (
            not required.issubset(request)
            or set(request) - required - optional
            or request.get("schema") != SCHEMA
        ):
            fail("request_shape_invalid")
        if not COMMIT.fullmatch(release_commit) or request["release_commit"] != release_commit:
            fail("release_commit_required")
        if not IMAGE.fullmatch(candidate_image) or request["candidate_image"] != candidate_image:
            fail("candidate_image_required")
        if any(
            not isinstance(request[k], str) or not IMAGE.fullmatch(request[k])
            for k in ("baseline_image", "nginx_image")
        ):
            fail("immutable_images_required")
        if not isinstance(request["deployment_id"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", request["deployment_id"]
        ):
            fail("deployment_id_invalid")
        for key in ("gateway_origin", "hermes_origin", "gateway_health_url"):
            try:
                parsed = urlsplit(request[key])
                valid = parsed.hostname and not (
                    parsed.username or parsed.password or parsed.query or parsed.fragment
                )
                valid = valid and parsed.path in {"", "/"} and parsed.port != 0
                if key == "gateway_health_url":
                    valid = (
                        valid
                        and parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "::1"}
                    )
                else:
                    valid = valid and parsed.scheme == "https"
                if not valid or any(c.isspace() for c in request[key]):
                    fail("origin_invalid")
            except (TypeError, ValueError):
                fail("origin_invalid")
        for key in (
            "gateway_root",
            "nginx_root",
            "release_root",
            "state_directory",
            "hermes_ca_file",
            "gateway_ca_file",
            "signing_env_file",
            "peer_state_file",
        ):
            if request[key] is None and key in {"hermes_ca_file", "gateway_ca_file"}:
                continue
            safe_path(request[key])
        roots = [Path(request[k]) for k in ("gateway_root", "nginx_root", "state_directory")]
        if any(
            a == b or a in b.parents or b in a.parents
            for i, a in enumerate(roots)
            for b in roots[i + 1 :]
        ):
            fail("overlapping_roots")
        if (
            Path(request["signing_env_file"])
            != Path(request["gateway_root"]) / "secrets/artifact-return.env"
        ):
            fail("signing_file_scope_invalid")

    def run(self, arguments, **kwargs):
        return self.commands.run(arguments, **kwargs)

    def docker(self, arguments, **kwargs):
        return self.run(["docker", "--context", "default", *arguments], **kwargs)

    def compose(self, arguments, *, nginx=False, timeout=60):
        root = self.nginx if nginx else self.root
        command = ["compose", "--project-directory", str(root)]
        if not nginx:
            command += ["--env-file", str(root / ".env")]
        command += ["--file", str(root / ("compose.yaml" if nginx else "docker-compose.prod.yml"))]
        if not nginx:
            command += ["--profile", "worker"]
        return self.docker([*command, *arguments], cwd=root, timeout=timeout)

    def controller(self, action):
        result = json.loads(
            self.run(
                [
                    str(self.root / "deploy/wechat-runtime-control"),
                    action,
                    "--timeout-seconds",
                    "180",
                ],
                cwd=self.root,
                timeout=190,
            )
        )
        if action == "stop" and result != {"stopped": True}:
            fail("controller_not_stopped")
        return result

    def image(self, reference):
        payload = json.loads(self.docker(["image", "inspect", reference]))
        if not isinstance(payload, list) or len(payload) != 1:
            fail("image_inspection_invalid")
        return payload[0]

    def inspect(self, identifier):
        if not CONTAINER.fullmatch(identifier):
            fail("container_identity_invalid")
        payload = json.loads(self.docker(["inspect", identifier]))
        if len(payload) != 1 or payload[0].get("Id") != identifier:
            fail("container_identity_changed")
        return payload[0]

    def containers(self):
        result = {}
        for service in APPS:
            ids = self.compose(["ps", "--all", "--quiet", service]).split()
            if len(ids) != 1:
                fail("four_app_containers_required")
            inspected = self.inspect(ids[0])
            labels = inspected.get("Config", {}).get("Labels", {})
            if labels.get("com.docker.compose.service") != service:
                fail("compose_identity_mismatch")
            result[service] = {
                "id": ids[0],
                "image": inspected["Image"],
                "running": inspected.get("State", {}).get("Running") is True,
            }
        return result

    def preflight(self):
        if (
            self.run(["git", "-C", str(self.release), "rev-parse", "HEAD"])
            != self.request["release_commit"]
        ):
            fail("checkout_commit_mismatch")
        if self.run(
            ["git", "-C", str(self.release), "status", "--porcelain", "--untracked-files=normal"]
        ):
            fail("checkout_not_clean")
        endpoint = self.docker(
            ["context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"]
        )
        if endpoint != "unix:///var/run/docker.sock":
            fail("local_docker_required")
        info = json.loads(self.docker(["info", "--format", "{{json .}}"]))
        if (
            info.get("OSType") != "linux"
            or info.get("LiveRestoreEnabled")
            or any("rootless" in str(x) for x in info.get("SecurityOptions", []))
        ):
            fail("rootful_linux_docker_required")
        image = self.image(self.request["candidate_image"])
        labels = image.get("Config", {}).get("Labels", {})
        if (
            image.get("Id") != self.request["candidate_image"]
            or labels.get("org.opencontainers.image.revision") != self.request["release_commit"]
            or labels.get("org.opencontainers.image.source")
            != "https://github.com/Tangbohu09527/CF_agent-gateway"
        ):
            fail("candidate_provenance_mismatch")
        module_code = (
            "import hashlib,json; from pathlib import Path; "
            "from cf_agent_gateway.hermes.return_bridge import site_server; "
            "p=Path(site_server.__file__).parent; "
            "print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() "
            "for n in ('site_server.py','protocol.py')}))"
        )
        packaged = json.loads(
            self.docker(
                [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "10001:10001",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    self.request["candidate_image"],
                    "python",
                    "-c",
                    module_code,
                ]
            )
        )
        source = self.release / "src/cf_agent_gateway/hermes/return_bridge"
        expected = {
            name: digest((source / name).read_bytes()) for name in ("site_server.py", "protocol.py")
        }
        if packaged != expected:
            fail("candidate_package_mismatch")
        if self.image(self.request["baseline_image"]).get("Id") != self.request["baseline_image"]:
            fail("rollback_image_missing")
        if self.image(self.request["nginx_image"]).get("Id") != self.request["nginx_image"]:
            fail("nginx_image_mismatch")
        contract = self.controller("contract")
        if contract != {
            "contract_version": 1,
            "poll_worker_service": "worker",
            "delivery_worker_service": "delivery-worker",
            "dispatch_worker_service": "dispatch-worker",
            "token_mode": "file",
            "token_container_path": "/run/secrets/cf-agent-wechat-auth-token",
        }:
            fail("controller_contract_mismatch")

    def assets(self, stage, *, writable=False, plan_sha=None):
        mounts = [(self.root, not writable), (self.nginx, not writable), (self.state_dir, False)]
        for value in (
            self.request_path,
            Path(self.request["hermes_ca_file"]),
            Path(self.request["gateway_ca_file"]),
        ):
            if not any(value == root or root in value.parents for root, _ in mounts):
                safe_path(value, file=True)
                mounts.append((value, True))
        arguments = [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:size=16m,mode=1777",
        ]
        if writable:
            # Restore existing non-root ownership on the atomically written file.
            # This capability is limited to the short-lived offline asset process.
            arguments += ["--cap-add", "CHOWN"]
        for source, readonly in mounts:
            arguments += [
                "--mount",
                f"type=bind,src={source},dst={source}" + (",readonly" if readonly else ""),
            ]
        arguments += [
            self.request["candidate_image"],
            "python",
            "-m",
            "cf_agent_gateway.hermes.return_bridge.site_server",
            stage,
            "--request",
            str(self.request_path),
            "--state-directory",
            str(self.asset_dir),
        ]
        if plan_sha:
            arguments += ["--expected-plan-sha256", plan_sha]
        result = json.loads(self.docker(arguments, timeout=120))
        if not isinstance(result, dict):
            fail("asset_result_invalid")
        return result

    def persist(self, phase, **changes):
        self.state = {**(self.state or {}), **changes, "phase": phase}
        save_json(self.state_path, self.state)

    def require_state(self, phases):
        if self.state is None or self.state.get("phase") not in phases:
            fail("stage_order_invalid")

    def plan(self):
        self.preflight()
        if self.state:
            return {
                "phase": self.state["phase"],
                "plan_sha256": self.state["plan_sha256"],
                "activation_allowed": False,
            }
        baseline = self.containers()
        if any(row["image"] != self.request["baseline_image"] for row in baseline.values()):
            fail("baseline_four_images_mismatch")
        self.config_mounts(baseline, enabled=False)
        self.nginx_definition()
        migration = self.schema_compatibility(baseline)
        certificates = self.certificate_status()
        if certificates["pending"]:
            return {
                "phase": "certificate_material_pending",
                **certificates,
                "activation_allowed": False,
            }
        artifact = self.audit_artifact_root(baseline)
        safe_path(self.state_dir)
        private_directory(self.state_dir)
        if os.name == "posix" and stat.S_IMODE(self.state_dir.stat().st_mode) != 0o700:
            fail("private_state_directory_required")
        result = self.assets("plan")
        sha = result.get("plan_sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            fail("asset_plan_digest_missing")
        self.validate_candidates(result)
        self.persist(
            "planned",
            request_sha256=self.request_hash,
            plan_sha256=sha,
            baseline=baseline,
            artifact_before=artifact,
            schema_compatibility=migration,
            deployment_id=self.request["deployment_id"],
            release_commit=self.request["release_commit"],
        )
        return {
            "phase": "planned",
            "plan_sha256": sha,
            "assets": result,
            "schema_compatibility": migration,
            "activation_allowed": False,
        }

    def schema_compatibility(self, rows):
        """Compare offline candidate head with the live read-only schema proof.

        Runtime health already calls check_database_migrations(), which compares
        the actual DB heads with that live build's expected packaged head. The
        health payload intentionally exposes status rather than the revision.
        Do not call initialize_database, Alembic upgrade, or a migration service.
        """
        gateway = rows["gateway"]
        if not gateway["running"]:
            fail("live_gateway_required_for_schema_proof")
        details = self.inspect(gateway["id"])
        if details.get("Image") != gateway["image"]:
            fail("live_gateway_image_changed")
        url = urlsplit(self.request["gateway_health_url"])
        bindings = details.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp") or []
        allowed = {"127.0.0.1", "0.0.0.0"} if url.hostname == "127.0.0.1" else {"::1", "::"}
        if not any(
            item.get("HostIp") in allowed and item.get("HostPort") == str(url.port or 80)
            for item in bindings
        ):
            fail("health_endpoint_not_live_gateway")
        code = (
            "import json; from alembic.config import Config; "
            "from alembic.script import ScriptDirectory; "
            "from cf_agent_gateway.database import _EXPECTED_MIGRATION_HEAD; "
            "c=Config(); c.set_main_option('script_location','cf_agent_gateway:migrations'); "
            "print(json.dumps({'expected':_EXPECTED_MIGRATION_HEAD,"
            "'packaged_heads':ScriptDirectory.from_config(c).get_heads()}))"
        )
        candidate = json.loads(
            self.docker(
                [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "10001:10001",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    self.request["candidate_image"],
                    "python",
                    "-c",
                    code,
                ]
            )
        )
        live = json.loads(
            self.docker(["exec", "--user", "10001:10001", gateway["id"], "python", "-c", code])
        )
        for observed in (candidate, live):
            expected = observed.get("expected")
            if (
                not isinstance(expected, str)
                or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", expected)
                or observed.get("packaged_heads") != [expected]
            ):
                fail("migration_head_not_single_or_consistent")
        if candidate["expected"] != live["expected"]:
            fail("candidate_schema_change_not_authorized")
        self.healthy_queues()  # Requires real DB/schema health, not zero fallback counters.
        if self.containers()["gateway"] != gateway:
            fail("live_gateway_changed_during_schema_proof")
        return {
            "candidate_packaged_head": candidate["expected"],
            "live_packaged_head": live["expected"],
            "live_database_proof": "matching_live_gateway_migration_schema_health",
            "schema_change_required": False,
        }

    def healthy_queues(self):
        url = self.request["gateway_health_url"].rstrip("/") + "/health/runtime"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                fail("health_redirect_rejected")

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            response = opener.open(url, timeout=5)
        except urllib.error.HTTPError as error:
            # Older endpoints may use 503 for stopped-worker degradation. Their
            # bounded JSON still must independently prove DB/schema health.
            if error.code != 503:
                raise
            response = error
        with response:
            body = response.read(2_097_153)
        if len(body) > 2_097_152:
            fail("health_limit")
        return self.validate_health(json.loads(body))

    @staticmethod
    def validate_health(value):
        for component in ("database", "migration_schema"):
            if value.get("components", {}).get(component, {}).get("status") != "ok":
                fail("database_or_schema_unhealthy")
        result = {}
        fields = {
            "dispatch": (
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
            "delivery": (
                "queued",
                "delivering",
                "failed",
                "uncertain",
                "stale_delivering",
                "missing_delivery",
            ),
        }
        for group, keys in fields.items():
            result[group] = {}
            for key in keys:
                number = value.get(group, {}).get(key)
                if type(number) is not int or number < 0:
                    fail("queue_observation_invalid")
                result[group][key] = number
        return result

    def stopped(self):
        rows = self.containers()
        if any(row["running"] for row in rows.values()):
            fail("apps_not_quiesced")
        return rows

    def config_mounts(self, rows, *, enabled):
        for service, row in rows.items():
            details = self.inspect(row["id"])
            mounts = details.get("Mounts", [])
            config = [m for m in mounts if m.get("Destination") == "/app/config/production.yaml"]
            if (
                len(config) != 1
                or config[0].get("Type") != "bind"
                or config[0].get("Source") != str(self.root / "config/production.yaml")
                or config[0].get("RW") is not False
            ):
                fail("config_mount_not_managed")
            env_names = [v.partition("=")[0] for v in details.get("Config", {}).get("Env", [])]
            if service not in {"gateway", "dispatch-worker"} and SIGNING_ENV in env_names:
                fail("signing_secret_scope_violation")
            if enabled and service in {"gateway", "dispatch-worker"}:
                ca = [
                    m for m in mounts if m.get("Destination") == "/run/cf-gateway/ca/hermes-ca.pem"
                ]
                if (
                    len(ca) != 1
                    or ca[0].get("Type") != "bind"
                    or ca[0].get("Source") != self.request["hermes_ca_file"]
                    or ca[0].get("RW") is not False
                    or env_names.count(SIGNING_ENV) != 1
                ):
                    fail("runtime_ca_or_signing_reference_missing")

    def term_wait(self, identifier, *, deadline):
        before = self.inspect(identifier)
        if before.get("State", {}).get("Running") is not True:
            return
        self.docker(["kill", "--signal", "SIGTERM", identifier])
        while self.inspect(identifier).get("State", {}).get("Running") is True:
            if time.monotonic() >= deadline:
                fail("graceful_shutdown_pending_no_force_kill")
            time.sleep(min(1.0, max(0.01, deadline - time.monotonic())))
        final = self.inspect(identifier).get("State", {})
        if final.get("OOMKilled") or final.get("ExitCode") not in (0, 143):
            fail("container_exit_requires_review")

    def quiesce(self, *, timeout=3660):
        self.require_state(
            {
                "planned",
                "quiescing",
                "quiesced",
                "applying",
                "applied",
                "starting",
                "started",
                "verified",
                "rolling_back",
                "rollback_quiescing",
                "rolled_back",
            }
        )
        rows = self.containers()
        if not any(row["running"] for row in rows.values()):
            if self.state.get("queues_quiesced") is None:
                fail("quiesced_database_evidence_missing")
            self.persist("quiesced")
            return self.receipt("quiesced")
        before = self.healthy_queues()
        self.persist("quiescing", queues_before=before)
        self.controller("stop")
        after_gate = self.containers()
        if any(after_gate[s]["running"] for s in GATED):
            fail("gate_not_closed")
        if any(after_gate[s]["id"] != rows[s]["id"] for s in APPS):
            fail("container_identity_changed")
        self.term_wait(rows["dispatch-worker"]["id"], deadline=time.monotonic() + timeout)
        observed = self.healthy_queues()
        if observed["dispatch"]["running"] or observed["delivery"]["delivering"]:
            fail("inflight_work_remains")
        # SIGTERM can revoke an inbound host claim; preserve newly ambiguous work.
        if (
            observed["dispatch"]["uncertain"] > before["dispatch"]["uncertain"]
            or observed["delivery"]["uncertain"] > before["delivery"]["uncertain"]
        ):
            self.persist("quiescing", queues_after_stop=observed)
            fail("new_uncertain_requires_review")
        self.persist("quiescing", queues_quiesced=observed)
        self.term_wait(rows["gateway"]["id"], deadline=time.monotonic() + 120)
        stopped = self.stopped()
        self.persist("quiesced", stopped_containers=stopped)
        return {**self.receipt("quiesced"), "queues": observed}

    def peer(self, required):
        peer = read_json(Path(self.request["peer_state_file"]), private=True)
        expected = {
            "schema": PEER_SCHEMA,
            "role": "hermes",
            "deployment_id": self.request["deployment_id"],
            "release_commit": self.request["release_commit"],
            "state": required,
            "activation_allowed": False,
        }
        if any(peer.get(key) != value for key, value in expected.items()):
            fail("peer_not_in_required_state")
        references = peer.get("public_references", {})
        if not isinstance(references, dict) or any(
            references.get(key) != value for key, value in self.public_references().items()
        ):
            fail("peer_public_references_mismatch")
        if required == "rolled_back" and peer.get("legacy_process_started") is not True:
            fail("legacy_hermes_not_started")
        plan = peer.get("plan_sha256")
        if not isinstance(plan, str) or not re.fullmatch(r"[0-9a-f]{64}", plan):
            fail("peer_plan_digest_required")
        recorded = self.state.get("hermes_plan_sha256") if self.state else None
        if recorded is not None and recorded != plan:
            fail("peer_plan_changed")
        if self.state is not None and recorded is None:
            self.persist(self.state["phase"], hermes_plan_sha256=plan)
        return peer

    def signing_file(self):
        path = Path(self.request["signing_env_file"])
        safe_path(path)
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700)
        safe_path(path.parent)
        if path.exists():
            safe_path(path, file=True, private=True)
            content = path.read_bytes()
            if not re.fullmatch(rb"CF_GATEWAY_ARTIFACT_RETURN_KEY=[A-Za-z0-9_-]{43}\n", content):
                fail("existing_signing_file_unknown")
            return
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((SIGNING_ENV + "=" + secrets.token_urlsafe(32) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())

    def apply(self):
        self.require_state({"quiesced", "applying", "applied"})
        self.stopped()
        self.peer("quiesced")
        self.persist("applying")
        self.prepare_artifact_root()
        self.signing_file()
        self.assets("apply", writable=True, plan_sha=self.state["plan_sha256"])
        self.check_rendered(self.request["candidate_image"])
        self.nginx_test()
        self.persist("applied")
        return self.receipt("applied")

    def check_rendered(self, image):
        value = json.loads(self.compose(["config", "--format", "json"]))
        services = value.get("services", {})
        if any(services.get(service, {}).get("image") != image for service in APPS):
            fail("rendered_four_images_mismatch")
        for service, definition in services.items():
            environment = definition.get("environment", {})
            if service not in {"gateway", "dispatch-worker"} and SIGNING_ENV in environment:
                fail("signing_secret_scope_violation")
        return value

    def nginx_definition(self):
        value = json.loads(self.compose(["config", "--format", "json"], nginx=True))
        services = value.get("services", {})
        if len(services) != 1:
            fail("nginx_single_service_required")
        name, definition = next(iter(services.items()))
        if definition.get("user") != "101:101":
            fail("nginx_existing_service_identity_mismatch")
        if self.image(definition.get("image", "")).get("Id") != self.request["nginx_image"]:
            fail("nginx_image_mismatch")
        return name, definition

    def validate_candidates(self, result):
        candidates = {}
        for item in result.get("candidates", []):
            candidate = safe_path(item["candidate_file"], file=True, private=True)
            if self.asset_dir not in candidate.parents:
                fail("candidate_outside_private_journal")
            candidates[item["path"]] = candidate
        compose_candidate = candidates.get(str(self.root / "docker-compose.prod.yml"))
        nginx_candidate = candidates.get(str(self.nginx / "nginx.conf"))
        if compose_candidate is None or nginx_candidate is None:
            fail("required_candidate_missing")
        value = json.loads(
            self.docker(
                [
                    "compose",
                    "--project-directory",
                    str(self.root),
                    "--env-file",
                    str(self.root / ".env"),
                    "--file",
                    str(compose_candidate),
                    "--profile",
                    "worker",
                    "config",
                    "--no-env-resolution",
                    "--format",
                    "json",
                ],
                cwd=self.root,
            )
        )
        services = value.get("services", {})
        if any(
            services.get(name, {}).get("image") != self.request["candidate_image"] for name in APPS
        ):
            fail("candidate_compose_images_mismatch")
        for name, definition in services.items():
            values = definition.get("env_file", [])
            references = [item if isinstance(item, str) else item.get("path") for item in values]
            required = name in {"gateway", "dispatch-worker"}
            if (self.request["signing_env_file"] in references) != required:
                fail("candidate_signing_scope_violation")
        self.nginx_test(candidate=nginx_candidate)

    @contextlib.contextmanager
    def nginx_check_file(self, candidate):
        if candidate is None:
            yield None
            return
        # Private journal files stay 0600/root. Give only the disposable syntax
        # checker a separate 0400 file owned by its actual service identity.
        path = self.state_dir / (".nginx-check-" + secrets.token_hex(16))
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(candidate.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o400)
            os.chown(path, 101, 101)
            yield path
        finally:
            if path.exists():
                path.unlink()

    def nginx_test(self, *, candidate=None):
        with self.nginx_check_file(candidate) as readable_candidate:
            self._nginx_test(candidate=readable_candidate)

    def _nginx_test(self, *, candidate=None):
        _, definition = self.nginx_definition()
        arguments = [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "101:101",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/var/cache/nginx:uid=101,gid=101,mode=0700",
            "--tmpfs",
            "/var/run:uid=101,gid=101,mode=0700",
            "--tmpfs",
            "/tmp:uid=101,gid=101,mode=0700",
        ]
        for mount in definition.get("volumes", []):
            if not isinstance(mount, dict) or mount.get("type") != "bind":
                fail("nginx_bind_mount_required")
            source = safe_path(mount["source"], service_uid=101)
            if candidate is not None and source == self.nginx / "nginx.conf":
                source = candidate
            if str(source) == "/var/run/docker.sock" or not mount.get("target", "").startswith(
                "/etc/nginx/"
            ):
                fail("nginx_mount_scope_invalid")
            arguments += ["--mount", f"type=bind,src={source},dst={mount['target']},readonly"]
        arguments += ["--entrypoint", "nginx", self.request["nginx_image"], "-t"]
        self.docker(arguments, timeout=60)

    def start(self, *, old=False):
        self.require_state(
            {"rolled_back", "old_starting", "old_started"}
            if old
            else {"applied", "starting", "started"}
        )
        self.peer("rolled_back" if old else "tls_ready")
        completed_phase = "old_started" if old else "started"
        if self.state["phase"] == completed_phase:
            expected_image = self.request["baseline_image" if old else "candidate_image"]
            self.check_rendered(expected_image)
            rows = self.containers()
            if (
                any(row["image"] != expected_image for row in rows.values())
                or any(rows[service]["running"] for service in GATED)
                or any(not rows[service]["running"] for service in ("gateway", "dispatch-worker"))
            ):
                fail("runtime_receipt_verification_failed")
            self.config_mounts(rows, enabled=not old)
            self.healthy_queues()
            # The original successful start is durable. A lost receipt only
            # permits read-only verification and exact receipt re-emission.
            return self.receipt("rolled_back" if old else "applied")
        # This aggregate was captured with the database/schema healthy before API stop.
        queues = self.state.get("queues_quiesced", {})
        if not self.dispatch_start_safe(queues):
            fail("queued_work_blocks_dispatch_start")
        image = self.request["baseline_image" if old else "candidate_image"]
        self.check_rendered(image)
        self.nginx_test()
        self.persist("old_starting" if old else "starting")
        self.compose(["create", "--no-deps", "--no-build", "--pull", "never", *APPS], timeout=180)
        rows = self.containers()
        if any(row["image"] != image for row in rows.values()) or any(
            rows[s]["running"] for s in GATED
        ):
            fail("prepared_container_boundary_invalid")
        self.docker(["start", rows["gateway"]["id"]])
        deadline = time.monotonic() + 90
        while True:
            try:
                health = self.healthy_queues()
                break
            except (OSError, ValueError, EntryError):
                if time.monotonic() >= deadline:
                    fail("gateway_readiness_failed_gate_closed")
                time.sleep(1)
        if not self.dispatch_start_safe(health) or health["delivery"]["delivering"]:
            fail("live_queue_blocks_dispatch_start")
        self.docker(["start", rows["dispatch-worker"]["id"]])
        # A single-file bind keeps the old inode after atomic replacement. Recreate
        # only this separately owned frontend, never reload an old mounted inode.
        name, _ = self.nginx_definition()
        self.compose(
            [
                "up",
                "--detach",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                name,
            ],
            nginx=True,
            timeout=120,
        )
        identifiers = self.compose(["ps", "--quiet", name], nginx=True).split()
        if len(identifiers) != 1:
            fail("nginx_running_container_required")
        if self.inspect(identifiers[0]).get("Image") != self.request["nginx_image"]:
            fail("running_nginx_image_mismatch")
        self.persist("old_started" if old else "started")
        return self.receipt("rolled_back" if old else "applied")

    @staticmethod
    def dispatch_start_safe(queues):
        # FAILED is automatically claimable too; reconciliation may also write old
        # business outcomes. Leave these untouched until separately classified.
        return all(
            queues.get("dispatch", {}).get(key) == 0
            for key in (
                "queued",
                "running",
                "stale_running",
                "failed",
                "reconciliation_backlog",
                "reconciliation_deferred",
                "missing_delivery",
            )
        )

    def verify(self):
        self.require_state({"started", "verified"})
        self.peer("tls_ready")
        self.assets("verify-assets", plan_sha=self.state["plan_sha256"])
        rows = self.containers()
        if (
            any(row["image"] != self.request["candidate_image"] for row in rows.values())
            or any(rows[s]["running"] for s in GATED)
            or any(not rows[s]["running"] for s in ("gateway", "dispatch-worker"))
        ):
            fail("verification_container_boundary_invalid")
        self.config_mounts(rows, enabled=True)
        self.verify_runtime_bytes(rows)
        self.healthy_queues()
        diagnostic = json.loads(
            self.compose(
                [
                    "exec",
                    "-T",
                    "gateway",
                    "python",
                    "-m",
                    "cf_agent_gateway.hermes.diagnose",
                    "--timeout",
                    "5",
                ]
            )
        )
        if (
            diagnostic.get("network") != "tls_verified"
            or diagnostic.get("ok") is not True
            or diagnostic.get("application") != "not_checked"
        ):
            fail("hermes_tls_unverified")
        target = urlsplit(self.request["gateway_origin"])
        context = ssl.create_default_context(cafile=self.request["gateway_ca_file"])
        with (
            socket.create_connection((target.hostname, target.port or 443), timeout=5) as connected,
            context.wrap_socket(connected, server_hostname=target.hostname),
        ):
            pass
        self.persist("verified")
        return {
            **self.receipt("tls_ready"),
            "gateway_to_hermes_tls": "verified",
            "gateway_https_tls": "verified",
            "ack": "pending_authorized_new_task",
            "wechat_receipt": "not_attempted",
        }

    def container_python(self, row, code, *, writable=False):
        if row["running"]:
            return self.docker(["exec", "--user", "10001:10001", row["id"], "python", "-c", code])
        return self.docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "10001:10001",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--volumes-from",
                row["id"] + (":rw" if writable else ":ro"),
                self.request["candidate_image"],
                "python",
                "-c",
                code,
            ]
        )

    @staticmethod
    def artifact_directory_script(*, prepare=False):
        common = """
import json, os, stat
from pathlib import Path
from cf_agent_gateway.config import load_settings
p = Path(load_settings('/app/config/production.yaml').artifact.storage_root)
base = Path('/var/lib/cf-agent-gateway')
assert p.is_absolute() and '..' not in p.parts and base in p.parents
for part in (*reversed(p.parents), p):
    try:
        meta = part.lstat()
    except FileNotFoundError:
        assert part == p
        continue
    assert stat.S_ISDIR(meta.st_mode) and not stat.S_ISLNK(meta.st_mode)
    if part == p:
        assert meta.st_uid == 10001 and meta.st_gid == 10001
"""
        if prepare:
            common += """
if not p.exists():
    p.mkdir(mode=0o700)
else:
    p.chmod(0o700)
"""
        return (
            common
            + """
meta = p.lstat() if p.exists() else None
print(json.dumps({'path':str(p),'exists':meta is not None,
                  'uid':meta.st_uid if meta else None,
                  'gid':meta.st_gid if meta else None,
                  'mode':stat.S_IMODE(meta.st_mode) if meta else None}))
"""
        )

    def audit_artifact_root(self, rows):
        observations, sources = [], []
        for service in ("gateway", "dispatch-worker", "delivery-worker"):
            details = self.inspect(rows[service]["id"])
            state = [
                m
                for m in details.get("Mounts", [])
                if m.get("Destination") == "/var/lib/cf-agent-gateway"
            ]
            if (
                len(state) != 1
                or state[0].get("RW") is not True
                or state[0].get("Type") not in {"bind", "volume"}
            ):
                fail("artifact_state_mount_invalid")
            sources.append((state[0]["Type"], state[0].get("Source"), state[0].get("Name")))
            observed = json.loads(
                self.container_python(rows[service], self.artifact_directory_script())
            )
            if set(observed) != {"path", "exists", "uid", "gid", "mode"}:
                fail("artifact_metadata_invalid")
            observations.append(observed)
        if any(value != sources[0] for value in sources) or any(
            value != observations[0] for value in observations
        ):
            fail("artifact_mount_or_directory_mismatch")
        return observations[0]

    def prepare_artifact_root(self):
        rows = self.stopped()
        observed = self.audit_artifact_root(rows)
        expected = self.state.get("artifact_before")
        if not isinstance(expected, dict) or observed.get("path") != expected.get("path"):
            fail("artifact_plan_missing_or_changed")
        if observed != expected and not (
            observed.get("exists") is True
            and observed.get("mode") == 0o700
            and observed.get("uid") == 10001
            and observed.get("gid") == 10001
        ):
            fail("artifact_directory_changed_since_plan")
        prepared = json.loads(
            self.container_python(
                rows["gateway"], self.artifact_directory_script(prepare=True), writable=True
            )
        )
        if (
            prepared.get("path") != expected["path"]
            or prepared.get("mode") != 0o700
            or prepared.get("uid") != 10001
            or prepared.get("gid") != 10001
        ):
            fail("artifact_private_preparation_failed")
        self.persist(
            self.state["phase"],
            artifact_prepared=prepared,
            artifact_rollback_policy="retain_private_root_and_contents",
        )

    def verify_runtime_bytes(self, rows):
        expected_config = digest((self.root / "config/production.yaml").read_bytes())
        expected_module = digest(
            (self.release / "src/cf_agent_gateway/hermes/return_bridge/protocol.py").read_bytes()
        )
        code = (
            "import hashlib,json; from pathlib import Path; import cf_agent_gateway; "
            "p=Path(cf_agent_gateway.__file__).parent; "
            "print(json.dumps({'config':hashlib.sha256("
            "Path('/app/config/production.yaml').read_bytes()).hexdigest(),"
            "'module':hashlib.sha256((p/'hermes/return_bridge/protocol.py')"
            ".read_bytes()).hexdigest()}))"
        )
        for row in rows.values():
            observed = json.loads(self.container_python(row, code))
            if observed != {"config": expected_config, "module": expected_module}:
                fail("container_config_or_module_bytes_mismatch")
        name, definition = self.nginx_definition()
        identifiers = self.compose(["ps", "--quiet", name], nginx=True).split()
        if (
            len(identifiers) != 1
            or self.inspect(identifiers[0]).get("Image") != self.request["nginx_image"]
        ):
            fail("running_nginx_image_mismatch")
        mounts = [
            m
            for m in definition.get("volumes", [])
            if m.get("source") == str(self.nginx / "nginx.conf")
        ]
        if len(mounts) != 1:
            fail("nginx_config_mount_missing")
        result = self.docker(["exec", identifiers[0], "sha256sum", mounts[0]["target"]]).split()
        if not result or result[0] != digest((self.nginx / "nginx.conf").read_bytes()):
            fail("nginx_mounted_bytes_mismatch")
        self.artifact_probe(rows)

    def artifact_probe(self, rows):
        # New non-business bytes only. Fixed scripts never read an existing artifact.
        filename = ".return-volume-check-" + secrets.token_hex(16)
        data = secrets.token_hex(32)
        expected = digest(data.encode())
        setup = (
            "from pathlib import Path; import os,stat,hashlib; "
            "from cf_agent_gateway.config import load_settings; "
            "p=Path(load_settings('/app/config/production.yaml').artifact.storage_root); "
            "assert p.is_absolute() and not p.is_symlink(); s=p.stat(); "
            "assert s.st_uid==10001 and stat.S_IMODE(s.st_mode)==0o700; f=p/"
            + repr(filename)
            + "; "
        )
        create = (
            setup
            + "d=os.open(f,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600); "
            "os.write(d," + repr(data.encode()) + "); os.fsync(d); os.close(d); print('created')"
        )
        if self.container_python(rows["gateway"], create) != "created":
            fail("artifact_probe_create_failed")
        try:
            read = (
                setup + "s=f.lstat(); assert stat.S_ISREG(s.st_mode) "
                "and s.st_nlink==1 and s.st_uid==10001; "
                "print(hashlib.sha256(f.read_bytes()).hexdigest())"
            )
            for service in ("gateway", "dispatch-worker", "delivery-worker"):
                if self.container_python(rows[service], read) != expected:
                    fail("artifact_shared_storage_mismatch")
        finally:
            remove = (
                setup
                + "assert hashlib.sha256(f.read_bytes()).hexdigest()=="
                + repr(expected)
                + "; f.unlink(); print('removed')"
            )
            if self.container_python(rows["gateway"], remove) != "removed":
                fail("artifact_probe_cleanup_requires_review")

    def certificate_status(self):
        pending = []
        for field in ("hermes_ca_file", "gateway_ca_file"):
            if self.request[field] is None:
                pending.append(field)
                continue
            candidate = Path(self.request[field])
            if not candidate.is_file():
                pending.append(field)
            else:
                safe_path(candidate, file=True)
                try:
                    ssl.create_default_context(cafile=str(candidate))
                except (OSError, ssl.SSLError):
                    pending.append(field + "_invalid_ca")
        return {
            "pending": pending,
            "signing": "use_existing_approved_CA_process_on_server",
            "next_step": (
                "Provide verified Gateway and Hermes public CA references; sign the "
                "Windows-generated CSR with the existing approved CA process on this server, "
                "retaining the CA private key here. "
                "Re-run plan after verified certificate issuance."
            ),
            "activation_allowed": False,
        }

    def rollback(self):
        self.require_state(
            {
                "applied",
                "starting",
                "started",
                "verified",
                "quiesced",
                "applying",
                "rolling_back",
                "rolled_back",
            }
        )
        if any(row["running"] for row in self.containers().values()):
            self.quiesce()
        self.stopped()
        self.persist("rolling_back")
        self.assets("rollback", writable=True, plan_sha=self.state["plan_sha256"])
        self.check_rendered(self.request["baseline_image"])
        self.nginx_test()
        self.persist("rolled_back")
        return {
            **self.receipt("rolled_back"),
            "restart": "await_hermes_rolled_back_peer",
            "signing_file": "retained_private_no_rotation",
        }

    def receipt(self, phase):
        value = {
            "schema": PEER_SCHEMA,
            "role": "server",
            "deployment_id": self.request["deployment_id"],
            "release_commit": self.request["release_commit"],
            "state": phase,
            "plan_sha256": self.state["plan_sha256"],
            "public_references": self.public_references(),
            "activation_allowed": False,
        }
        save_json(self.state_dir / "peer-receipt.json", value)
        return value

    def public_references(self):
        return {
            **{
                key: self.request[key]
                for key in (
                    "hermes_origin",
                    "gateway_origin",
                    "profile_reference",
                    "profile_revision",
                )
            },
            "hermes_ca_sha256": digest(Path(self.request["hermes_ca_file"]).read_bytes()),
            "gateway_ca_sha256": digest(Path(self.request["gateway_ca_file"]).read_bytes()),
        }


@contextlib.contextmanager
def operation_lock(directory):
    import fcntl

    safe_path(directory)
    private_directory(directory)
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        fail("private_state_directory_required")
    path = directory / ".server-operation.lock"
    safe_path(path)
    marker = b"cf-return-site/server-operation/v1\n"
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        os.write(descriptor, marker)
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except FileExistsError:
        safe_path(path, file=True, private=True)
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r+b") as stream:
        if os.fstat(stream.fileno()).st_nlink != 1 or stream.read() != marker:
            fail("linked_operation_lock")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fail("operation_busy")
        yield


def init_request(options, *, commands=None):
    """Read public Docker identities and save references; no model or Token inputs."""
    if not COMMIT.fullmatch(options.release_commit) or not IMAGE.fullmatch(options.candidate_image):
        fail("fixed_release_identity_required")
    if not options.release_root:
        fail("fixed_checkout_path_required")
    root = safe_path(options.gateway_root)
    nginx = safe_path(options.nginx_root)
    release = safe_path(options.release_root)
    command = commands or Commands()
    entry = ServerEntry.__new__(ServerEntry)
    entry.commands, entry.root, entry.nginx = command, root, nginx
    current = entry.containers()
    images = {row["image"] for row in current.values()}
    if len(images) != 1 or any(not IMAGE.fullmatch(image) for image in images):
        fail("baseline_four_images_mismatch")
    rendered = json.loads(entry.compose(["config", "--format", "json"], nginx=True))
    services = rendered.get("services", {})
    if len(services) != 1:
        fail("nginx_single_service_required")
    name = next(iter(services))
    nginx_ids = entry.compose(["ps", "--all", "--quiet", name], nginx=True).split()
    if len(nginx_ids) != 1:
        fail("nginx_existing_container_required")
    nginx_image = entry.inspect(nginx_ids[0])["Image"]
    if not IMAGE.fullmatch(nginx_image):
        fail("nginx_image_mismatch")
    gateway = entry.inspect(current["gateway"]["id"])
    bindings = gateway.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp")
    if not isinstance(bindings, list) or len(bindings) != 1:
        fail("gateway_loopback_port_reference_required")
    binding = bindings[0]
    if binding.get("HostIp") not in {"127.0.0.1", "0.0.0.0"} or not re.fullmatch(
        r"[0-9]{1,5}", str(binding.get("HostPort", ""))
    ):
        fail("gateway_loopback_port_reference_required")
    port = int(binding["HostPort"])
    if not 1 <= port <= 65535:
        fail("gateway_loopback_port_reference_required")
    references = {}
    deployment = options.deployment_id or "return-" + options.release_commit[:12]
    if options.peer_public:
        peer_value = read_json(safe_path(options.peer_public, file=True))
        if (
            peer_value.get("schema") != PEER_SCHEMA
            or peer_value.get("role") != "hermes"
            or peer_value.get("release_commit") != options.release_commit
            or peer_value.get("activation_allowed") is not False
        ):
            fail("peer_public_reference_mismatch")
        deployment = peer_value["deployment_id"]
        references = peer_value.get("public_references", {})
    for key in ("hermes_origin", "gateway_origin"):
        if references.get(key) is not None:
            url = urlsplit(references[key])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
            ):
                fail("peer_https_reference_invalid")
    state = safe_path(
        options.state_directory or str(Path("/var/lib/cf-agent-gateway-return") / deployment)
    )
    value = {
        "schema": SCHEMA,
        "release_commit": options.release_commit,
        "candidate_image": options.candidate_image,
        "baseline_image": next(iter(images)),
        "gateway_root": str(root),
        "nginx_root": str(nginx),
        "hermes_origin": references.get("hermes_origin"),
        "hermes_ca_file": str(safe_path(options.hermes_ca_file))
        if options.hermes_ca_file
        else None,
        "gateway_origin": references.get("gateway_origin"),
        "gateway_ca_file": str(safe_path(options.gateway_ca_file))
        if options.gateway_ca_file
        else None,
        "profile_reference": references.get("profile_reference") or "default",
        "profile_revision": references.get("profile_revision") or 1,
        "signing_env_file": str(root / "secrets/artifact-return.env"),
        "release_root": str(release),
        "nginx_image": nginx_image,
        "state_directory": str(state),
        "deployment_id": deployment,
        "gateway_health_url": f"http://127.0.0.1:{port}",
        "peer_state_file": str(safe_path(options.peer_public)) if options.peer_public else None,
    }
    output = safe_path(options.request)
    if not output.parent.exists():
        if output.parent != state and state not in output.parent.parents:
            fail("request_parent_missing_outside_private_state")
        private_directory(output.parent)
    if output.exists():
        before = read_json(output, private=True)
        if before != value:
            if set(before) != set(value) or any(
                before[key] is not None and before[key] != item for key, item in value.items()
            ):
                fail("existing_request_differs")
            original = output.read_bytes()
            backup = output.with_name(output.name + ".pending-" + digest(original) + ".json")
            if not backup.exists():
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            elif backup.read_bytes() != original:
                fail("request_backup_conflict")
            if output.read_bytes() != original:
                fail("request_changed")
            save_json(output, value)
    else:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
    return {
        "request": str(output),
        "missing_public_references": [key for key, item in value.items() if item is None],
        "activation_allowed": False,
        "configuration_changed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES, nargs="?", default="plan")
    parser.add_argument("--request", required=True)
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--apply-authorized", action="store_true")
    parser.add_argument("--release-root")
    parser.add_argument("--gateway-root", default="/opt/cf-agent-gateway")
    parser.add_argument("--nginx-root", default="/opt/cf-agent-gateway-https")
    parser.add_argument("--state-directory")
    parser.add_argument("--deployment-id")
    parser.add_argument("--peer-public")
    parser.add_argument("--hermes-ca-file")
    parser.add_argument("--gateway-ca-file")
    options = parser.parse_args(argv)
    try:
        if os.name != "posix" or os.geteuid() != 0:
            fail("existing_root_management_session_required")
        if (
            options.stage not in {"plan", "certificate-status", "init-request"}
            and not options.apply_authorized
        ):
            fail("explicit_apply_authorization_required")
        os.umask(0o077)
        if options.stage == "init-request":
            print(json.dumps(init_request(options), sort_keys=True))
            return 0
        pending = read_json(safe_path(options.request, file=True))
        missing = [
            key
            for key in (
                "hermes_origin",
                "gateway_origin",
                "hermes_ca_file",
                "gateway_ca_file",
                "peer_state_file",
            )
            if pending.get(key) is None
        ]
        if missing:
            print(
                json.dumps(
                    {
                        "phase": "site_references_pending",
                        "missing_public_references": missing,
                        "activation_allowed": False,
                    }
                )
            )
            return 1
        entry = ServerEntry(
            options.request,
            release_commit=options.release_commit,
            candidate_image=options.candidate_image,
        )
        with operation_lock(entry.state_dir):
            entry.preflight()
            if options.stage == "start-old":
                result = entry.start(old=True)
            else:
                result = getattr(entry, options.stage.replace("-", "_"))()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (EntryError, OSError, ValueError, KeyError, TypeError):
        import sys

        error = sys.exception()
        code = (
            str(error)
            if isinstance(error, EntryError)
            else "operation_failed_details_retained_privately"
        )
        print(json.dumps({"error": code, "activation_allowed": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
