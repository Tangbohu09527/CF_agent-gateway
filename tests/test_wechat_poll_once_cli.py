from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cf_agent_gateway import wechat_poll_once
from cf_agent_gateway.adapters.wechat import (
    ChatPollResult,
    PollFailure,
    PollFailureStage,
    PollResult,
)
from cf_agent_gateway.config import Settings
from cf_agent_gateway.runtime import (
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
)


def invoke_cli(monkeypatch: pytest.MonkeyPatch, result: PollResult) -> int:
    monkeypatch.setattr(wechat_poll_once, "load_settings", lambda path: Settings())
    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", lambda settings: result)
    return wechat_poll_once.main()


def test_cli_prints_only_the_safe_poll_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_body = "complete-sensitive-message-body"
    sensitive_token = "secret-token-that-must-not-leak"
    result = PollResult(
        source_account_id="wxid_gateway",
        logged_in=True,
        chats_seen=1,
        chats_succeeded=1,
        messages_seen=3,
        messages_processed=1,
        messages_skipped_by_checkpoint=2,
        chat_results=[
            ChatPollResult(
                conversation_id="wxid_sensitive",
                conversation_name=sensitive_body,
                succeeded=True,
            )
        ],
    )

    exit_code = invoke_cli(monkeypatch, result)

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert summary == {
        "bootstrapped_chats": 0,
        "chats_failed": 0,
        "chats_seen": 1,
        "chats_succeeded": 1,
        "failure_codes": [],
        "logged_in": True,
        "messages_processed": 1,
        "messages_seen": 3,
        "messages_skipped_by_checkpoint": 2,
        "source_account_id": "wxid_gateway",
    }
    serialized = captured.out + captured.err
    assert sensitive_body not in serialized
    assert sensitive_token not in serialized
    assert "wxid_sensitive" not in serialized


@pytest.mark.parametrize(
    ("result", "expected_exit_code"),
    [
        (PollResult(logged_in=True), 0),
        (
            PollResult(
                logged_in=True,
                chats_seen=1,
                chats_failed=1,
                failures=[PollFailure(stage=PollFailureStage.SINK, code="wechat_sink_error")],
            ),
            1,
        ),
        (PollResult(logged_in=False), 2),
    ],
)
def test_cli_exit_codes_are_stable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: PollResult,
    expected_exit_code: int,
) -> None:
    assert invoke_cli(monkeypatch, result) == expected_exit_code
    capsys.readouterr()


def test_cli_runtime_failure_does_not_print_exception_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_token = "runtime-secret-token"
    sensitive_body = "runtime-sensitive-body"
    monkeypatch.setattr(wechat_poll_once, "load_settings", lambda path: Settings())

    def fail(settings: Settings) -> PollResult:
        del settings
        raise RuntimeError(f"{sensitive_token}:{sensitive_body}")

    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", fail)

    assert wechat_poll_once.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "wechat_poll_once_failed"}
    assert sensitive_token not in captured.err
    assert sensitive_body not in captured.err


@pytest.mark.parametrize(
    ("error", "expected_exit_code"),
    [
        (WechatRuntimeDisabledError(), 2),
        (WechatTokenEnvironmentError("TEST_WECHAT_TOKEN"), 1),
    ],
)
def test_cli_runtime_boundary_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_exit_code: int,
) -> None:
    monkeypatch.setattr(wechat_poll_once, "load_settings", lambda path: Settings())

    def fail(settings: Settings) -> PollResult:
        del settings
        raise error

    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", fail)

    assert wechat_poll_once.main() == expected_exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error_code"] == error.code  # type: ignore[attr-defined]


def test_python_module_entrypoint_exits_two_when_runtime_is_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wechat:\n  enabled: false\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["CF_GATEWAY_CONFIG"] = str(config_path)

    completed = subprocess.run(
        [sys.executable, "-m", "cf_agent_gateway.wechat_poll_once"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert json.loads(completed.stderr) == {"error_code": "wechat_runtime_disabled"}
