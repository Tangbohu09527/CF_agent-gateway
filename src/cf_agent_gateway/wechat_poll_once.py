from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.config import load_settings
from cf_agent_gateway.runtime import (
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
    run_wechat_poll_once,
)

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


def main() -> int:
    config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
    try:
        settings = load_settings(config_path)
        result = run_wechat_poll_once(settings)
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
    except Exception:
        _write_json({"error_code": "wechat_poll_once_failed"}, file=sys.stderr)
        return 1

    _write_json(poll_result_summary(result), file=sys.stdout)
    return poll_result_exit_code(result)


def _write_json(payload: dict[str, Any], *, file: TextIO) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=file)


if __name__ == "__main__":
    raise SystemExit(main())
