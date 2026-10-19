"""Trusted enterprise output descriptors; no local file bytes or credentials."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from cf_agent_gateway.hermes.return_bridge.files import safe_filename
from cf_agent_gateway.hermes.return_bridge.zip_contract import MAX_ZIP_BYTES


def normalized_path(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or "\\" in value
        or any(p in {"", ".", ".."} for p in value[1:].split("/"))
        or any(ord(c) < 32 or c == "\x7f" for c in value)
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError("invalid enterprise export path")
    return value


def export_metadata(value):
    if not isinstance(value, dict) or set(value) != {
        "source",
        "path",
        "name",
        "mime_type",
        "size_bytes",
        "sha256",
        "manifest",
    }:
        raise ValueError("invalid enterprise export metadata")
    filename = safe_filename(value["name"])
    if (
        not filename.lower().endswith(".zip")
        or value["mime_type"] != "application/zip"
        or type(value["size_bytes"]) is not int
        or not 0 < value["size_bytes"] <= MAX_ZIP_BYTES
        or not isinstance(value["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
        or not isinstance(value["source"], str)
        or not value["source"]
        or not isinstance(value["manifest"], list)
        or not 0 < len(value["manifest"]) <= 2000
    ):
        raise ValueError("invalid enterprise export metadata")
    normalized_path(value["path"])
    for member in value["manifest"]:
        if (
            not isinstance(member, dict)
            or set(member) != {"path", "size_bytes", "sha256"}
            or not isinstance(member["path"], str)
            or type(member["size_bytes"]) is not int
            or not 0 <= member["size_bytes"] <= 32 * 1024 * 1024
            or not isinstance(member["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", member["sha256"])
        ):
            raise ValueError("invalid enterprise export manifest")
    return value


@dataclass(frozen=True, slots=True)
class EnterpriseFile:
    reference: str
    filename: str
    size: int
    sha256: str
    export: dict = field(repr=False)
    kind: str = "file"
    mime_type: str = "application/zip"

    def projection(self):
        return {
            "file_ref": self.reference,
            "kind": self.kind,
            "filename": self.filename,
            "size_bytes": self.size,
            "sha256": self.sha256,
        }
