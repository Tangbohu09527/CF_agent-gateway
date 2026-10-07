"""Read a bounded task file through pinned, non-link handles.

This constrains the return tool; it is not an OS sandbox for other host tools.
The caller receives immutable bytes, so upload never reopens a model-supplied path.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_BYTES = 1_048_576


class ReturnBridgeError(ValueError):
    """Public errors never include a path, URL, credential or server response."""

    def __init__(self, code: str = "return_unavailable"):
        self.code = code
        super().__init__(code)


def safe_filename(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReturnBridgeError("invalid_file_reference")
    try:
        if len(value.encode("utf-8")) > 255:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ReturnBridgeError("invalid_file_reference") from None
    if value.endswith((".", " ")) or value in {".", ".."}:
        raise ReturnBridgeError("invalid_file_reference")
    if any(
        c in '<>:"/\\|?*'
        or unicodedata.category(c).startswith("C")
        or unicodedata.category(c) in {"Zl", "Zp"}
        for c in value
    ):
        raise ReturnBridgeError("invalid_file_reference")
    reserved = {"AUX", "CON", "CONIN$", "CONOUT$", "NUL", "PRN"}
    reserved |= {f"{prefix}{index}" for prefix in ("COM", "LPT") for index in "123456789¹²³"}
    if value.split(".", 1)[0].rstrip(" .").upper() in reserved:
        raise ReturnBridgeError("invalid_file_reference")
    return value


def reference_parts(reference: str) -> tuple[str, ...]:
    if not isinstance(reference, str) or not reference or "\\" in reference:
        raise ReturnBridgeError("invalid_file_reference")
    parts = tuple(reference.split("/"))
    if PurePosixPath(reference).is_absolute() or len(parts) > 16:
        raise ReturnBridgeError("invalid_file_reference")
    for part in parts:
        safe_filename(part)
    return parts


@dataclass(frozen=True, slots=True)
class TaskFile:
    reference: str
    filename: str
    kind: str
    mime_type: str
    content: bytes = field(repr=False)


def read_task_file(
    root: Path,
    reference: str,
    kind: str,
    filename: str | None = None,
    *,
    max_bytes: int = MAX_BYTES,
) -> TaskFile:
    parts = reference_parts(reference)
    filename = safe_filename(filename if filename is not None else parts[-1])
    if not isinstance(kind, str) or kind not in {"file", "image"}:
        raise ReturnBridgeError("invalid_file_type")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise ReturnBridgeError("invalid_file_type")
    try:
        content = (
            _windows_read(root, parts, max_bytes)
            if os.name == "nt"
            else _posix_read(root, parts, max_bytes)
        )
    except (OSError, ValueError):
        raise ReturnBridgeError("file_unavailable") from None
    if (
        kind == "file"
        and filename.lower().endswith(".pdf")
        and content.startswith(b"%PDF-")
        and b"%%EOF" in content[-1024:]
    ):
        mime = "application/pdf"
    elif (
        kind == "image"
        and filename.lower().endswith(".png")
        and content.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        and content.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    ):
        mime = "image/png"
    else:
        raise ReturnBridgeError("invalid_file_type")
    return TaskFile(reference, filename, kind, mime, content)


def _read_descriptor(fd: int, limit: int) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
        raise ValueError()
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ValueError()
    after = os.fstat(fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_nlink,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_nlink,
    ) or total != before.st_size:
        raise ValueError()
    return b"".join(chunks)


def _posix_read(root: Path, parts: tuple[str, ...], limit: int) -> bytes:
    root = Path(os.path.abspath(root))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        directory = os.open(root.anchor, directory_flags)
        stack.callback(os.close, directory)
        # Pin every ancestor; a renamed/replaced path cannot redirect an openat.
        for part in (*root.parts[1:], *parts[:-1]):
            directory = os.open(part, directory_flags, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        stack.callback(os.close, descriptor)
        content = _read_descriptor(descriptor, limit)
        opened = os.fstat(descriptor)
        named = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError()
        return content


def _windows_read(root: Path, parts: tuple[str, ...], limit: int) -> bytes:
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

    def open_handle(path, directory):
        # OPEN_REPARSE_POINT opens the link itself; inspect it before proceeding.
        # No FILE_SHARE_DELETE pins ancestors against rename/replacement. The
        # file itself also denies FILE_SHARE_WRITE for the whole bounded read.
        handle = create(
            str(path),
            0x1 if directory else 0x80000000,
            0x3 if directory else 0x1,
            None,
            3,
            0x00200000 | (0x02000000 if directory else 0),
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise OSError()
        info = FileInfo()
        if not get_info(handle, ctypes.byref(info)) or info.attributes & 0x400:
            close(handle)
            raise ValueError()
        if bool(info.attributes & 0x10) != directory or (not directory and info.links != 1):
            close(handle)
            raise ValueError()
        return handle

    with ExitStack() as stack:
        directory = Path(root.anchor)
        stack.callback(close, open_handle(directory, True))
        for part in (*root.parts[1:], *parts[:-1]):
            directory /= part
            stack.callback(close, open_handle(directory, True))
        handle = open_handle(directory / parts[-1], False)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except OSError:
            close(handle)
            raise
        stack.callback(os.close, descriptor)
        return _read_descriptor(descriptor, limit)
