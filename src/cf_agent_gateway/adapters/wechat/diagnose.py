"""Read current WeChat authentication only; no messages, database, or model calls."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from typing import Any

from cf_agent_gateway.adapters.wechat.client import AgentWechatClient
from cf_agent_gateway.adapters.wechat.errors import (
    WechatAPIError,
    WechatResponseError,
    WechatTransportError,
)
from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.runtime.errors import WechatTokenContractError
from cf_agent_gateway.runtime.wechat_token import resolve_wechat_token


def inspect_authentication(
    settings: Settings,
    *,
    environment_reader: Callable[[str], str | None] = os.getenv,
    client_factory: Callable[..., AgentWechatClient] = AgentWechatClient,
) -> dict[str, Any]:
    """Use the same auth client and normalized account source as the Poll worker.

    The account is deliberately returned only by this explicit operator command;
    callers must not put its output in routine worker logs or public artifacts.
    """
    result: dict[str, Any] = {"status": "service_unavailable", "authenticated": False}
    if not settings.wechat.enabled:
        return {**result, "code": "wechat_disabled"}
    try:
        token = resolve_wechat_token(
            settings.wechat.token_env, environment_reader=environment_reader
        )
    except WechatTokenContractError:
        return {**result, "status": "token_error", "code": "wechat_token_invalid"}
    try:
        with client_factory(settings.wechat.base_url, token) as client:
            auth = client.get_auth_status()
    except WechatAPIError as error:
        status = "token_error" if error.status_code in {401, 403} else "service_unavailable"
        return {**result, "status": status, "code": error.code}
    except WechatResponseError:
        return {**result, "status": "invalid_account", "code": "wechat_auth_response_invalid"}
    except WechatTransportError as error:
        return {**result, "code": error.code}
    except ValueError:
        return {**result, "status": "token_error", "code": "wechat_client_configuration_invalid"}
    if auth.status != "logged_in":
        return {"status": "not_logged_in", "authenticated": False}
    if auth.source_account_id is None:
        return {
            **result,
            "status": "invalid_account",
            "code": "wechat_authenticated_account_missing",
        }
    return {"status": "authenticated", "authenticated": True, "account_id": auth.source_account_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.getenv("CF_GATEWAY_CONFIG", "config/config.yaml"))
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except Exception:
        result = {
            "status": "service_unavailable",
            "authenticated": False,
            "code": "runtime_configuration_invalid",
        }
    else:
        result = inspect_authentication(settings)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"authenticated", "not_logged_in"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
