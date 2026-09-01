from __future__ import annotations

import os
import stat
from collections.abc import Callable

from cf_agent_gateway.runtime.errors import (
    WechatTokenEnvironmentError,
    WechatTokenFileInvalidError,
    WechatTokenSourceConflictError,
)

TOKEN_FILE_ENV = "CF_AGENT_WECHAT_TOKEN_FILE"
MAX_TOKEN_FILE_BYTES = 4096


def resolve_wechat_token(
    token_env: str,
    *,
    environment_reader: Callable[[str], str | None] = os.getenv,
) -> str:
    """Resolve exactly one validated agent-wechat token source."""

    token = environment_reader(token_env)
    token_file = environment_reader(TOKEN_FILE_ENV)
    if token is not None and token_file is not None:
        raise WechatTokenSourceConflictError()
    if token_file is not None:
        return _read_token_file(token_file)
    if token is None or not token.strip():
        raise WechatTokenEnvironmentError(token_env)
    return token


def _read_token_file(path: str) -> str:
    descriptor: int | None = None
    try:
        if not isinstance(path, str) or not path:
            raise ValueError
        before = os.stat(path, follow_symlinks=False)
        _validate_file_status(before)

        flags = os.O_RDONLY
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        _validate_file_status(opened)
        ctime_changed = os.name == "posix" and before.st_ctime_ns != opened.st_ctime_ns
        if before[:7] != opened[:7] or before.st_mtime_ns != opened.st_mtime_ns or ctime_changed:
            raise ValueError

        content = os.read(descriptor, MAX_TOKEN_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        _validate_file_status(after)
        if (
            opened[:7] != after[:7]
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
            or len(content) != after.st_size
        ):
            raise ValueError
        if not content or len(content) > MAX_TOKEN_FILE_BYTES:
            raise ValueError
        if any(byte < 0x21 or byte > 0x7E for byte in content):
            raise ValueError
        return content.decode("ascii")
    except (OSError, UnicodeError, ValueError, TypeError):
        raise WechatTokenFileInvalidError() from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raise WechatTokenFileInvalidError() from None


def _validate_file_status(status: os.stat_result) -> None:
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise ValueError
    if not 1 <= status.st_size <= MAX_TOKEN_FILE_BYTES:
        raise ValueError
    if os.name == "posix":
        if stat.S_IMODE(status.st_mode) not in {0o400, 0o600}:
            raise ValueError
        if (status.st_uid, status.st_gid) != (os.geteuid(), os.getegid()):
            raise ValueError
