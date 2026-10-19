"""Bounded ZIP verification, never extraction or execution of archive members."""

import hashlib
import stat
import unicodedata
import zipfile
from pathlib import PurePosixPath

MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 2000


def verify_zip(stream, *, check_active=lambda: None):
    """Return the exact regular member manifest; caller owns a seekable stream."""
    manifest = []
    seen = set()
    regular = set()
    expanded = 0
    stream.seek(0)
    try:
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if not 0 < len(members) <= MAX_MEMBERS:
                raise ValueError("zip_member_limit")
            for member in members:
                check_active()
                name = member.filename
                parts = name.rstrip("/").split("/")
                key = name.rstrip("/").casefold()
                mode = member.external_attr >> 16
                if (
                    not name
                    or member.orig_filename != name
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or ":" in name
                    or any(p in {"", ".", ".."} for p in parts)
                    or any(unicodedata.category(c).startswith("C") for c in name)
                    or PurePosixPath(name).is_absolute()
                    or key in seen
                    or any(
                        "/".join(parts[:index]).casefold() in regular
                        for index in range(1, len(parts))
                    )
                    or not member.is_dir()
                    and any(old.startswith(key + "/") for old in seen)
                    or member.flag_bits & 1
                    or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or stat.S_ISDIR(mode)
                    and not member.is_dir()
                    or member.file_size > MAX_MEMBER_BYTES
                    or member.file_size > max(1024 * 1024, member.compress_size * 200)
                ):
                    raise ValueError("unsafe_zip_member")
                seen.add(key)
                expanded += member.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise ValueError("zip_expanded_limit")
                if member.is_dir():
                    if member.file_size:
                        raise ValueError("unsafe_zip_directory")
                    continue
                regular.add(key)
                digest = hashlib.sha256()
                size = 0
                with archive.open(member) as content:
                    while chunk := content.read(65536):
                        check_active()
                        size += len(chunk)
                        if size > member.file_size or size > MAX_MEMBER_BYTES:
                            raise ValueError("zip_member_size_changed")
                        digest.update(chunk)
                if size != member.file_size:
                    raise ValueError("zip_member_size_changed")
                manifest.append({"path": name, "size_bytes": size, "sha256": digest.hexdigest()})
        if not manifest:
            raise ValueError("empty_zip")
        return sorted(manifest, key=lambda item: item["path"])
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError) as error:
        raise ValueError("invalid_zip") from error
    finally:
        stream.seek(0)
