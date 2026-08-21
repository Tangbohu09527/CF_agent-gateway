from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from typing import Any, TextIO

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.runtime import (
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
    run_wechat_poll_once,
)
from cf_agent_gateway.runtime.status import DatabaseWorkerStatusReporter, WorkerLeaseError

DEFAULT_CONFIG_PATH = "config/config.yaml"


def poll_result_summary(result: PollResult) -> dict[str, Any]:
    return {
        "logged_in": result.logged_in,
        "source_account_id": result.source_account_id,
        "chats_seen": result.chats_seen,
        "chats_succeeded": result.chats_succeeded,
        "chats_failed": result.chats_failed,
        "messages_seen": result.messages_seen,
        "messages_processed": result.messages_processed,
        "messages_skipped_by_checkpoint": result.messages_skipped_by_checkpoint,
        "bootstrapped_chats": result.bootstrapped_chats,
        "failure_codes": [failure.code for failure in result.failures],
    }


def poll_result_exit_code(result: PollResult) -> int:
    if result.failures or result.chats_failed:
        return 1
    if not result.logged_in:
        return 2
    return 0


def _poll_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if (
        isinstance(code, str)
        and 0 < len(code) <= 128
        and all(character.isalnum() or character in {"_", "-", "."} for character in code)
    ):
        return code
    return "wechat_poll_once_failed"


def run_exclusive_poll_once(settings: Settings) -> PollResult:
    """Run a diagnostic cycle while holding the resident Worker's singleton lease."""

    if not settings.wechat.enabled:
        raise WechatRuntimeDisabledError()

    reporter = DatabaseWorkerStatusReporter(
        settings.database.url,
        hermes_enabled=settings.hermes.enabled,
        heartbeat_interval_seconds=settings.runtime.heartbeat_interval_seconds,
        heartbeat_stale_after_seconds=settings.runtime.heartbeat_stale_after_seconds,
    )
    reporter.start()
    try:
        reporter.cycle_started()
        result = run_wechat_poll_once(settings, lease_guard=reporter.ensure_active)
        reporter.cycle_succeeded(result)
    except Exception as error:
        with suppress(Exception):
            reporter.cycle_failed(_poll_error_code(error))
        with suppress(Exception):
            reporter.stop()
        raise
    reporter.stop()
    return result


def main() -> int:
    config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
    try:
        settings = load_settings(config_path)
        result = run_exclusive_poll_once(settings)
    except WechatRuntimeDisabledError as error:
        _write_json({"error_code": error.code}, file=sys.stderr)
        return 2
    except WechatTokenEnvironmentError as error:
        _write_json(
            {
                "error_code": error.code,
                "environment_variable": error.environment_variable,
            },
            file=sys.stderr,
        )
        return 1
    except WorkerLeaseError as error:
        _write_json({"error_code": error.code}, file=sys.stderr)
        return 1
    except Exception:
        _write_json({"error_code": "wechat_poll_once_failed"}, file=sys.stderr)
        return 1

    _write_json(poll_result_summary(result), file=sys.stdout)
    return poll_result_exit_code(result)


def _write_json(payload: dict[str, Any], *, file: TextIO) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=file)


if __name__ == "__main__":
    raise SystemExit(main())
