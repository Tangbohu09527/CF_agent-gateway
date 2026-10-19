"""Audit the actual official PM environment against fixed core + default extras."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

COMMIT = "427d605d4985c2d49a795493ef6c5d66413057d1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    installed = {
        canonicalize_name(item.metadata["Name"]): item.version
        for item in importlib.metadata.distributions()
    }
    key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
    facts = json.loads((source.parent / "installs" / key / "facts.json").read_bytes())
    extras = facts["packages"]["venv"]["extras"]
    requirements = list(project["dependencies"])
    visited = set()
    pending = list(extras)
    while pending:
        extra = pending.pop()
        if extra in visited:
            continue
        visited.add(extra)
        for value in project["optional-dependencies"][extra]:
            requirement = Requirement(value)
            if canonicalize_name(requirement.name) == canonicalize_name(project["name"]):
                pending.extend(requirement.extras)
            else:
                requirements.append(value)
    missing, mismatches, checked = [], [], []
    for value in requirements:
        requirement = Requirement(value)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        version = installed.get(name)
        checked.append(value)
        if version is None:
            missing.append(name)
        elif requirement.specifier and not requirement.specifier.contains(version):
            mismatches.append({"requirement": value, "installed": version})
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    clean = not subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True
    ).strip()
    result = {
        "hermes_commit": commit,
        "installer": "official scripts/install.sh -> pm.cli install -> locked default [all]",
        "installer_stage": "python-deps",
        "installer_exit": 0,
        "optional_tool_opt_outs": ["agent-browser", "cua-driver"],
        "official_default_extras": extras,
        "expanded_extras": sorted(visited),
        "python_version": list(sys.version_info[:3]),
        "source_clean": clean,
        "source_input_sha256": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock", "pm/lock.json")
        },
        "checked_requirements": checked,
        "installed_versions": dict(sorted(installed.items())),
        "missing": missing,
        "mismatches": mismatches,
    }
    result["all_requirements_satisfied"] = (
        commit == COMMIT
        and clean
        and extras == ["all"]
        and sys.version_info[:3] == (3, 14, 7)
        and not missing
        and not mismatches
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "installed_versions"}))
    return 0 if result["all_requirements_satisfied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
