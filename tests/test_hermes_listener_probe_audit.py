"""The explicit real-CLI probe's containment must fail closed in its children."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def _probe():
    path = Path(__file__).parent / "hermes_listener_probe/probe.py"
    spec = importlib.util.spec_from_file_location("listener_probe_audit_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sitecustomize_failure_terminates_instead_of_running_unaudited(tmp_path):
    probe = _probe()
    (tmp_path / "sitecustomize.py").write_text(probe.audit_bootstrap(), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CF_LISTENER_PROBE_")}
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-B", "-c", "print('must-not-run')"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 97
    assert "must-not-run" not in result.stdout


def test_audit_denies_external_profile_writes_network_and_child_commands(tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("synthetic-secret", encoding="utf-8")
    (case / "ports.json").write_text("[32199]", encoding="utf-8")
    probe_path = Path(__file__).parent / "hermes_listener_probe"
    env = dict(os.environ)
    env.update(
        {
            "CF_LISTENER_PROBE_CASE": str(case),
            "CF_LISTENER_PROBE_SOURCE": str(case / "empty-source"),
            "CF_LISTENER_PROBE_REPOSITORY": str(case / "empty-repository"),
        }
    )
    script = """
import json, os, socket, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from probe import install_audit
install_audit()
outside = Path(sys.argv[2])
checks = {}
for name, operation in {
    'outside_read': lambda: outside.read_bytes(),
    'outside_write': lambda: outside.write_bytes(b'not-written'),
    'external_connect': lambda: socket.socket().connect(('192.0.2.1', 443)),
    'loopback_model_connect': lambda: socket.socket().connect(('127.0.0.1', 1)),
    'unapproved_child': lambda: subprocess.run([sys.executable, '-c', 'pass']),
}.items():
    try:
        operation()
    except PermissionError:
        checks[name] = True
    else:
        checks[name] = False
print(json.dumps(checks))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(probe_path), str(outside)],
        env=env,
        cwd=case,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    checks = json.loads(result.stdout.strip())
    assert len(checks) == 5 and all(checks.values())
    assert outside.read_text() == "synthetic-secret"
