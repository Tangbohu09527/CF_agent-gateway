"""Each public entry must import without pytest's already-populated module cache."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "cf_agent_gateway.artifact.return_config",
        "cf_agent_gateway.artifact.return_handoff",
        "cf_agent_gateway.artifact.return_routes",
        "cf_agent_gateway.gateway.app",
        "cf_agent_gateway.hermes.client",
        "cf_agent_gateway.hermes.service",
        "cf_agent_gateway.hermes.result_store",
        "cf_agent_gateway.hermes.worker",
        "cf_agent_gateway.runtime.dispatch_worker",
        "cf_agent_gateway.runtime.delivery_worker",
    ],
)
def test_artifact_return_entries_import_in_fresh_process(module: str, tmp_path: Path) -> None:
    # Keep the test runner's dependency locations, including an existing local
    # dependency target, without inheriting imported modules or real credentials.
    source = str(Path(__file__).resolve().parents[1] / "src")
    paths = list(dict.fromkeys([source, *(str(Path(p).resolve()) for p in sys.path if p)]))
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    script = (
        "import importlib,json,sys; "
        "sys.path[:0]=json.loads(sys.argv[1]); "
        "importlib.import_module(sys.argv[2]); "
        "print('import-ok')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", script, json.dumps(paths), module],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-ok"
    assert "RuntimeWarning" not in result.stderr
