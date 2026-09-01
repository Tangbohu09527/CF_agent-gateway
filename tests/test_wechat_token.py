from __future__ import annotations

import os
from pathlib import Path

import pytest

from cf_agent_gateway.runtime.errors import (
    WechatTokenFileInvalidError,
    WechatTokenSourceConflictError,
)
from cf_agent_gateway.runtime.wechat_token import (
    MAX_TOKEN_FILE_BYTES,
    TOKEN_FILE_ENV,
    resolve_wechat_token,
)

TOKEN_ENV = "CF_AGENT_WECHAT_TOKEN"
TOKEN = "development-compatible-token"


def _secure_token_file(path: Path, content: bytes = TOKEN.encode("ascii")) -> Path:
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_token_file_is_the_validated_runtime_source(tmp_path: Path) -> None:
    token_file = _secure_token_file(tmp_path / "auth-token")

    resolved = resolve_wechat_token(
        TOKEN_ENV,
        environment_reader={TOKEN_FILE_ENV: str(token_file)}.get,
    )

    assert resolved == TOKEN


@pytest.mark.parametrize("content", [b"", b"token\x00value", b"token\nvalue", b"token value"])
def test_token_file_rejects_empty_or_control_content(tmp_path: Path, content: bytes) -> None:
    token_file = _secure_token_file(tmp_path / "auth-token", content)

    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(token_file)}.get,
        )


def test_token_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(tmp_path / "missing")}.get,
        )


def test_token_file_rejects_symlink(tmp_path: Path) -> None:
    target = _secure_token_file(tmp_path / "target")
    link = tmp_path / "auth-token"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(link)}.get,
        )


def test_token_file_rejects_hardlink(tmp_path: Path) -> None:
    target = _secure_token_file(tmp_path / "target")
    link = tmp_path / "auth-token"
    try:
        os.link(target, link)
    except OSError as error:
        pytest.skip(f"hardlinks are unavailable: {error}")

    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(link)}.get,
        )


def test_token_file_rejects_oversized_content(tmp_path: Path) -> None:
    token_file = _secure_token_file(
        tmp_path / "auth-token",
        b"a" * (MAX_TOKEN_FILE_BYTES + 1),
    )

    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(token_file)}.get,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions are enforced in production")
def test_token_file_rejects_group_or_other_access(tmp_path: Path) -> None:
    token_file = _secure_token_file(tmp_path / "auth-token")
    token_file.chmod(0o640)

    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(token_file)}.get,
        )


def test_environment_and_file_sources_fail_closed(tmp_path: Path) -> None:
    token_file = _secure_token_file(tmp_path / "auth-token")

    with pytest.raises(WechatTokenSourceConflictError, match="^token_source_conflict$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={
                TOKEN_ENV: TOKEN,
                TOKEN_FILE_ENV: str(token_file),
            }.get,
        )


def test_token_file_rejects_change_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = _secure_token_file(tmp_path / "auth-token")
    before = list(os.stat(token_file, follow_symlinks=False))
    before[8] -= 1

    def stale_status(
        path: str | bytes | os.PathLike[str],
        *,
        follow_symlinks: bool,
    ) -> os.stat_result:
        assert os.fspath(path) == os.fspath(token_file)
        assert follow_symlinks is False
        return os.stat_result(before)

    monkeypatch.setattr(os, "stat", stale_status)

    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(token_file)}.get,
        )


def test_token_file_close_failure_uses_fixed_error_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = _secure_token_file(tmp_path / "auth-token")
    real_close = os.close

    def fail_close(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("sensitive operating system detail")

    monkeypatch.setattr(os, "close", fail_close)

    with pytest.raises(WechatTokenFileInvalidError, match="^token_file_invalid$"):
        resolve_wechat_token(
            TOKEN_ENV,
            environment_reader={TOKEN_FILE_ENV: str(token_file)}.get,
        )
