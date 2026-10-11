"""Each public entry must import without pytest's already-populated module cache."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _cold_script(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    source = str(Path(__file__).resolve().parents[1] / "src")
    paths = list(dict.fromkeys([source, *(str(Path(p).resolve()) for p in sys.path if p)]))
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    prefix = "import json,sys; sys.path[:0]=json.loads(sys.argv[1]);\n"
    return subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", prefix + script, json.dumps(paths)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def test_return_host_imports_without_gateway_database_dependencies(tmp_path: Path) -> None:
    # This is the official host's thin import boundary. Deny imports even when
    # the test runner happens to have SQLAlchemy installed elsewhere.
    result = _cold_script(
        """
import importlib, importlib.abc
class NoGatewayDatabase(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlalchemy" or fullname.startswith("sqlalchemy."):
            raise AssertionError("unexpected Gateway SQLAlchemy dependency")
        if fullname == "cf_agent_gateway.database":
            raise AssertionError("unexpected Gateway database initialization")
sys.meta_path.insert(0, NoGatewayDatabase())
for name in (
    "cf_agent_gateway.hermes",
    "cf_agent_gateway.hermes.tls",
    "cf_agent_gateway.hermes.return_bridge.files",
    "cf_agent_gateway.hermes.return_bridge.scope",
    "cf_agent_gateway.hermes.return_bridge.transport",
):
    importlib.import_module(name)
assert "sqlalchemy" not in sys.modules
assert "cf_agent_gateway.database" not in sys.modules
assert "cf_agent_gateway.hermes.worker" not in sys.modules
print("thin-import-ok")
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "thin-import-ok"


def test_lazy_hermes_exports_keep_original_from_import_behavior(tmp_path: Path) -> None:
    result = _cold_script(
        """
import cf_agent_gateway.hermes as public
assert "cf_agent_gateway.hermes.worker" not in sys.modules
from cf_agent_gateway.hermes import HermesClient, HermesDispatchWorker
from cf_agent_gateway.hermes.client import HermesClient as expected_client
from cf_agent_gateway.hermes.worker import HermesDispatchWorker as expected_worker
assert HermesClient is expected_client
assert HermesDispatchWorker is expected_worker
assert public.HermesClient is HermesClient
namespace = {}
exec("from cf_agent_gateway.hermes import *", namespace)
assert set(namespace) - {"__builtins__"} == set(public.__all__)
assert all(name in dir(public) for name in public.__all__)
try:
    public.nonexistent_export
except AttributeError:
    pass
else:
    raise AssertionError("unknown attribute did not fail")
print("public-import-ok")
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "public-import-ok"


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
