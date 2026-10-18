"""Exclusive, bounded staging of already-verified inbound bytes.

No arbitrary destination, ACL repair, owner change, or model-visible host path.
Failed creations stay private and consume an attempt in their request scope.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from cf_agent_gateway.hermes.return_bridge.files import (
    ReturnBridgeError,
    _read_descriptor,
    safe_filename,
    task_file_from_bytes,
)


def inbound_bytes(stream, size_bytes, sha256, limit, check_active):
    if (
        type(size_bytes) is not int
        or not 0 < size_bytes <= limit
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        raise ReturnBridgeError("inbound_return_metadata_invalid")
    chunks, total = [], 0
    try:
        while True:
            check_active()
            chunk = stream.read(min(65536, size_bytes + 1 - total))
            check_active()
            if not isinstance(chunk, bytes):
                raise ReturnBridgeError("inbound_return_metadata_invalid")
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > size_bytes:
                raise ReturnBridgeError("inbound_return_bytes_changed")
    except ReturnBridgeError:
        raise
    except Exception:
        raise ReturnBridgeError("inbound_return_inactive") from None
    content = b"".join(chunks)
    if len(content) != size_bytes or hashlib.sha256(content).hexdigest() != sha256:
        raise ReturnBridgeError("inbound_return_bytes_changed")
    return content


def inbound_file(filename, content):
    filename = safe_filename(filename)
    kind = "image" if filename.lower().endswith(".png") else "file"
    extension = ".png" if kind == "image" else ".pdf"
    reference = f"inbound-{uuid4().hex}{extension}"
    return task_file_from_bytes(reference, filename, kind, content)


def write_staged_file(root, file, check_active, on_created):
    """Create one generated leaf, retaining all ancestor handles until readback."""
    check_active()
    try:
        if os.name == "nt":
            _windows_write(root, file, check_active, on_created)
        else:
            _posix_write(root, file, check_active, on_created)
    except ReturnBridgeError:
        raise
    except (OSError, ValueError):
        raise ReturnBridgeError("inbound_return_file_unavailable") from None
    check_active()


def _write_readback(descriptor, file, check_active, on_created):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
        raise ValueError()
    on_created((info.st_dev, info.st_ino))
    remaining = memoryview(file.content)
    while remaining:
        check_active()
        count = os.write(descriptor, remaining[:65536])
        if count <= 0:
            raise ValueError()
        remaining = remaining[count:]
    check_active()
    os.fsync(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if _read_descriptor(descriptor, len(file.content), check_active) != file.content:
        raise ReturnBridgeError("inbound_return_bytes_changed")


def _posix_write(root, file, check_active, on_created):
    root = Path(os.path.abspath(root))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        directory = os.open(root.anchor, flags)
        stack.callback(os.close, directory)
        for part in root.parts[1:]:
            check_active()
            directory = os.open(part, flags, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(
            file.reference,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        stack.callback(os.close, descriptor)
        _write_readback(descriptor, file, check_active, on_created)
        opened = os.fstat(descriptor)
        named = os.stat(file.reference, dir_fd=directory, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError()


def _windows_write(root, file, check_active, on_created):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    get_info = kernel.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInfo)]
    get_info.restype = wintypes.BOOL
    root = Path(os.path.abspath(root))
    if root.drive.startswith("\\") or str(root).startswith("\\\\?"):
        raise ValueError()

    def opened(path, directory):
        check_active()
        # Ancestors cannot be renamed. CREATE_NEW rejects every existing leaf;
        # it never truncates a link, reparse point, or someone else's file.
        handle = create(
            str(path),
            0x1 if directory else 0xC0000000,
            0x3 if directory else 0x1,
            None,
            3 if directory else 1,
            0x00200000 | (0x02000000 if directory else 0),
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise OSError()
        info = FileInfo()
        if (
            not get_info(handle, ctypes.byref(info))
            or info.attributes & 0x400
            or bool(info.attributes & 0x10) != directory
            or (not directory and info.links != 1)
        ):
            close(handle)
            raise ValueError()
        return handle

    with ExitStack() as stack:
        directory = Path(root.anchor)
        stack.callback(close, opened(directory, True))
        for part in root.parts[1:]:
            directory /= part
            stack.callback(close, opened(directory, True))
        handle = opened(directory / file.reference, False)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except OSError:
            close(handle)
            raise
        stack.callback(os.close, descriptor)
        _write_readback(descriptor, file, check_active, on_created)
