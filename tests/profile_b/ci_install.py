"""Ephemeral Linux CI only: official locked dependency stage, never pip subsets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from cf_agent_gateway.profile_b.demo import isolated_environment


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("CI-only installer")
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("runtime", type=Path)
    args = parser.parse_args()
    source, runtime = args.source.resolve(), args.runtime.resolve()
    env = isolated_environment(runtime / "hermes-home", runtime / "tools", runtime / "os-home")
    env.update(UV_CACHE_DIR=str(runtime / "cache"), UV_PYTHON_INSTALL_DIR=str(runtime / "python"))
    subprocess.run(
        [
            "bash",
            str(source / "scripts/install.sh"),
            "--dir",
            str(source),
            "--hermes-home",
            str(runtime / "hermes-home"),
            "--stage",
            "python-deps",
            "--non-interactive",
            "--skip-browser",
            "--skip-computer-use",
        ],
        env=env,
        check=True,
    )
    install_key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
    facts = runtime / "hermes-home" / "installs" / install_key / "facts.json"
    python = Path(json.loads(facts.read_bytes())["packages"]["venv"]["environment"]) / "bin/python"
    subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "tests/profile_b/verify_dependencies.py",
            "--hermes-source",
            str(source),
            "--facts",
            str(facts),
            "--output",
            str(runtime / "dependency-summary.json"),
            "--installer",
            "bash",
        ],
        check=True,
    )
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        output.write("CF_OFFICIAL_PROFILE_PYTHON=" + str(python) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
