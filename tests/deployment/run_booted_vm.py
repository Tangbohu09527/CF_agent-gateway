"""Run the unchanged B entry point inside a disposable Debian VM on GitHub Actions.

No remote target can be supplied. SSH reaches only this process's loopback QEMU
forward using a new key. The guest's sudo password and all installation secrets
stay in memory/private temporary files; only allowlisted, scanned evidence leaves.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

IMAGE_BASE = "https://cloud.debian.org/images/cloud/trixie/20260831-2587/"
IMAGE_NAME = "debian-13-generic-amd64-20260831-2587.qcow2"
IMAGE_SHA512 = (
    "5a069019420fb9441ad4f8004c661fadb747edd5662ca54a17c8f923dee7d717e21dbdaa4"
    "ba72d6fce7f920e0217f0a9af382298a7d46ed4bc9dc33ac19181b6"
)
WECHAT_COMMIT = "67cbbb04ce15703428ce165ac38effac19f4b701"
WECHAT_IMAGE = (
    "ghcr.io/thisnick/agent-wechat@sha256:"
    "b5e92047e28ce67e34576e574d8ccf00f8619f485597109f7342a137300285c0"
)
MANAGER = "cf-b-manager"
ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


class VMFailure(RuntimeError):
    pass


def run(command: list[str | Path], *, data: bytes | None = None, timeout: int = 120) -> bytes:
    result = subprocess.run(
        [str(value) for value in command],
        input=data,
        capture_output=True,
        env=ENVIRONMENT,
        timeout=timeout,
    )
    if result.returncode:
        raise VMFailure("command_failed:" + Path(command[0]).name)
    return result.stdout


def protected_write(path: Path, content: str | bytes) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(content.encode() if isinstance(content, str) else content)


def checked_download(directory: Path) -> Path:
    opener = build_opener(ProxyHandler({}))
    with opener.open(IMAGE_BASE + "SHA512SUMS", timeout=60) as response:
        sums = response.read().decode()
    expected = [line.split() for line in sums.splitlines() if line.split()[-1:] == [IMAGE_NAME]]
    if expected != [[IMAGE_SHA512, IMAGE_NAME]]:
        raise VMFailure("official_dated_image_checksum_record_changed")
    destination = directory / IMAGE_NAME
    digest = hashlib.sha512()
    deadline = time.monotonic() + 1200
    with (
        opener.open(IMAGE_BASE + IMAGE_NAME, timeout=120) as response,
        destination.open("xb") as out,
    ):
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            if time.monotonic() >= deadline:
                raise VMFailure("official_image_download_total_timeout")
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != IMAGE_SHA512:
        raise VMFailure("official_dated_image_download_checksum_mismatch")
    return destination


def ssh_options(work: Path, port: int) -> list[str]:
    return [
        "-F",
        "/dev/null",
        "-i",
        str(work / "ssh-key"),
        "-p",
        str(port),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "UserKnownHostsFile=" + str(work / "known-hosts"),
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
    ]


def ssh(work: Path, port: int, command: list[str], **kwargs) -> bytes:
    return run(["ssh", *ssh_options(work, port), "root@127.0.0.1", shlex.join(command)], **kwargs)


def cloud_config(public_key: str, password_hash: str) -> str:
    return (
        "#cloud-config\n"
        + json.dumps(
            {
                "disable_root": False,
                "ssh_pwauth": False,
                "users": [
                    {"name": "root", "ssh_authorized_keys": [public_key], "lock_passwd": True},
                    {
                        "name": MANAGER,
                        "uid": 1100,
                        "groups": ["sudo"],
                        "shell": "/bin/bash",
                        "lock_passwd": False,
                        "hashed_passwd": password_hash,
                        "sudo": None,
                    },
                ],
            }
        )
        + "\n"
    )


def select_accelerator() -> dict[str, str]:
    # Probe actual KVM initialization. Missing device/permissions is a reason to
    # try full software emulation, not to claim that a booted VM is impossible.
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        try:
            run(
                [
                    "qemu-system-x86_64",
                    "-machine",
                    "none,accel=kvm",
                    "-nodefaults",
                    "-display",
                    "none",
                    "-S",
                    "-qmp",
                    "stdio",
                ],
                data=b'{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n',
                timeout=15,
            )
            return {"selected": "kvm", "kvm_probe": "initialized"}
        except VMFailure:
            return {"selected": "tcg", "kvm_probe": "initialization_failed"}
        except subprocess.TimeoutExpired:
            return {"selected": "tcg", "kvm_probe": "initialization_timeout"}
    return {"selected": "tcg", "kvm_probe": "device_not_readable_and_writable"}


class TCPOnlyService(socketserver.ThreadingTCPServer):
    daemon_threads = True
    accepted = 0


class TCPOnlyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        # Default Hermes diagnosis opens a socket only. No HTTP/model response or
        # API authentication is simulated or claimed by this B fixture.
        self.server.accepted += 1


def wait_for_guest(
    work: Path, port: int, process: subprocess.Popen, previous_boot: str = ""
) -> str:
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VMFailure("qemu_exited_before_guest_ready")
        try:
            boot = (
                ssh(work, port, ["cat", "/proc/sys/kernel/random/boot_id"], timeout=15)
                .decode()
                .strip()
            )
            if re.fullmatch(r"[0-9a-f-]{36}", boot) and boot != previous_boot:
                return boot
        except (VMFailure, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
    raise VMFailure("guest_boot_or_reboot_timeout")


def run_interactive(
    work: Path, port: int, command: list[str], password: str, known_secrets: list[str]
) -> list[str]:
    import pexpect

    process = pexpect.spawn(
        "ssh",
        [*ssh_options(work, port), "-tt", "root@127.0.0.1", shlex.join(command)],
        encoding="utf-8",
        env=ENVIRONMENT,
        timeout=30,
    )
    transcript = ""
    prompts_answered = 0
    deadline = time.monotonic() + 3600
    try:
        while time.monotonic() < deadline:
            try:
                # Consume each byte exactly once, including output preceding a
                # timeout. Bound private memory and never attach a raw logfile.
                transcript += process.read_nonblocking(size=4096, timeout=30)
            except pexpect.TIMEOUT:
                print("B VM: formal acceptance is still running", flush=True)
                continue
            except pexpect.EOF:
                break
            if len(transcript) > 2 * 1024 * 1024:
                raise VMFailure("formal_B_terminal_output_limit")
            if any(value and value in transcript for value in known_secrets):
                raise VMFailure("credential_in_terminal_output_suppressed")
            prompts = transcript.count(f"[sudo] password for {MANAGER}:")
            if prompts > prompts_answered:
                if not process.waitnoecho(timeout=5):
                    raise VMFailure("sudo_terminal_echo_must_be_disabled")
                process.sendline(password)
                prompts_answered = prompts
        else:
            raise VMFailure("formal_B_entry_timeout")
        process.close()
        stages = re.findall(r"B official stage passed: ([a-z-]+)", transcript)
        if process.exitstatus != 0:
            raise VMFailure(
                "formal_B_entry_failed_after:" + (stages[-1] if stages else "authentication")
            )
        expected = [
            "system",
            "controller",
            "build",
            "configure",
            "database",
            "migrate",
            "start",
            "diagnose",
            "boot-service",
        ]
        if "before-reboot" in command:
            complete = stages == expected and "B preparation passed." in transcript
        else:
            complete = (
                "B booted-host installation and reboot passed; C remains pending." in transcript
            )
        if not complete or prompts_answered == 0:
            raise VMFailure("formal_B_success_marker_or_password_auth_missing")
        return stages
    finally:
        if process.isalive():
            process.terminate(force=True)


def evidence_json(payload: dict, secret_values: list[str]) -> str:
    encoded = json.dumps(payload, indent=2) + "\n"
    if any(
        value and (value in encoded or json.dumps(value)[1:-1] in encoded)
        for value in secret_values
    ):
        raise VMFailure("secret_in_evidence_suppressed")
    return encoded


def input_files(work: Path, port: int, hermes_key: str) -> list[Path]:
    values = {
        "hermes-key": hermes_key,
        "inputs.json": json.dumps(
            {
                "version": 1,
                "hermes_url": f"http://10.0.2.2:{port}",
                "hermes_model": "synthetic-B-no-call",
                "hermes_api_key_file": "/root/b-inputs/hermes-key",
                "database": {"mode": "managed"},
            }
        ),
    }
    for name, value in values.items():
        protected_write(work / name, value)
    return [work / name for name in values]


def verify_clean_guest(work: Path, port: int) -> dict:
    script = """
