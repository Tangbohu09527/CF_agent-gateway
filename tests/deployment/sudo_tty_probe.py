"""A-only real sudo/PTY check of the unchanged B management helper.

The test password never enters argv, environment, a pipe, or persisted output.
This is an authentication regression, not a systemd or reboot acceptance.
"""

from __future__ import annotations

import contextlib
import errno
import importlib.util
import json
import os
import pty
import select
import signal
import stat
import subprocess
import termios
import time
from pathlib import Path


def probe(source: Path, evidence: Path, manager: str) -> None:
    rule = Path("/etc/sudoers.d/a-manager-access")
    metadata = rule.lstat()
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
    original = rule.read_bytes()
    assert original == f"Defaults:{manager} timestamp_type=global\n".encode()
    expected = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
    assert expected == (0, 0, 0o440)
    password_file = Path("/root/a-manager-password")
    password_metadata = password_file.lstat()
    assert stat.S_ISREG(password_metadata.st_mode)
    assert password_metadata.st_uid == 0 and stat.S_IMODE(password_metadata.st_mode) == 0o600
    password = password_file.read_bytes().strip()
    assert password
    record = {"layer": "A", "scope": "B helper sudo authentication only; no boot/reboot"}
    try:
        rule.write_bytes(f"Defaults:{manager} timestamp_type=tty\n".encode())
        subprocess.run(["visudo", "-cf", str(rule)], capture_output=True, check=True)
        policy = subprocess.check_output(["sudo", "-l", "-U", manager], text=True)
        assert "use_pty" in policy and "!use_pty" not in policy
        assert "timestamp_type=tty" in policy and "timestamp_type=global" not in policy
        subprocess.run(
            ["sudo", "-H", "-u", manager, "--", "sudo", "-K"],
            capture_output=True,
            check=True,
        )
        record.update({"use_pty": True, "timestamp_type": "tty", "old_cache_removed": True})
        child, terminal = pty.fork()
        if child == 0:
            try:
                os.environ["LC_ALL"] = "C.UTF-8"
                spec = importlib.util.spec_from_file_location(
                    "boot_acceptance", source / "tests/deployment/accept_booted_debian.py"
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.run_manager_stage(
                    manager,
                    ["sudo", "-n", "--", str(source / "deploy/wechat-runtime-control"), "contract"],
                )
            except BaseException:
                os._exit(1)
            os._exit(0)
        captured = bytearray()
        sent = False
        status = None
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(terminal, 4096)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        chunk = b""
                    captured.extend(chunk)
                    assert len(captured) < 65536, "unexpected PTY output size"
                prompt = f"password for {manager}:".encode() in captured
                echo_disabled = not (termios.tcgetattr(terminal)[3] & termios.ECHO)
                if prompt and echo_disabled and not sent:
                    os.write(terminal, password + b"\n")
                    sent = True
                    record.update(
                        {"password_prompt_seen": True, "echo_disabled_before_input": True}
                    )
                completed, status = os.waitpid(child, os.WNOHANG)
                if completed:
                    record["helper_exit_code"] = os.waitstatus_to_exitcode(status)
                    break
                status = None
            assert status is not None, "default sudo PTY helper timed out"
            assert password not in captured, "test password echoed; output withheld"
            assert sent, "helper did not request actual manager authentication"
            assert os.waitstatus_to_exitcode(status) == 0, "default sudo PTY helper failed"
            record["result"] = "passed"
        finally:
            if status is None:
                # Only the child session created by this probe is terminated.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child, signal.SIGKILL)
                os.waitpid(child, 0)
            os.close(terminal)
    finally:
        rule.write_bytes(original)
        restored = rule.lstat()
        assert rule.read_bytes() == original
        assert (restored.st_uid, restored.st_gid, stat.S_IMODE(restored.st_mode)) == expected
        record["a_sudoers_bytes_owner_mode_restored"] = True
        (evidence / "sudo-default-tty-probe.json").write_text(json.dumps(record, indent=2) + "\n")
