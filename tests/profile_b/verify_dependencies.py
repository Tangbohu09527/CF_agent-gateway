"""Reuse PR23's expanded [all] audit, with an explicit isolated PM facts path."""

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installer", choices=["powershell", "bash"], required=True)
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    installed = {
        canonicalize_name(item.metadata["Name"]): item.version
        for item in importlib.metadata.distributions()
    }
    extras = json.loads(args.facts.read_bytes())["packages"]["venv"]["extras"]
    requirements = list(project["dependencies"])
    pending, visited = list(extras), set()
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
    checked, missing, mismatches = [], [], []
    for value in requirements:
        requirement = Requirement(value)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        checked.append(value)
        version = installed.get(canonicalize_name(requirement.name))
        if version is None:
            missing.append(requirement.name)
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
        "source_clean": clean,
        "installer": "official scripts/install."
        + ("ps1" if args.installer == "powershell" else "sh"),
        "installer_stage": "python-deps",
        "official_default_extras": extras,
        "python_version": list(sys.version_info[:3]),
        "expanded_extras": sorted(visited),
        "checked_requirements": checked,
        "missing": missing,
        "mismatches": mismatches,
        "source_input_sha256": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock", "pm/lock.json")
        },
        "installed_versions": dict(sorted(installed.items())),
    }
    result["all_requirements_satisfied"] = (
        commit == COMMIT
        and clean
        and extras == ["all"]
        and sys.version_info[:3] == (3, 14, 7)
        and not missing
        and not mismatches
    )
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "all_requirements_satisfied": result["all_requirements_satisfied"],
                "source_clean": clean,
                "checked": len(checked),
                "missing": missing,
                "mismatches": mismatches,
            }
        )
    )
    return 0 if result["all_requirements_satisfied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