import json, shutil, subprocess
from pathlib import Path
paths = ('/opt/cf-agent-gateway', '/opt/cf-agent-wechat',
    '/var/lib/cf-agent-gateway-install', '/var/lib/cf-agent-gateway-postgres',
    '/srv/storage/cf-agent-wechat', '/var/lib/docker')
release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
    if '=' in line)
report = {
    'absent_paths': {path: not Path(path).exists() for path in paths},
    'docker_command_absent': shutil.which('docker') is None,
    'pid1': Path('/proc/1/comm').read_text().strip(),
    'distribution': release.get('ID', '').strip(chr(34)),
    'version': release.get('VERSION_ID', '').strip(chr(34)),
    'architecture': subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip(),
    'container': subprocess.run(['systemd-detect-virt', '--container'],
        capture_output=True, text=True).stdout.strip(),
    'dockerenv_absent': not Path('/.dockerenv').exists(),
}
if not (all(report['absent_paths'].values()) and report['docker_command_absent']
    and report['pid1'] == 'systemd' and report['distribution'] == 'debian'
    and report['version'] == '13' and report['architecture'] == 'amd64'
    and report['container'] in ('none', '') and report['dockerenv_absent']):
    raise RuntimeError('B_guest_not_pristine_booted_debian13_amd64')
