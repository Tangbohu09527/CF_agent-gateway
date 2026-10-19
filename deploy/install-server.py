#!/usr/bin/env python3
"""Pinned first-install orchestration; no business identity, login or gates.

Component installers remain authoritative for their resources. A protected
journal records attempts, not just a success bit. A changed input/version is
never treated as an upgrade. The test constructor can relocate journal files;
the privileged CLI has no arbitrary-root/path/test-command option.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import sys
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
STATE = Path("/var/lib/cf-suite-install")
FB = Path("/opt/cf-filebrowser-enterprise")
WC = Path("/opt/cf-agent-wechat")


class SuiteError(RuntimeError):
    pass


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_inputs(raw):
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or type(raw.get("version")) is not int
        or set(raw) != {"version", "manager", "storage_evidence", "gateway", "filebrowser", "https"}
    ):
        raise SuiteError("invalid input version or key allowlist")
    if (
        not isinstance(raw["manager"], str)
        or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", raw["manager"])
        or raw["manager"] == "root"
    ):
        raise SuiteError("a separate non-root manager name is required")
    if raw["storage_evidence"] not in ("test-disk", "raid-confirmed"):
        raise SuiteError("explicit independent storage evidence required; no disk is initialized")
    fb = raw["filebrowser"]
    if (
        not isinstance(fb, dict)
        or set(fb) != {"image", "mode"}
        or fb["mode"] not in ("staging", "candidate")
    ):
        raise SuiteError("invalid FileBrowser image/mode")
    if fb["image"] is not None and (
        not isinstance(fb["image"], str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}", fb["image"])
    ):
        raise SuiteError("FileBrowser supplied image requires an immutable digest")
    if fb["mode"] == "candidate" and (
        fb["image"] is None or raw["storage_evidence"] != "raid-confirmed"
    ):
        raise SuiteError(
            "candidate mode needs a published immutable image and reviewed storage; "
            "local source builds are staging evidence"
        )
    tls = raw["https"]
    expected = {
        "bind_address",
        "gateway_port",
        "filebrowser_port",
        "allowed_cidrs",
        "server_name",
        "certificate_file",
        "private_key_file",
        "ca_file",
    }
    if not isinstance(tls, dict) or set(tls) != expected:
        raise SuiteError("invalid HTTPS inputs")
    address = ipaddress.ip_address(tls["bind_address"])
    if (
        address.version != 4
        or address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
    ):
        raise SuiteError("HTTPS needs a specific non-loopback IPv4 bind address")
    ports = [tls["gateway_port"], tls["filebrowser_port"]]
    if (
        any(type(port) is not int or not 1024 <= port <= 65535 for port in ports)
        or len(set(ports)) != 2
    ):
        raise SuiteError("HTTPS needs two distinct non-privileged ports")
    if not isinstance(tls["allowed_cidrs"], list) or not tls["allowed_cidrs"]:
        raise SuiteError("explicit HTTPS client networks required")
    if len(tls["allowed_cidrs"]) != len(set(tls["allowed_cidrs"])):
        raise SuiteError("HTTPS client networks must be unique")
    for value in tls["allowed_cidrs"]:
        network = ipaddress.ip_network(value, strict=True)
        if (
            network.version != 4
            or network.prefixlen == 0
            or str(network) != value
            or network.network_address.is_unspecified
            or network.is_loopback
            or network.is_multicast
            or network.is_link_local
        ):
            raise SuiteError("HTTPS requires canonical unicast source networks narrower than /0")
    if (
        not isinstance(tls["server_name"], str)
        or len(tls["server_name"]) > 253
        or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in tls["server_name"].split(".")
        )
    ):
        raise SuiteError("invalid TLS server name")
    for name in ("certificate_file", "private_key_file", "ca_file"):
        if not isinstance(tls[name], str) or not Path(tls[name]).is_absolute():
            raise SuiteError("protected absolute TLS input paths required")
    if not isinstance(raw["gateway"], dict) or "initial_identity_file" in raw["gateway"]:
        raise SuiteError("employee onboarding is separate; base entry accepts no initial identity")
    return raw


class Journal:
    def __init__(self, path, binding, write):
        self.path, self.write = path, write
        if path.exists():
            self.value = json.loads(path.read_bytes())
            if self.value.get("version") != 1 or self.value.get("binding") != binding:
                raise SuiteError(
                    "installed input/source binding differs; preserve it and use a reviewed upgrade"
                )
        else:
            self.value = {"version": 1, "binding": binding, "events": [], "completed": []}
            self.save()

    def save(self):
        self.write(self.path, canonical(self.value))

    def stage(self, name, action, *, reconcile=False):
        if name in self.value["completed"] and not reconcile:
            return
        started = dt.datetime.now(dt.UTC).isoformat()
        event = {"stage": name, "started_utc": started, "result": "running"}
        self.value["events"].append(event)
        self.save()
        print("[suite] " + name, flush=True)
        try:
            action()
        except Exception:
            event["result"] = "failed-or-interrupted"
            self.save()
            raise
        event.update(result="completed", finished_utc=dt.datetime.now(dt.UTC).isoformat())
        if name not in self.value["completed"]:
            self.value["completed"].append(name)
        self.save()


def core_module():
    spec = importlib.util.spec_from_file_location(
        "gateway_clean_install", SOURCE / "deploy/prepare-clean-host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Installer:
    def __init__(self, raw, gateway_sha, container_test=False):
        self.core = core_module()
        self.raw = validate_inputs(raw)
        self.sha = gateway_sha
        self.container_test = container_test
        self.lock = json.loads((SOURCE / "deploy/server-components.json").read_bytes())
        self.manager = raw["manager"]
        self.common = [
            "--manager",
            self.manager,
            "--gateway-commit",
            self.sha,
            "--wechat-commit",
            self.lock["wechat"]["revision"],
        ]
        if not re.fullmatch(r"[0-9a-f]{40}", gateway_sha):
            raise SuiteError("full Gateway entry SHA required")
        if self.core.run(["git", "-C", SOURCE, "rev-parse", "HEAD"]) != gateway_sha:
            raise SuiteError("entry checkout differs from requested Gateway SHA")
        if self.core.run(
            ["git", "-C", SOURCE, "status", "--porcelain", "--untracked-files=normal"]
        ):
            raise SuiteError("entry checkout is dirty; retained without replacement")
        if self.core.run(["git", "-C", SOURCE, "remote", "get-url", "origin"]).removesuffix(
            ".git"
        ) != self.lock["gateway"]["repository"].removesuffix(".git"):
            raise SuiteError("entry origin differs from the fixed repository")
        # Validate Gateway schema before a journal/package/credential is created.
        settings = dict(raw["gateway"])
        settings.setdefault("hermes_api_key_file", str(STATE / "secrets/hermes-api-key"))
        settings.setdefault("python_image", self.lock["images"]["python"])
        settings.setdefault("postgres_image", self.lock["images"]["postgres"])
        self.gateway_settings = self.core.validate_inputs(settings)
        self.input_hashes = {}
        for name in ("certificate_file", "private_key_file", "ca_file"):
            path = raw["https"][name]
            self.input_hashes[path] = hashlib.sha256(
                self.core.administrator_input(path, "root")
            ).hexdigest()
        if "hermes_api_key_file" in raw["gateway"]:
            path = raw["gateway"]["hermes_api_key_file"]
            self.input_hashes[path] = hashlib.sha256(
                self.core.administrator_input(path, "root")
            ).hexdigest()
        if raw["gateway"]["database"]["mode"] == "external":
            path = raw["gateway"]["database"]["external_url_file"]
            self.input_hashes[path] = hashlib.sha256(
                self.core.administrator_input(path, "root")
            ).hexdigest()
        if container_test and (
            not Path("/.dockerenv").is_file() or os.environ.get("GITHUB_ACTIONS") != "true"
        ):
            raise SuiteError(
                "container-test requires a disposable GitHub Actions container; "
                "it never proves systemd/host reboot"
            )

    def command(self, argv, *, timeout=1800, data=None):
        record = STATE / ("command-" + secrets.token_hex(8) + ".private.log")
        return self.core.run(argv, timeout=timeout, data=data, log_path=record)

    def gateway(self, stage, inputs=False):
        args = ["bash", SOURCE / "deploy/install-clean-device.sh", stage, *self.common]
        if inputs:
            args += ["--inputs", STATE / "gateway-inputs.json"]
        self.command(args, timeout=2400)

    def fb(self, action, *args):
        return self.command(
            [
                "bash",
                FB / "deploy/shared-host/manage.sh",
                action,
                "--hostname",
                socket.gethostname(),
                *args,
            ],
            timeout=600,
        )

    def atomic(self, path, data):
        self.core.safe_parents(path)
        if path.exists():
            self.core.regular(path)
        temporary = path.with_name(path.name + ".pending")
        if temporary.exists():
            # A known stale journal write is diagnostic material, not overwritten.
            temporary = path.with_name(path.name + ".pending-" + secrets.token_hex(8))
        self.core.once(temporary, data)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def credentials(self):
        self.core.directory(STATE / "secrets")
        for name in ("filebrowser-admin", "hermes-api-key"):
            target = STATE / "secrets" / name
            if not target.exists():
                self.core.once(target, secrets.token_urlsafe(36) + "\n")
            self.core.regular(target)
        self.core.once(STATE / "gateway-inputs.json", canonical(self.gateway_settings))

    def checkout_filebrowser(self):
        self.core.fixed_checkout(
            FB,
            self.lock["filebrowser"]["repository"],
            self.lock["filebrowser"]["revision"],
            mode=0o755,
        )
        # This one reviewed code file is bind-mounted and executed as 10001.
        # Git honors the installer's 077 umask; leave every credential/private
        # file unchanged rather than relaxing the checkout or global umask.
        entrypoint = FB / "scripts/container-entrypoint.sh"
        mode = stat.S_IMODE(entrypoint.lstat().st_mode)
        if mode not in (0o700, 0o755):
            raise SuiteError("unexpected FileBrowser entrypoint mode; retained")
        self.core.regular(entrypoint, mode=mode)
        if mode != 0o755:
            os.chmod(entrypoint, 0o755)
        self.core.regular(entrypoint, mode=0o755)

    def filebrowser_image(self):
        revision = self.lock["filebrowser"]["revision"]
        image = self.raw["filebrowser"]["image"]
        record = STATE / "filebrowser-image.json"
        if record.exists():
            saved = self.core.read_json(record)
            expected = image or "cf-filebrowser-unified:" + revision
            if saved.get("source_sha") != revision or saved.get("reference") != expected:
                raise SuiteError("recorded FileBrowser image binding differs; retained")
            current = json.loads(self.command(["docker", "image", "inspect", expected]))[0]
            if (
                current.get("Id") != saved.get("image_id")
                or current.get("Config", {})
                .get("Labels", {})
                .get("org.opencontainers.image.revision")
                != revision
            ):
                raise SuiteError("recorded FileBrowser image is missing or changed; retained")
            return
        if image:
            self.command(["docker", "pull", "--platform", "linux/amd64", image], timeout=1200)
            info = json.loads(self.command(["docker", "image", "inspect", image]))[0]
        else:
            image = "cf-filebrowser-unified:" + revision
            # Each failed build has an independent retained evidence directory.
            evidence = STATE / ("filebrowser-build-" + secrets.token_hex(8))
            self.command(
                [
                    "bash",
                    FB / "deploy/shared-host/image.sh",
                    "build",
                    "--source-sha",
                    revision,
                    "--image",
                    image,
                    "--version",
                    "fixed-" + revision,
                    "--evidence",
                    evidence,
                ],
                timeout=3000,
            )
            info = json.loads(self.command(["docker", "image", "inspect", image]))[0]
        if (
            info.get("Os") != "linux"
            or info.get("Architecture") != "amd64"
            or info.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
            != revision
        ):
            raise SuiteError("FileBrowser actual image/source label mismatch")
        self.core.once(
            record, canonical({"source_sha": revision, "reference": image, "image_id": info["Id"]})
        )

    def prepare_filebrowser(self):
        image = self.core.read_json(STATE / "filebrowser-image.json")
        tls = self.raw["https"]
        self.fb(
            "prepare",
            "--source-sha",
            self.lock["filebrowser"]["revision"],
            "--image-ref",
            image["reference"],
            "--mode",
            self.raw["filebrowser"]["mode"],
            "--exposure",
            "lan",
            "--" + self.raw["storage_evidence"],
            "--bootstrap-password-file",
            str(STATE / "secrets/filebrowser-admin"),
            "--lan-bind-address",
            tls["bind_address"],
            "--lan-port",
            str(tls["filebrowser_port"]),
            "--lan-allowed-cidrs",
            ",".join(tls["allowed_cidrs"]),
            "--tls-name",
            tls["server_name"],
            "--tls-cert-file",
            tls["certificate_file"],
            "--tls-key-file",
            tls["private_key_file"],
            "--tls-ca-file",
            tls["ca_file"],
        )

    def https_prepare(self):
        tls = self.raw["https"]
        pinned = self.core.pull_record(self.lock["images"]["https"], STATE / "https-image.json")
        directory = STATE / "https"
        self.core.safe_parents(directory)
        if directory.exists():
            import stat

            info = directory.stat()
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 10001, 0o750):
                raise SuiteError("HTTPS directory identity differs; retained")
        else:
            directory.mkdir(mode=0o750)
            os.chown(directory, 0, 10001)
        for name, key in [
            ("server.crt", "certificate_file"),
            ("server.key", "private_key_file"),
            ("ca.crt", "ca_file"),
        ]:
            # FileBrowser's earlier prepare validated chain/name/key/lifetime.
            content = self.core.administrator_input(tls[key], self.manager)
            self.core.once(directory / name, content, owner=(0, 10001), mode=0o440)
        allow = "".join("    allow " + value + ";\n" for value in tls["allowed_cidrs"])
        config = (
            "pid /tmp/nginx.pid;\nevents { worker_connections 256; }\nhttp {\n"
            "  access_log /dev/stdout; error_log /dev/stderr;\n"
            "  client_body_temp_path /tmp/body; proxy_temp_path /tmp/proxy;\n"
            "  fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi;\n"
            "  scgi_temp_path /tmp/scgi;\n"
            "  server {\n    listen 8443 ssl; server_name " + tls["server_name"] + ";\n"
            "    ssl_certificate /etc/cf-https/server.crt;\n"
            "    ssl_certificate_key /etc/cf-https/server.key;\n"
            "    ssl_protocols TLSv1.2 TLSv1.3; client_max_body_size 256m;\n"
            + allow
            + "    deny all;\n    location / { proxy_pass http://gateway:8080;\n"
            "      proxy_read_timeout 600s; proxy_set_header Host $host;\n"
            "      proxy_set_header X-Forwarded-Proto https; }\n  }\n}\n"
        )
        self.core.once(directory / "nginx.conf", config, owner=(0, 10001), mode=0o440)
        compose = {
            "name": "cf-suite-https",
            "services": {
                "https": {
                    "image": pinned["digest_reference"],
                    "user": "10001:10001",
                    "read_only": True,
                    "restart": "unless-stopped",
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                    "environment": {"TZ": "Asia/Shanghai"},
                    "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=128m"],
                    "command": ["nginx", "-c", "/etc/cf-https/nginx.conf", "-g", "daemon off;"],
                    "ports": [tls["bind_address"] + ":" + str(tls["gateway_port"]) + ":8443"],
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(directory),
                            "target": "/etc/cf-https",
                            "read_only": True,
                            "bind": {"create_host_path": False},
                        }
                    ],
                    "networks": ["cf-internal"],
                    "pids_limit": 128,
                    "mem_limit": "256m",
                    "healthcheck": {
                        "test": ["CMD", "nginx", "-t", "-c", "/etc/cf-https/nginx.conf"],
                        "interval": "10s",
                        "timeout": "5s",
                        "retries": 5,
                    },
                    "logging": {"driver": "local", "options": {"max-size": "20m", "max-file": "3"}},
                }
            },
            "networks": {"cf-internal": {"external": True, "name": "cf-internal"}},
        }
        self.core.once(STATE / "https-compose.json", canonical(compose))

    def run(self):
        self.core.platform()
        if not self.container_test and not Path("/run/systemd/system").is_dir():
            raise SuiteError(
                "booted systemd is required; container tests are a different evidence layer"
            )
        self.core.directory(STATE)
        # Serialize suite invocations in addition to each component's own locks.
        import fcntl

        lock_path = STATE / "install.lock"
        self.core.safe_parents(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.core.regular(lock_path)
            identity = {
                "inputs": self.raw,
                "gateway_sha": self.sha,
                "components": self.lock,
                "container_test": self.container_test,
                "protected_input_hashes": self.input_hashes,
            }
            journal = Journal(STATE / "journal.json", fingerprint(identity), self.atomic)
            stage = journal.stage
            stage(
                "system",
                lambda: self.gateway("system-packages" if self.container_test else "system"),
            )
            if self.container_test:
                # The disposable harness starts its own actual daemon. This is
                # neither a host socket nor a systemd replacement in production.
                deadline = time.monotonic() + 120
                while True:
                    try:
                        self.core.docker_check()
                        break
                    except self.core.InstallError:
                        if time.monotonic() >= deadline:
                            raise SuiteError("isolated harness Docker daemon unavailable") from None
                        time.sleep(1)
            stage(
                "filebrowser-dependencies",
                lambda: self.command(
                    [
                        "apt-get",
                        "install",
                        "--yes",
                        "--no-install-recommends",
                        "python3-yaml",
                        "iproute2",
                    ]
                ),
            )
            stage("gateway-controller", lambda: self.gateway("controller"))
            w = self.lock["wechat"]
            stage(
                "wechat-checkout",
                lambda: self.command(
                    [
                        "bash",
                        "/usr/local/libexec/cf-agent-wechat-prepare",
                        "checkout",
                        "--manager",
                        self.manager,
                        "--commit",
                        w["revision"],
                        "--execution-mode",
                        "root-managed",
                    ]
                ),
            )
            stage(
                "wechat-configure",
                lambda: self.command(
                    [
                        "bash",
                        WC / "scripts/prepare-clean-host.sh",
                        "configure",
                        "--manager",
                        self.manager,
                        "--commit",
                        w["revision"],
                        "--image",
                        w["image"],
                        "--runtime-uid",
                        str(w["runtime_uid"]),
                        "--runtime-gid",
                        str(w["runtime_gid"]),
                        "--execution-mode",
                        "root-managed",
                    ]
                ),
            )

            def bootstrap():
                command = ["bash", WC / "scripts/bootstrap-cfserver.sh"]
                if self.container_test:
                    command = [
                        "env",
                        "CF_BOOTSTRAP_TESTING=1",
                        "CF_BOOTSTRAP_SYSTEMCTL_BIN=/usr/local/bin/cf-a-systemctl",
                        *command,
                    ]
                self.command(command)

            stage("wechat-bootstrap-no-login", bootstrap)
            stage("credentials", self.credentials)
            stage(
                "filebrowser-checkout",
                self.checkout_filebrowser,
            )
            stage("filebrowser-image", self.filebrowser_image)
            stage("filebrowser-prepare", self.prepare_filebrowser)
            stage("filebrowser-start", lambda: self.fb("start"), reconcile=True)
            stage(
                "filebrowser-bootstrap-finish",
                lambda: self.fb(
                    "bootstrap-finish",
                    "--admin-password-file",
                    str(STATE / "secrets/filebrowser-admin"),
                ),
            )
            stage("gateway-build", lambda: self.gateway("build", inputs=True))
            stage("gateway-configure", lambda: self.gateway("configure", inputs=True))
            stage("gateway-database", lambda: self.gateway("database"), reconcile=True)
            stage("gateway-migrate", lambda: self.gateway("migrate"))
            stage("gateway-start", lambda: self.gateway("start"), reconcile=True)
            stage("https-prepare", self.https_prepare)
            stage(
                "https-start",
                lambda: self.command(
                    [
                        "docker",
                        "compose",
                        "-f",
                        STATE / "https-compose.json",
                        "up",
                        "--detach",
                        "--wait",
                        "--wait-timeout",
                        "120",
                    ],
                    timeout=180,
                ),
                reconcile=True,
            )
            if not self.container_test:
                stage("gateway-boot-service", lambda: self.gateway("boot-service"))
            self.status()

    def status(self):
        if not FB.is_dir():
            raise SuiteError("FileBrowser component is not installed; status does not install it")
        self.fb("validate")
        self.gateway("diagnose")
        print(
            json.dumps(
                {
                    "base": "ready",
                    "wechat": "pending-explicit-login",
                    "employee": "pending-explicit-authorization",
                    "poll_delivery": "not-opened-by-installer",
                    "hermes_configuration_only_business": "not-yet-proven",
                    "evidence_layer": "Debian-container-real-services-systemd-substitute"
                    if self.container_test
                    else "booted-Debian-host",
                    "timezone": "Asia/Shanghai",
                    "sql_timezone": "UTC",
                },
                sort_keys=True,
            )
        )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status"))
    parser.add_argument("--gateway-commit", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument(
        "--container-test",
        action="store_true",
        help="CI-only partial systemd evidence; never a production fallback",
    )
    args = parser.parse_args()
    try:
        core = core_module()
        core.platform()
        raw = json.loads(core.administrator_input(args.inputs, "root"))
        Installer(
            raw, args.gateway_commit, args.container_test
        ).run() if args.action == "install" else Installer(
            raw, args.gateway_commit, args.container_test
        ).status()
    except Exception as error:
        # Do not emit arbitrary argv, JSON bodies, certificates or responses.
        detail = str(error) if isinstance(error, SuiteError) else type(error).__name__
        print(
            "[suite failed] " + detail + "; data and failed-stage evidence retained",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
