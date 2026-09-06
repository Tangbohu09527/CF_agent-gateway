"""Guard the VM harness without starting VMs; B still requires the real CI job."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness():
    spec = importlib.util.spec_from_file_location(
        "booted_vm_harness_under_test", Path(__file__).parent / "deployment/run_booted_vm.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("manifest_valid", [False, True])
def test_download_rejects_changed_manifest_or_tampered_disk(
    harness, tmp_path, monkeypatch, manifest_valid
):
    checksum = harness.IMAGE_SHA512 if manifest_valid else "a" * 128
    calls = []

    def opened(url, timeout):
        calls.append(url)
        return io.BytesIO(
            (checksum + "  " + harness.IMAGE_NAME + "\n").encode()
            if url.endswith("SHA512SUMS")
            else b"tampered image"
        )

    monkeypatch.setattr(harness, "build_opener", lambda *args: SimpleNamespace(open=opened))
    with pytest.raises(harness.VMFailure, match="checksum"):
        harness.checked_download(tmp_path)
    assert len(calls) == (2 if manifest_valid else 1)


@pytest.mark.parametrize("secret", ["test-private-password", "line one\nline two"])
def test_export_rejects_plain_and_json_escaped_secrets(harness, secret):
    with pytest.raises(harness.VMFailure, match="secret_in_evidence"):
        harness.evidence_json({"nested": {"value": secret}}, [secret])
    assert json.loads(harness.evidence_json({"layer": "B"}, [secret])) == {"layer": "B"}


def test_seed_supplies_only_initial_accounts_without_installation_assets(harness):
    config = json.loads(
        harness.cloud_config("ssh-ed25519 isolated-public-key", "test-hash").split("\n", 1)[1]
    )
    assert set(config) == {"disable_root", "ssh_pwauth", "users"}
    manager = next(user for user in config["users"] if user["name"] == harness.MANAGER)
    assert manager["uid"] == 1100
    assert manager["groups"] == ["sudo"]
    assert manager["sudo"] is None and manager["lock_passwd"] is False


def test_ssh_uses_private_identity_and_ignores_existing_configuration(harness, tmp_path):
    options = harness.ssh_options(tmp_path, 22022)
    assert options[:2] == ["-F", "/dev/null"]
    for required in (
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "GlobalKnownHostsFile=/dev/null",
        "UserKnownHostsFile=" + str(tmp_path / "known-hosts"),
        "ProxyCommand=none",
        "ProxyJump=none",
        "ControlMaster=no",
        "ControlPath=none",
        "BatchMode=yes",
    ):
        assert required in options
    assert options[options.index("-i") + 1] == str(tmp_path / "ssh-key")


@pytest.mark.parametrize("outcome", ["missing_access", "failed", "timeout", "success"])
def test_kvm_is_probed_and_tcg_retains_specific_reason(harness, monkeypatch, outcome):
    monkeypatch.setattr(harness.os, "access", lambda *args: outcome != "missing_access")
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        if outcome == "failed":
            raise harness.VMFailure("command_failed:qemu-system-x86_64")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 15)
        return b""

    monkeypatch.setattr(harness, "run", probe)
    result = harness.select_accelerator()
    assert result["selected"] == ("kvm" if outcome == "success" else "tcg")
    assert (
        result["kvm_probe"]
        == {
            "missing_access": "device_not_readable_and_writable",
            "failed": "initialization_failed",
            "timeout": "initialization_timeout",
            "success": "initialized",
        }[outcome]
    )
    assert bool(calls) == (outcome != "missing_access")


@pytest.mark.parametrize("problem", [None, "missing_stages", "credential"])
def test_pty_preserves_partial_output_across_timeout_and_requires_complete_evidence(
    harness, tmp_path, monkeypatch, capsys, problem
):
    class Timeout(Exception):
        pass

    class End(Exception):
        pass

    stages = [
        "system",
        "controller",
        "build",
        "configure",
        "database",
        "migrate",
        "initialize",
        "start",
        "diagnose",
        "boot-service",
    ]
    chunks = ["[sudo] password for ", Timeout, harness.MANAGER + ":"]
    chunks += [
        "test-private-password" if problem == "credential" else "\r\n",
        ""
        if problem == "missing_stages"
        else "\n".join("B official stage passed: " + stage for stage in stages),
        "\nB preparation passed. Reboot this disposable host manually.\n",
        End,
    ]
    sent = []

    class Child:
        exitstatus = 0

        def read_nonblocking(self, **kwargs):
            chunk = chunks.pop(0)
            if isinstance(chunk, type) and issubclass(chunk, Exception):
                raise chunk
            return chunk

        def waitnoecho(self, **kwargs):
            return True

        def sendline(self, value):
            sent.append(value)

        def close(self):
            pass

        def isalive(self):
            return False

    monkeypatch.setitem(
        sys.modules,
        "pexpect",
        SimpleNamespace(spawn=lambda *args, **kwargs: Child(), TIMEOUT=Timeout, EOF=End),
    )
    if problem:
        with pytest.raises(harness.VMFailure):
            harness.run_interactive(
                tmp_path,
                22022,
                ["before-reboot"],
                "test-private-password",
                ["test-private-password"],
            )
    else:
        assert (
            harness.run_interactive(
                tmp_path,
                22022,
                ["before-reboot"],
                "test-private-password",
                ["test-private-password"],
            )
            == stages
        )
    assert sent == ["test-private-password"]
    assert "test-private-password" not in capsys.readouterr().out


def test_harness_refuses_non_disposable_environment_before_any_command(harness, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(harness, "run", lambda *args, **kwargs: pytest.fail("unexpected command"))
    with pytest.raises(SystemExit, match="disposable GitHub-hosted"):
        harness.main()


def test_embedded_guest_scripts_are_valid_python(harness, tmp_path, monkeypatch):
    def inspect_script(work, port, command):
        assert command[:2] == ["python3", "-c"]
        compile(command[2], "guest-evidence", "exec")
        return b"{}"

    monkeypatch.setattr(harness, "ssh", inspect_script)
    harness.verify_clean_guest(tmp_path, 22022)
    harness.guest_build_evidence(tmp_path, 22022)
    harness.guest_secrets(tmp_path, 22022)
