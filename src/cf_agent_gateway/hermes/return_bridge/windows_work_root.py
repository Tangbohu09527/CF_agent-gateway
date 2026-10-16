"""Bind a new return work directory to the verified ordinary Windows runner.

Existing directories are observed only. This module never repairs their ACLs,
impersonates another user, starts processes, opens SQLite, or consumes a grant.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from pathlib import Path

from .enablement import EnablementError

DACL = "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"


class WorkRootError(EnablementError):
    pass


def _need(condition, code):
    if not condition:
        raise WorkRootError(code)


def _api():
    _need(os.name == "nt", "windows_work_root_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, pointer(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        pointer(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, pointer(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        pointer(wintypes.LPVOID),
        pointer(wintypes.DWORD),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        pointer(wintypes.BOOL),
        pointer(wintypes.LPVOID),
        pointer(wintypes.BOOL),
    ]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        pointer(wintypes.LPVOID),
        wintypes.LPVOID,
        pointer(wintypes.LPVOID),
        wintypes.LPVOID,
        pointer(wintypes.LPVOID),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID,
        pointer(wintypes.WORD),
        pointer(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    return kernel, advapi


def _sid(kernel, advapi, pointer):
    text = wintypes.LPWSTR()
    _need(advapi.ConvertSidToStringSidW(pointer, ctypes.byref(text)), "runtime_token_unavailable")
    try:
        value = text.value
        _need(re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", value or ""), "runtime_token_unavailable")
        return value
    finally:
        kernel.LocalFree(text)


def process_user(pid):
    """Read TokenUser/elevation from one opened process; no command line or env."""
    _need(type(pid) is int and pid > 0, "runtime_token_unavailable")
    kernel, advapi = _api()
    process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    _need(process, "runtime_token_unavailable")
    token = wintypes.HANDLE()
    try:
        _need(
            advapi.OpenProcessToken(process, 0x8, ctypes.byref(token)), "runtime_token_unavailable"
        )
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        _need(0 < needed.value <= 65536, "runtime_token_unavailable")
        data = ctypes.create_string_buffer(needed.value)
        _need(
            advapi.GetTokenInformation(token, 1, data, len(data), ctypes.byref(needed)),
            "runtime_token_unavailable",
        )
        sid_pointer = ctypes.cast(data, ctypes.POINTER(wintypes.LPVOID)).contents
        sid = _sid(kernel, advapi, sid_pointer)
        elevated = wintypes.DWORD()
        _need(
            advapi.GetTokenInformation(
                token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(needed)
            ),
            "runtime_token_unavailable",
        )
        _need(elevated.value in (0, 1), "runtime_token_unavailable")
        return {"sid": sid, "elevated": bool(elevated.value)}
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def current_user():
    value = process_user(os.getpid())
    _need(value["elevated"] is False, "ordinary_hermes_identity_required")
    return value


def require_user(expected):
    _need(
        type(expected) is dict
        and set(expected) == {"sid", "elevated"}
        and type(expected["sid"]) is str
        and expected["elevated"] is False,
        "runtime_user_proof_invalid",
    )
    _need(current_user() == expected, "installer_runtime_user_changed")


@contextmanager
def _descriptor(kernel, advapi, text):
    pointer = wintypes.LPVOID()
    _need(
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            text, 1, ctypes.byref(pointer), None
        ),
        "work_root_security_unavailable",
    )
    try:
        yield pointer
    finally:
        kernel.LocalFree(pointer)


def _acl_bytes(pointer):
    _need(pointer, "work_root_security_unavailable")
    size = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))[1]
    _need(8 <= size <= 65535, "work_root_security_unavailable")
    return ctypes.string_at(pointer, size)


def _security(path, user):
    kernel, advapi = _api()
    owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
    _need(
        advapi.GetNamedSecurityInfoW(
            str(path),
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        == 0,
        "work_root_security_unavailable",
    )
    try:
        _need(_sid(kernel, advapi, owner) == user["sid"], "work_root_owner_mismatch")
        control, revision = wintypes.WORD(), wintypes.DWORD()
        _need(
            advapi.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            )
            and control.value & 0x1000,
            "work_root_policy_mismatch",
        )
        with _descriptor(kernel, advapi, DACL) as expected:
            present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), wintypes.LPVOID()
            _need(
                advapi.GetSecurityDescriptorDacl(
                    expected, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
                )
                and present,
                "work_root_security_unavailable",
            )
            _need(_acl_bytes(dacl) == _acl_bytes(acl), "work_root_policy_mismatch")
    finally:
        kernel.LocalFree(descriptor)


@contextmanager
def _pin(path, *, work=False):
    info = path.lstat()
    _need(
        stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
        "work_root_path_unsafe",
    )
    kernel, _ = _api()
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    # Actual ordinary-token access, not a simulated AccessCheck. Root additionally
    # requires list/create-file/create-subdirectory/read-attributes/read-control.
    handle = kernel.CreateFileW(str(path), 0x20087 if work else 0x1, 3, None, 3, 0x02200000, None)
    _need(handle != ctypes.c_void_p(-1).value, "work_root_runtime_access_denied")
    try:

        class Attributes(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        value = Attributes()
        kernel.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        _need(
            kernel.GetFileInformationByHandleEx(
                handle, 9, ctypes.byref(value), ctypes.sizeof(value)
            )
            and value.attributes & 0x10
            and not value.attributes & 0x400,
            "work_root_path_unsafe",
        )
        yield
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _parents(path):
    _need(
        path.is_absolute() and ".." not in path.parts and not path.drive.startswith("\\\\"),
        "work_root_path_unsafe",
    )
    with ExitStack() as stack:
        for parent in reversed(path.parents):
            stack.enter_context(_pin(parent))
        yield


def check(path: Path, user, *, missing_allowed=False):
    require_user(user)
    with _parents(path):
        if missing_allowed and not path.exists():
            return
        with _pin(path, work=True):
            _security(path, user)


def prepare(path: Path, user):
    """Create one leaf atomically; never change an existing security descriptor."""
    require_user(user)
    with _parents(path):
        if not path.exists():
            kernel, advapi = _api()

            class SecurityAttributes(ctypes.Structure):
                _fields_ = [
                    ("length", wintypes.DWORD),
                    ("descriptor", wintypes.LPVOID),
                    ("inherit", wintypes.BOOL),
                ]

            kernel.CreateDirectoryW.argtypes = [
                wintypes.LPCWSTR,
                ctypes.POINTER(SecurityAttributes),
            ]
            kernel.CreateDirectoryW.restype = wintypes.BOOL
            with _descriptor(kernel, advapi, "O:" + user["sid"] + DACL) as descriptor:
                attributes = SecurityAttributes(
                    ctypes.sizeof(SecurityAttributes), descriptor, False
                )
                _need(
                    kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)),
                    "work_root_create_failed_preserve",
                )
        with _pin(path, work=True):
            _security(path, user)