print(json.dumps(report))
"""
    return json.loads(ssh(work, port, ["python3", "-c", script]).decode())


def guest_secrets(work: Path, port: int) -> list[str]:
    script = """
import json
from pathlib import Path
values = []
for name in ('secrets.json', 'runtime-values.json'):
    path = Path('/var/lib/cf-agent-gateway-install') / name
    if path.exists():
        values.extend(
            value for value in json.loads(path.read_text()).values() if isinstance(value, str)
        )
token = Path('/srv/storage/cf-agent-wechat/secrets/auth-token')
if token.exists():
    values.append(token.read_text().strip())
print(json.dumps(values))
"""
    return json.loads(ssh(work, port, ["python3", "-c", script]).decode())


def guest_build_evidence(work: Path, port: int) -> dict:
    script = """
import grp, json, os, pwd, subprocess
from pathlib import Path
state = Path('/var/lib/cf-agent-gateway-install')
records = {name: json.loads((state / name).read_text()) for name in (
    'source-versions.json', 'gateway-image.json', 'python-image.json', 'postgres-image.json')}
records['python-packages.txt'] = (state / 'python-packages.txt').read_text()
account = pwd.getpwnam('cf-b-manager')
records['manager'] = {'uid': account.pw_uid, 'gid': account.pw_gid,
    'groups': [grp.getgrgid(gid).gr_name
        for gid in os.getgrouplist(account.pw_name, account.pw_gid)]}
records['system_packages'] = subprocess.check_output([
    'dpkg-query', '-W', '-f=${Package}=${Version}\\n', 'systemd', 'sudo', 'docker-ce',
    'docker-ce-cli', 'containerd.io', 'docker-compose-plugin', 'python3', 'git'], text=True)
records['wechat_sources'] = (Path(account.pw_dir) /
    '.local/share/cf-agent-wechat/install/sources.txt').read_text()
for project, service in (('cf-agent-gateway', 'gateway'), ('cf-agent-gateway-db', 'postgres')):
    container = subprocess.check_output(['docker', 'ps', '-q', '--filter',
        'label=com.docker.compose.project=' + project, '--filter',
        'label=com.docker.compose.service=' + service], text=True).strip()
    if not container or chr(10) in container:
        raise RuntimeError('expected_one_running_service')
    proc = subprocess.check_output(
        ['docker', 'exec', container, 'cat', '/proc/1/status'], text=True)
    records[service + '_pid1_identity'] = [line for line in proc.splitlines()
        if line.startswith(('Uid:', 'Gid:'))]
