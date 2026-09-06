#!/usr/bin/env python3
"""Debian 13 first installation. No upgrades, restore, or implicit Gate opening."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import grp
import json
import os
import pwd
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path("/opt/cf-agent-gateway")
STATE = Path("/var/lib/cf-agent-gateway-install")
DB_ROOT = Path("/var/lib/cf-agent-gateway-postgres")
WECHAT_SOURCE = STATE / "wechat-source"
TOKEN = Path("/srv/storage/cf-agent-wechat/secrets/auth-token")
REPO = "https://github.com/Tangbohu09527/CF_agent-gateway.git"
WECHAT_REPO = "https://github.com/Tangbohu09527/CF_agent-wechat.git"
COMPOSE = ROOT / "docker-compose.prod.yml"
DB_COMPOSE = ROOT / "deploy/postgres/compose.yml"
SERVICE = (10001, 10001)
STAGES = (
    "system",
    "system-packages",
    "controller",
    "configure",
    "build",
    "database",
    "migrate",
    "initialize",
    "start",
    "diagnose",
    "boot-service",
)


class InstallError(RuntimeError):
    pass


def fail(code: str) -> None:
    raise InstallError(code)


def run(
    args: list[str | Path], *, timeout: int = 300, cwd: Path | None = None, data: str | None = None
) -> str:
    # Do not inherit Compose overrides, proxy credentials, Python injection or
    # caller-controlled Docker endpoints. Output can contain secrets: never echo.
    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "USER": "root",
        "GIT_TERMINAL_PROMPT": "0",
    }
    process = subprocess.Popen(
        [str(arg) for arg in args],
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        fail("command_timeout:" + Path(args[0]).name)
    if process.returncode:
        fail("command_failed:" + Path(args[0]).name)
    return output.strip()


def safe_parents(path: Path, *, input_file: bool = False) -> None:
    if not path.is_absolute() or ".." in path.parts:
        fail("absolute_normalized_path_required")
    for parent in (path, *path.parents):
        if not parent.exists() and not parent.is_symlink():
            continue
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail("symlink_conflict:" + str(parent))
        # /tmp is allowed only for protected administrator-supplied input files.
        sticky_tmp = input_file and parent == Path("/tmp") and info.st_uid == 0
        if info.st_mode & 0o022 and not sticky_tmp:
            fail("writable_path_conflict:" + str(parent))


def directory(path: Path, mode: int = 0o700) -> None:
    safe_parents(path)
    if path.exists():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0):
            fail("directory_owner_conflict:" + str(path))
        if stat.S_IMODE(info.st_mode) != mode:
            fail("directory_mode_conflict:" + str(path))
    else:
        path.mkdir(mode=mode)
        os.chmod(path, mode)


def regular(path: Path, *, owner: tuple[int, int] = (0, 0), mode: int = 0o600) -> bytes:
    safe_parents(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_uid, info.st_gid) != owner
            or stat.S_IMODE(info.st_mode) != mode
        ):
            fail("file_permission_conflict:" + str(path))
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def once(
    path: Path, content: str | bytes, *, owner: tuple[int, int] = (0, 0), mode: int = 0o600
) -> None:
    expected = content.encode() if isinstance(content, str) else content
    safe_parents(path)
    if path.exists():
        if regular(path, owner=owner, mode=mode) != expected:
            fail("existing_asset_differs:" + str(path))
        return
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), *owner)
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def encoded(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def read_json(path: Path) -> dict:
    result = json.loads(regular(path))
    if not isinstance(result, dict):
        fail("json_object_required:" + str(path))
    return result


def administrator_input(path: str, manager: str) -> bytes:
    target = Path(path)
    safe_parents(target, input_file=True)
    info = target.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in (0, pwd.getpwnam(manager).pw_uid)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 1024 * 1024
    ):
        fail("input_requires_owner_only_regular_file")
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if os.fstat(descriptor) != info:
            fail("input_changed_during_open")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def secret_input(path: str, manager: str) -> str:
    value = administrator_input(path, manager).decode().rstrip("\r\n")
    if not 16 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        fail("secret_requires_16_to_4096_printable_nonspace_characters")
    return value


def dotenv(values: dict[str, str]) -> str:
    def quote(value: str) -> str:
        if "\n" in value or "\r" in value or "\0" in value:
            fail("invalid_environment_value")
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    return "".join(f"{key}={quote(value)}\n" for key, value in sorted(values.items()))


def fixed_checkout(path: Path, repo: str, revision: str, *, mode: int) -> None:
    safe_parents(path)
    if not path.exists():
        staging = Path(tempfile.mkdtemp(prefix=path.name + "-pending-", dir=path.parent))
        run(["git", "-c", "core.hooksPath=/dev/null", "init", staging])
        run(["git", "-C", staging, "remote", "add", "origin", repo])
        run(
            [
                "git",
                "-C",
                staging,
                "-c",
                "core.hooksPath=/dev/null",
                "fetch",
                "--depth",
                "1",
                "origin",
                revision,
            ],
            timeout=600,
        )
        run(
            [
                "git",
                "-C",
                staging,
                "-c",
                "core.hooksPath=/dev/null",
                "checkout",
                "--detach",
                "FETCH_HEAD",
            ]
        )
        if run(["git", "-C", staging, "rev-parse", "HEAD"]) != revision:
            fail("fetched_commit_mismatch")
        os.chmod(staging, mode)
        # Single installer lock protects this rename; unknown existing assets
        # are never removed. Failed staging directories remain for inspection.
        if path.exists() or path.is_symlink():
            fail("checkout_appeared_concurrently")
        staging.rename(path)
    directory(path, mode)
    if not (path / ".git").is_dir() or (path / ".git").is_symlink():
        fail("standalone_checkout_required")
    if run(["git", "-C", path, "remote", "get-url", "origin"]) != repo:
        fail("checkout_origin_conflict")
    if run(["git", "-C", path, "rev-parse", "HEAD"]) != revision:
        fail("checkout_version_conflict_use_separate_upgrade")
    if run(["git", "-C", path, "status", "--porcelain", "--untracked-files=normal"]):
        fail("checkout_modified_preserved")


def platform() -> None:
    if os.geteuid() != 0:
        fail("initial_installation_requires_sudo_or_root")
    release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    if (
        release.get("ID", "").strip('"') != "debian"
        or release.get("VERSION_ID", "").strip('"') != "13"
        or run(["dpkg", "--print-architecture"]) != "amd64"
    ):
        fail("only_debian_13_amd64_supported")


def manager_identity(manager: str) -> None:
    account = pwd.getpwnam(manager)
    groups = os.getgrouplist(manager, account.pw_gid)
    if account.pw_uid == 0 or account.pw_uid == SERVICE[0] or SERVICE[1] in groups:
        fail("manager_and_service_identity_must_be_separate")
    for name in ("root", "docker"):
        with contextlib.suppress(KeyError):
            if grp.getgrnam(name).gr_gid in groups:
                fail("manager_must_not_join_root_or_docker")


def docker(args: list[str | Path], **kwargs) -> str:
    return run(["docker", "--context", "default", *args], **kwargs)


def docker_check() -> None:
    if docker(["context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"]) != (
        "unix:///var/run/docker.sock"
    ):
        fail("local_rootful_docker_required")
    info = json.loads(docker(["info", "--format", "{{json .}}"]))
    if info.get("LiveRestoreEnabled") or any("rootless" in x for x in info["SecurityOptions"]):
        fail("docker_live_restore_or_rootless_unsupported")
    if not re.fullmatch(r"v?2\..+", docker(["compose", "version", "--short"])):
        fail("compose_v2_required")


def compose(args: list[str | Path], **kwargs) -> str:
    return docker(
        ["compose", "--env-file", ROOT / ".env", "--file", COMPOSE, "--profile", "worker", *args],
        cwd=ROOT,
        **kwargs,
    )


def db_compose(args: list[str | Path], **kwargs) -> str:
    return docker(
        ["compose", "--env-file", DB_ROOT / ".env", "--file", DB_COMPOSE, *args],
        cwd=DB_ROOT,
        **kwargs,
    )


def contract() -> None:
    result = json.loads(run([ROOT / "deploy/wechat-runtime-control", "contract"]))
    if result != {
        "contract_version": 1,
        "poll_worker_service": "worker",
        "delivery_worker_service": "delivery-worker",
        "dispatch_worker_service": "dispatch-worker",
        "token_mode": "file",
        "token_container_path": "/run/secrets/cf-agent-wechat-auth-token",
    }:
        fail("static_controller_contract_mismatch")


def validate_token_and_network() -> None:
    content = regular(TOKEN, owner=SERVICE, mode=stat.S_IMODE(TOKEN.lstat().st_mode))
    if (
        stat.S_IMODE(TOKEN.lstat().st_mode) not in (0o400, 0o600)
        or not 1 <= len(content) <= 4096
        or any(byte < 33 or byte > 126 for byte in content)
    ):
        fail("wechat_bootstrap_token_invalid")
    info = json.loads(docker(["network", "inspect", "cf-internal"]))[0]
    if info["Driver"] != "bridge" or info["Scope"] != "local" or info["Internal"]:
        fail("cf_internal_network_conflict_run_wechat_bootstrap")


def validate_inputs(raw: dict) -> dict:
    allowed = {
        "version",
        "hermes_url",
        "hermes_model",
        "hermes_api_key_file",
        "initial_identity_file",
        "database",
        "python_image",
        "postgres_image",
    }
    if set(raw) - allowed or raw.get("version") != 1 or isinstance(raw.get("version"), bool):
        fail("installation_inputs_version_or_unknown_key")
    for key in ("hermes_url", "hermes_model", "hermes_api_key_file", "initial_identity_file"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            fail("missing_input:" + key)
    url = urlsplit(raw["hermes_url"])
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.port == 0
        or any(c.isspace() or ord(c) < 32 for c in raw["hermes_url"])
    ):
        fail("invalid_hermes_url")
    if url.hostname in ("localhost", "127.0.0.1", "::1"):
        fail("hermes_url_must_reach_ai_host_from_container")
    if url.path.rstrip("/").endswith("/v1"):
        fail("hermes_url_must_not_include_v1")
    database = raw.get("database")
    if not isinstance(database, dict) or set(database) - {"mode", "external_url_file"}:
        fail("database_input_invalid")
    if database.get("mode") not in ("managed", "external"):
        fail("database_mode_required")
    if database["mode"] == "managed" and set(database) != {"mode"}:
        fail("managed_database_does_not_accept_external_credentials")
    if database["mode"] == "external" and not isinstance(database.get("external_url_file"), str):
        fail("external_url_file_required")
    result = dict(raw)
    result.setdefault("python_image", "python:3.12-slim-bookworm")
    result.setdefault("postgres_image", "postgres:16-bookworm")
    for key in ("python_image", "postgres_image"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]+", result[key]):
            fail("invalid_image_reference:" + key)
    return result


def configure(options) -> None:
    if not options.inputs:
        fail("configure_requires_inputs_file")
    raw = json.loads(administrator_input(options.inputs, options.manager))
    settings = validate_inputs(raw)
    hermes_key = secret_input(settings["hermes_api_key_file"], options.manager)
    identity = administrator_input(settings["initial_identity_file"], options.manager)
    image_id = read_json(STATE / "gateway-image.json")["image_id"]
    # Validate the real application schema before any configuration is committed.
    # This image-only check requires neither database nor network and echoes no input.
    docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--interactive",
            "--entrypoint",
            "python",
            image_id,
            "-c",
            "import json,sys; from cf_agent_gateway.initial_identity import "
            "InitialIdentityConfig; x=json.load(sys.stdin); "
            "c=InitialIdentityConfig.model_validate(x['identity']); "
            "assert c.profile.model == x['model']",
        ],
        data=json.dumps({"identity": json.loads(identity), "model": settings["hermes_model"]}),
    )
    if settings["database"]["mode"] == "external":
        database_url = secret_input(settings["database"]["external_url_file"], options.manager)
        parsed = urlsplit(database_url)
        if (
            parsed.scheme != "postgresql+psycopg"
            or not parsed.hostname
            or not parsed.username
            or not parsed.password
            or parsed.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            fail("external_postgresql_url_requires_container_reachable_host_and_credentials")
        query = parsed.query.split("&")
        if not any(
            item in ("sslmode=verify-full", "sslmode=verify-ca", "sslmode=require")
            for item in query
        ):
            fail("external_postgresql_requires_explicit_tls")
    else:
        database_url = ""
    # Input conflicts are detected before generating any new credentials.
    once(STATE / "inputs.json", encoded(settings))
    once(STATE / "initial-identity.json", identity, owner=(0, SERVICE[1]), mode=0o640)
    secret_path = STATE / "secrets.json"
    if secret_path.exists():
        credentials = read_json(secret_path)
    else:
        credentials = {
            key: secrets.token_urlsafe(36)
            for key in ("gateway_api", "gateway_admin", "postgres_admin", "postgres_app")
        }
        once(secret_path, encoded(credentials))
    if any(not isinstance(value, str) or len(value) < 40 for value in credentials.values()):
        fail("generated_credential_record_invalid")
    if len(set([*credentials.values(), hermes_key])) != len(credentials) + 1:
        fail("credentials_must_be_independent")
    if not database_url:
        database_url = (
            "postgresql+psycopg://cf_gateway:"
            + credentials["postgres_app"]
            + "@cf-gateway-postgres:5432/cf_gateway?connect_timeout=5"
        )
    runtime = {
        "CF_GATEWAY_API_TOKEN": credentials["gateway_api"],
        "CF_AGENT_GATEWAY_ADMIN_TOKEN": credentials["gateway_admin"],
        "HERMES_API_KEY": hermes_key,
        "CF_AGENT_GATEWAY_DATABASE_URL": database_url,
    }
    once(ROOT / "runtime.env", dotenv(runtime))
    config = {
        "server": {"host": "0.0.0.0", "port": 8080},
        "database": {"url": "postgresql+psycopg://configuration-required"},
        "logging": {"level": "INFO"},
        "api": {
            "token_env": "CF_GATEWAY_API_TOKEN",
            "admin_token_env": "CF_AGENT_GATEWAY_ADMIN_TOKEN",
        },
        "artifact": {"storage_root": "/var/lib/cf-agent-gateway/artifacts"},
        "runtime": {"polling_interval_seconds": 3, "v2_routing_enabled": True},
        "worker": {"enabled": True, "concurrency": 4, "lease_seconds": 60, "retry_limit": 3},
        "wechat": {
            "enabled": True,
            "base_url": "http://cf-agent-wechat:6174",
            "bootstrap_mode": "latest",
            "token_env": "CF_AGENT_WECHAT_TOKEN",
        },
        "hermes": {
            "enabled": True,
            "base_url": settings["hermes_url"],
            "api_key_env": "HERMES_API_KEY",
            "model": settings["hermes_model"],
        },
    }
    # JSON is a YAML subset; no host-side PyYAML dependency is required.
    once(ROOT / "config/production.local.yaml", encoded(config), owner=(0, SERVICE[1]), mode=0o640)
    once(STATE / "runtime-values.json", encoded(runtime))
    if settings["database"]["mode"] == "managed":
        directory(DB_ROOT)
    finalize_configuration(image_id)
    once(STATE / "configured.json", encoded({"version": 1}))


def pull_record(reference: str, record: Path) -> dict:
    if record.exists():
        saved = read_json(record)
        if saved["requested_reference"] != reference:
            fail("image_input_conflict")
        digest = saved["digest_reference"]
        # Always re-obtain the recorded digest, never re-resolve a tag on retry.
    else:
        digest = reference
    docker(["pull", "--platform", "linux/amd64", digest], timeout=1200)
    inspected = json.loads(docker(["image", "inspect", digest]))[0]
    if inspected["Os"] != "linux" or inspected["Architecture"] != "amd64":
        fail("image_platform_mismatch")
    digests = inspected.get("RepoDigests", [])
    if not digests or "@sha256:" not in digests[0]:
        fail("registry_digest_missing")
    result = {
        "requested_reference": reference,
        "digest_reference": digest if "@sha256:" in digest else digests[0],
        "image_id": inspected["Id"],
    }
    once(record, encoded(result))
    return result


def build(options) -> None:
    settings = {"python_image": "python:3.12-slim-bookworm"}
    if options.inputs:
        settings = validate_inputs(json.loads(administrator_input(options.inputs, options.manager)))
    base = pull_record(settings["python_image"], STATE / "python-image.json")
    image_record = STATE / "gateway-image.json"
    if image_record.exists():
        result = read_json(image_record)
        if result["gateway_commit"] != options.gateway_commit:
            fail("built_source_commit_conflict")
        inspected = json.loads(docker(["image", "inspect", result["image_id"]]))[0]
        if inspected["Id"] != result["image_id"]:
            fail("built_image_missing_use_separate_rebuild")
    else:
        iid = STATE / "pending-image-id"
        if iid.exists():
            # It contains no credentials and is only this installer's build output.
            regular(iid)
            iid.unlink()
        docker(
            [
                "build",
                "--platform",
                "linux/amd64",
                "--file",
                ROOT / "docker/Dockerfile",
                "--build-arg",
                "BASE_IMAGE=" + base["digest_reference"],
                "--build-arg",
                "SOURCE_COMMIT=" + options.gateway_commit,
                "--iidfile",
                iid,
                ROOT,
            ],
            timeout=1800,
        )
        image_id = iid.read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            fail("invalid_built_image_id")
        result = {
            "gateway_commit": options.gateway_commit,
            "image_id": image_id,
            "base_digest": base["digest_reference"],
        }
        once(image_record, encoded(result))
        iid.unlink()
    image_id = result["image_id"]
    packages = docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "cat",
            image_id,
            "/app/python-packages.txt",
        ]
    )
    once(STATE / "python-packages.txt", packages + "\n")


def finalize_configuration(image_id: str) -> None:
    runtime = read_json(STATE / "runtime-values.json")
    environment = {
        "COMPOSE_PROJECT_NAME": "cf-agent-gateway",
        "CF_GATEWAY_IMAGE": image_id,
        "CF_GATEWAY_ENV_FILE": str(ROOT / "runtime.env"),
        "CF_GATEWAY_CONFIG_FILE": str(ROOT / "config/production.local.yaml"),
        "CF_AGENT_GATEWAY_DATABASE_URL": runtime["CF_AGENT_GATEWAY_DATABASE_URL"],
        "CF_AGENT_WECHAT_TOKEN_HOST_FILE": str(TOKEN),
        "CF_GATEWAY_BIND_ADDRESS": "127.0.0.1",
        "CF_GATEWAY_PORT": "8080",
    }
    once(ROOT / ".env", dotenv(environment))
    rendered = json.loads(compose(["config", "--format", "json"]))
    for name in ("gateway", "migration", "worker", "dispatch-worker", "delivery-worker"):
        service = rendered["services"][name]
        if (
            service["image"] != image_id
            or service["environment"]["CF_AGENT_GATEWAY_DATABASE_URL"]
            != runtime["CF_AGENT_GATEWAY_DATABASE_URL"]
        ):
            fail("rendered_configuration_mismatch")
    if rendered["networks"]["default"]["name"] != "cf-internal":
        fail("rendered_network_mismatch")
    # Parse the actual effective config, including credentials, in the image.
    compose(
        [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migration",
            "python",
            "-c",
            "import os; from cf_agent_gateway.config import load_settings; "
            'load_settings(os.environ["CF_GATEWAY_CONFIG"])',
        ]
    )


def database() -> None:
    settings = read_json(STATE / "inputs.json")
    if settings["database"]["mode"] == "external":
        compose(
            [
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "migration",
                "python",
                "-c",
                "import os; from sqlalchemy import create_engine,text; "
                'e=create_engine(os.environ["CF_AGENT_GATEWAY_DATABASE_URL"]); '
                'c=e.connect(); c.execute(text("SELECT 1")); c.close(); e.dispose()',
            ]
        )
        once(STATE / "database.json", encoded({"mode": "external", "connectivity": "passed"}))
        return
    image = pull_record(settings["postgres_image"], STATE / "postgres-image.json")
    identity = docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "id",
            image["digest_reference"],
            "postgres",
        ]
    )
    match = re.match(r"uid=(\d+)\(postgres\) gid=(\d+)\(postgres\)", identity)
    if not match or "0" in match.groups():
        fail("postgres_service_identity_invalid")
    uid, gid = map(int, match.groups())
    credentials = read_json(STATE / "secrets.json")
    for name, key in (("postgres-password", "postgres_admin"), ("app-password", "postgres_app")):
        once(DB_ROOT / name, credentials[key], owner=(uid, gid), mode=0o400)
    once(
        DB_ROOT / ".env",
        dotenv({"CF_POSTGRES_IMAGE": image["digest_reference"], "CF_POSTGRES_ROOT": str(DB_ROOT)}),
    )
    once(DB_ROOT / "init-app.sh", (ROOT / "deploy/postgres/init-app.sh").read_bytes(), mode=0o644)
    # Guard against accidentally claiming a pre-existing unrecorded database.
    intent = DB_ROOT / "volume-intent.json"
    existing = docker(
        ["volume", "ls", "--filter", "name=^cf-agent-gateway-db_data$", "--format", "{{.Name}}"]
    )
    if existing and not intent.exists():
        fail("unowned_database_volume_preserved")
    once(intent, encoded({"project": "cf-agent-gateway-db", "volume": "cf-agent-gateway-db_data"}))
    db_compose(["up", "--detach", "--wait", "--wait-timeout", "180"], timeout=240)
    # Resume a crash between database-role and database creation without resetting
    # an existing role, credential or database. This uses only owned DB assets.
    db_compose(
        [
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "bash",
            "/docker-entrypoint-initdb.d/10-gateway.sh",
        ]
    )
    # Check app credentials too: pg_isready alone does not authenticate.
    compose(
        [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migration",
            "python",
            "-c",
            "import os; from sqlalchemy import create_engine,text; "
            'e=create_engine(os.environ["CF_AGENT_GATEWAY_DATABASE_URL"]); '
            'c=e.connect(); assert c.execute(text("SELECT rolsuper FROM pg_roles '
            'WHERE rolname=current_user")).scalar() is False; c.close(); e.dispose()',
        ]
    )
    once(STATE / "database.json", encoded({"mode": "managed", "connectivity": "passed"}))


def initialize(*, check: bool = False) -> None:
    regular(STATE / "database.json")
    args = [
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--volume",
        str(STATE / "initial-identity.json") + ":/run/initial-identity.json:ro",
        "migration",
        "python",
        "-m",
        "cf_agent_gateway.initial_identity",
        "--input",
        "/run/initial-identity.json",
    ]
    if check:
        args += ["--check"]
    compose(args)


def core_ready() -> None:
    output = compose(["ps", "--format", "json", "gateway", "dispatch-worker"])
    containers = (
        json.loads(output)
        if output.startswith("[")
        else [json.loads(line) for line in output.splitlines() if line.strip()]
    )
    if len(containers) != 2 or any(row.get("Health") != "healthy" for row in containers):
        fail("gateway_dispatch_not_healthy")


def start() -> None:
    initialize(check=True)
    # Never start worker or delivery-worker here. The Controller retains that duty.
    compose(
        ["up", "--detach", "--wait", "--wait-timeout", "180", "gateway", "dispatch-worker"],
        timeout=300,
    )
    core_ready()
    once(STATE / "started.json", encoded({"gateway": "ready", "dispatch": "ready"}))


def diagnose() -> None:
    compose(
        [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "migration",
            "python",
            "-m",
            "cf_agent_gateway.runtime.startup",
            "check",
        ]
    )
    initialize(check=True)
    core_ready()
    # Default diagnostic makes no HTTP/business/model request.
    output = compose(["exec", "-T", "gateway", "python", "-m", "cf_agent_gateway.hermes.diagnose"])
    print(output)
    print("Gateway/Dispatch ready; Poll/Delivery status is separate from Hermes acceptance.")


def boot_service(options) -> None:
    if not Path("/run/systemd/system").is_dir():
        fail("booted_systemd_host_required_not_container_acceptance")
    initialize(check=True)
    for name in ("cf-agent-gateway-core.service", "cf-agent-gateway-database.service"):
        if (
            name.endswith("database.service")
            and read_json(STATE / "inputs.json")["database"]["mode"] != "managed"
        ):
            continue
        once(
            Path("/etc/systemd/system") / name,
            (ROOT / "deploy/systemd" / name).read_bytes(),
            mode=0o644,
        )
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", name])
    print("Boot units enabled. Validate them and reboot only in the separate B acceptance window.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--manager", required=True)
    parser.add_argument("--gateway-commit", required=True)
    parser.add_argument("--wechat-commit", required=True)
    parser.add_argument(
        "--inputs",
        help="absolute, owner-only JSON file (configure; optional image input for build)",
    )
    options = parser.parse_args(argv)
    if (
        not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", options.manager)
        or options.manager == "root"
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", value)
            for value in (options.gateway_commit, options.wechat_commit)
        )
    ):
        parser.error("non-root manager and two complete lowercase Git SHAs are required")
    try:
        os.umask(0o077)
        platform()
        directory(STATE)
        safe_parents(STATE / "install.lock")
        with (STATE / "install.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            versions = {
                "gateway_commit": options.gateway_commit,
                "wechat_commit": options.wechat_commit,
                "manager": options.manager,
                "configuration_version": 1,
            }
            # Refuse conflicting invocations before touching any staged asset.
            once(STATE / "source-versions.json", encoded(versions))
            if options.stage in ("system", "system-packages"):
                fixed_checkout(WECHAT_SOURCE, WECHAT_REPO, options.wechat_commit, mode=0o700)
                run(
                    [
                        "bash",
                        WECHAT_SOURCE / "scripts/prepare-clean-host.sh",
                        options.stage,
                        "--manager",
                        options.manager,
                    ],
                    timeout=1800,
                )
                manager_identity(options.manager)
                directory(Path("/usr/local/libexec"), 0o755)
                once(
                    Path("/usr/local/libexec/cf-agent-wechat-prepare"),
                    (WECHAT_SOURCE / "scripts/prepare-clean-host.sh").read_bytes(),
                    mode=0o755,
                )
            else:
                manager_identity(options.manager)
                fixed_checkout(ROOT, REPO, options.gateway_commit, mode=0o750)
                if options.stage == "controller":
                    # Only these static public executable assets are exposed. Root
                    # remains 0750, manager never joins root/docker/service groups.
                    for path in (ROOT / "deploy", ROOT / "deploy/wechat-runtime-control"):
                        safe_parents(path)
                        os.chmod(path, 0o755)
                    contract()
                else:
                    contract()
                    docker_check()
                    if options.stage != "build":
                        validate_token_and_network()
                    if options.stage == "configure":
                        configure(options)
                    elif options.stage == "build":
                        build(options)
                    elif options.stage == "database":
                        database()
                    elif options.stage == "migrate":
                        regular(STATE / "database.json")
                        compose(["run", "--rm", "-T", "migration"])
                    elif options.stage == "initialize":
                        initialize()
                    elif options.stage == "start":
                        start()
                    elif options.stage == "diagnose":
                        diagnose()
                    elif options.stage == "boot-service":
                        boot_service(options)

        print("[PASS:" + options.stage + "] completed; existing configuration and data retained")
        return 0
    except (InstallError, OSError, ValueError, KeyError) as error:
        code = str(error) if isinstance(error, InstallError) else type(error).__name__
        print(
            "[FAIL:"
            + options.stage
            + "] "
            + code
            + "; no data cleanup performed; correct this stage dependency and retry",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