print(json.dumps(records))
"""
    return json.loads(ssh(work, port, ["python3", "-c", script]).decode())


def execute_vm(work: Path, gateway_commit: str, result: dict, secret_values: list[str]) -> None:
    result["phase"] = "download_official_image"
    base = checked_download(work)
    password = secrets.token_urlsafe(32)
    hermes_key = secrets.token_urlsafe(32)
    secret_values.extend((password, hermes_key))
    password_hash = (
        run(["openssl", "passwd", "-6", "-stdin"], data=(password + "\n").encode()).decode().strip()
    )
    secret_values.append(password_hash)
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "ssh-key"])
    secret_values.append((work / "ssh-key").read_text())
    protected_write(
        work / "user-data", cloud_config((work / "ssh-key.pub").read_text().strip(), password_hash)
    )
    protected_write(work / "meta-data", json.dumps({"instance-id": "cf-b-" + secrets.token_hex(8)}))
    run(["cloud-localds", work / "seed.img", work / "user-data", work / "meta-data"])
    run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base, work / "disk.qcow2", "16G"]
    )
    run(["git", "archive", "--format=tar", "--output=" + str(work / "source.tar"), gateway_commit])
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        ssh_port = selected.getsockname()[1]
    selection = select_accelerator()
    accelerator = selection["selected"]
    result["accelerator"] = selection
    result["qemu_version"] = run(["qemu-system-x86_64", "--version"]).decode().splitlines()[0]
    result["phase"] = "boot_clean_guest"
    with TCPOnlyService(("127.0.0.1", 0), TCPOnlyHandler) as service:
        threading.Thread(target=service.serve_forever, daemon=True).start()
        process = subprocess.Popen(
            [
                "qemu-system-x86_64",
                "-no-user-config",
                "-nodefaults",
                "-accel",
                "kvm" if accelerator == "kvm" else "tcg,thread=multi",
                "-cpu",
                "host" if accelerator == "kvm" else "max",
                "-m",
                "4096",
                "-smp",
                "2",
                "-drive",
                f"file={work / 'disk.qcow2'},if=virtio,format=qcow2",
                "-drive",
                f"file={work / 'seed.img'},if=virtio,format=raw,readonly=on",
                "-nic",
                f"user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
                "-display",
                "none",
                "-serial",
                "none",
                "-monitor",
                "none",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=ENVIRONMENT,
        )
        try:
            before = wait_for_guest(work, ssh_port, process)
            ssh(work, ssh_port, ["cloud-init", "status", "--wait"], timeout=900)
            result["phase"] = "verify_pristine_booted_guest"
            result["initial_guest"] = verify_clean_guest(work, ssh_port)
            result["phase"] = "supply_source_and_necessary_inputs"
            ssh(work, ssh_port, ["mkdir", "-m", "0700", "/root/gateway-source", "/root/b-inputs"])
            ssh(
                work,
                ssh_port,
                ["tar", "-xf", "-", "-C", "/root/gateway-source"],
                data=(work / "source.tar").read_bytes(),
            )
            for path in input_files(work, service.server_address[1], hermes_key):
                # Fixed root-owned input paths; no deployment assets are supplied.
                ssh(
                    work,
                    ssh_port,
                    [
                        "python3",
                        "-c",
                        "import os,sys; "
                        "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
                        "os.write(fd,sys.stdin.buffer.read()); os.close(fd)",
                        "/root/b-inputs/" + path.name,
                    ],
                    data=path.read_bytes(),
                )
            entry = "/root/gateway-source/tests/deployment/accept_booted_debian.py"
            result["phase"] = "formal_before_reboot"
            result["before_stages"] = run_interactive(
                work,
                ssh_port,
                [
                    "python3",
                    entry,
                    "before-reboot",
                    "--manager",
                    MANAGER,
                    "--gateway-commit",
                    gateway_commit,
                    "--wechat-commit",
                    WECHAT_COMMIT,
                    "--inputs",
                    "/root/b-inputs/inputs.json",
                    "--wechat-image",
                    WECHAT_IMAGE,
                    "--wechat-runtime-uid",
                    "1000",
                    "--wechat-runtime-gid",
                    "1000",
                ],
                password,
                secret_values,
            )
            result["phase"] = "reboot_guest"
            with contextlib.suppress(VMFailure):
                ssh(work, ssh_port, ["systemctl", "reboot"], timeout=30)
            after = wait_for_guest(work, ssh_port, process, before)
            result["phase"] = "formal_after_reboot"
            run_interactive(
                work,
                ssh_port,
                [
                    "python3",
                    "/opt/cf-agent-gateway/tests/deployment/accept_booted_debian.py",
                    "after-reboot",
                    "--manager",
                    MANAGER,
                ],
                password,
                secret_values,
            )
            secret_values.extend(guest_secrets(work, ssh_port))
            report = json.loads(
                ssh(
                    work,
                    ssh_port,
                    [
                        "cat",
                        "/var/lib/cf-agent-gateway-install/boot-acceptance.json",
                    ],
                ).decode()
            )
            if report.get("result") != "passed" or before == after:
                raise VMFailure("formal_B_evidence_did_not_pass")
            # Preserve the first empty-business reboot evidence, then explicitly
            # onboard a synthetic observed employee through the formal business CLI.
            result["phase"] = "post_install_formal_business_onboarding"
            business_entry = "/opt/cf-agent-gateway/tests/deployment/authorization_reboot.py"
            onboarding = json.loads(
                ssh(
                    work,
                    ssh_port,
                    ["python3", business_entry, "prepare"],
                    timeout=300,
                ).decode()
            )
            if onboarding["before_boot_id"] != after:
                raise VMFailure("business_onboarding_not_after_empty_business_reboot")
            result["phase"] = "reboot_guest_with_authorized_business"
            with contextlib.suppress(VMFailure):
                ssh(work, ssh_port, ["systemctl", "reboot"], timeout=30)
            authorized_boot = wait_for_guest(work, ssh_port, process, after)
            result["phase"] = "formal_after_authorized_reboot"
            run_interactive(
                work,
                ssh_port,
                [
                    "python3",
                    "/opt/cf-agent-gateway/tests/deployment/accept_booted_debian.py",
                    "after-reboot",
                    "--manager",
                    MANAGER,
                ],
                password,
                secret_values,
            )
            authorization = json.loads(
                ssh(
                    work,
                    ssh_port,
                    ["python3", business_entry, "check"],
                    timeout=180,
                ).decode()
            )
            if (
                authorization["result"] != "passed"
                or authorization["after_boot_id"] != authorized_boot
            ):
                raise VMFailure("authorized_business_reboot_evidence_did_not_pass")
            build_records = guest_build_evidence(work, ssh_port)
            result.update(
                {
                    "build_and_identity_evidence": build_records,
                    "result": "passed",
                    "phase": "complete",
                    "report": report,
                    "authorization_reboot": authorization,
                    "synthetic_hermes_tcp_connections": service.accepted,
                }
            )
        finally:
            if process.poll() is None:
                with contextlib.suppress(VMFailure, subprocess.TimeoutExpired, ValueError):
                    secret_values.extend(guest_secrets(work, ssh_port))
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            service.shutdown()


def main() -> int:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or platform.system() != "Linux"
        or platform.machine() != "x86_64"
    ):
        raise SystemExit("B VM harness requires a disposable GitHub-hosted Linux amd64 runner")
    gateway_commit = os.environ.get("GATEWAY_COMMIT", "")
    if not re.fullmatch(r"[0-9a-f]{40}", gateway_commit):
        raise SystemExit("a fixed Gateway GitHub commit is required")
    if run(["git", "rev-parse", "HEAD"]).decode().strip() != gateway_commit:
        raise SystemExit("the checked-out GitHub source must match the selected commit")
    temporary_root = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    evidence = Path(os.environ["EVIDENCE_DIR"]).resolve()
    evidence.relative_to(temporary_root)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {
        "layer": "B",
        "result": "failed",
        "gateway_commit": gateway_commit,
        "wechat_commit": WECHAT_COMMIT,
        "wechat_image": WECHAT_IMAGE,
        "debian_image_url": IMAGE_BASE + IMAGE_NAME,
        "debian_image_sha512": IMAGE_SHA512,
        "manager_fixture": "cloud-init UID 1100 with standard sudo password; not auto-allocation",
        "real_components_under_test": [
            "booted Debian 13 VM",
            "systemd",
            "Docker",
            "PostgreSQL",
            "Gateway",
            "fixed Controller",
            "fixed WeChat Bootstrap",
            "password-authenticated manager",
        ],
        "synthetic": [
            "Hermes TCP listener only; no model request",
            "post-install WeChat auth-only fixture and synthetic internal message intake",
        ],
        "C": "pending separately approved real WeChat, Windows AI and Hermes acceptance",
    }
    secret_values = []
    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="cf-gateway-b-vm-", dir=temporary_root) as selected:
            execute_vm(Path(selected), gateway_commit, result, secret_values)
    except Exception as error:
        result["failure"] = str(error) if isinstance(error, VMFailure) else type(error).__name__
    finally:
        os.umask(previous_umask)
    try:
        output = evidence_json(result, secret_values)
    except VMFailure:
        result["result"] = "failed"
        output = (
            json.dumps(
                {"layer": "B", "result": "failed", "failure": "secret_in_evidence_suppressed"}
            )
            + "\n"
        )
    protected_write(evidence / "B-result.json", output)
    print("B VM result: " + result["result"] + "; only sanitized structured evidence was exported")
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
