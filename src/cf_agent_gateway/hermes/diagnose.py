"""Bounded LAN checks; model execution always requires an explicit opt-in.

This module reads the same config/env as a Gateway process. It deliberately does
not initialise the database, inspect business sessions, or print remote bodies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import yaml

from cf_agent_gateway.config import HermesSettings, load_settings
from cf_agent_gateway.hermes.client import HermesClient
from cf_agent_gateway.hermes.errors import HermesAPIError, HermesError


def diagnose(
    settings: HermesSettings,
    *,
    environ: Mapping[str, str],
    check_auth: bool = False,
    allow_model_call: bool = False,
    check_replay: bool = False,
    profile_reference: str | None = None,
    profile_revision: int | None = None,
    timeout: float = 5.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Check only the requested layers, stopping at the first failed layer.

    ``transport`` is a unit-test seam for HTTP, not a substitute for the socket
    check or evidence that a real Hermes installation implements this contract.
    """
    result: dict[str, Any] = {
        "network": "not_checked",
        "authentication": "not_checked",
        "protocol": "not_checked",
        "application": "not_checked",
        "replay": "not_checked",
        "profile_semantics": "requires_upstream_evidence",
        "durable_idempotency": "requires_upstream_evidence",
        "production_acceptance": False,
        "ok": False,
    }
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        return _failed(result, "configuration", "invalid_timeout")
    if not settings.enabled or not settings.base_url:
        return _failed(result, "configuration", "hermes_disabled_or_unconfigured")
    if check_replay and not allow_model_call:
        return _failed(result, "configuration", "model_opt_in_required")
    if allow_model_call and (
        not isinstance(profile_reference, str)
        or not profile_reference.strip()
        or len(profile_reference) > 255
        or isinstance(profile_revision, bool)
        or not isinstance(profile_revision, int)
        or profile_revision <= 0
    ):
        return _failed(result, "configuration", "v2_profile_required")

    parsed = urlsplit(settings.base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            pass
    except TimeoutError:
        return _failed(result, "network", "connect_timeout")
    except OSError:
        return _failed(result, "network", "connect_failed")
    result["network"] = "tcp_connected"
    if not (check_auth or allow_model_call):
        result["ok"] = True
        return result

    api_key = environ.get(settings.api_key_env)
    # Reuse the production client's validation before creating any HTTP request.
    try:
        client = HermesClient(
            settings.base_url, api_key, settings.model, timeout=timeout, transport=transport
        )
    except (HermesError, ValueError):
        return _failed(result, "configuration", "hermes_api_key_or_client_invalid")

    with client:
        auth_error = _check_auth(settings, api_key, timeout=timeout, transport=transport)
        if auth_error is not None:
            return _failed(result, "authentication", auth_error)
        result["authentication"] = "models_authenticated_and_wrong_key_rejected"
        if not allow_model_call:
            result["ok"] = True
            return result

        probe_id = "gateway-lan-probe-" + uuid4().hex
        result["probe_id"] = probe_id
        marker = "ACK-" + probe_id
        invocation = {
            "hermes_thread_id": probe_id,
            "profile_reference": profile_reference,
            "profile_revision": profile_revision,
            "thread_id": probe_id,
            "session_metadata": {
                "message_id": 0,
                "source": "diagnostic",
                "channel": "diagnostic",
                "source_account_id": probe_id,
                "conversation_id": probe_id,
                "conversation_type": "private",
                "enterprise_identity_id": probe_id,
                "sender_identity_id": probe_id,
                "sender_id": probe_id,
                "thread_id": probe_id,
                "thread_policy": "private_sender",
                "context_available": False,
                "available_tools": [],
            },
            "idempotency_key": probe_id,
        }
        content = f"Connectivity acceptance probe. Do not use tools. Reply exactly: {marker}"
        # A read-only models route does not prove the business route enforces auth.
        # This POST is inside the explicit billing opt-in: broken auth can execute it.
        try:
            with HermesClient(
                settings.base_url,
                "rejected-probe-" + uuid4().hex,
                settings.model,
                timeout=timeout,
                transport=transport,
            ) as invalid_client:
                invalid_client.chat(content, **invocation)
        except HermesAPIError as exc:
            if exc.status_code not in {401, 403}:
                return _failed(result, "authentication", "wrong_post_key_not_rejected")
        except HermesError as exc:
            return _failed(result, "authentication", exc.code)
        else:
            return _failed(result, "authentication", "wrong_post_key_not_rejected")
        result["authentication"] = "models_authenticated_and_wrong_post_key_rejected"
        try:
            response = client.chat(content, **invocation)
        except HermesAPIError as exc:
            return _failed(result, "protocol", f"http_{exc.status_code}_{exc.category}")
        except HermesError as exc:
            return _failed(result, "protocol", exc.code)
        except ValueError:
            return _failed(result, "protocol", "invalid_invocation")
        result["protocol"] = "gateway_response_and_session_header_valid"
        if response.assistant_content.strip() != marker:
            return _failed(result, "application", "probe_marker_mismatch")
        result["application"] = "unique_marker_returned"
        if check_replay:
            try:
                replay = client.chat(content, **invocation)
            except HermesError as exc:
                return _failed(result, "replay", exc.code)
            if replay != response:
                return _failed(result, "replay", "replay_response_changed")
            # Equal responses cannot prove a model/tool ran only once.
            result["replay"] = "response_consistent_execution_count_unverified"
    result["ok"] = True
    return result


def _check_auth(
    settings: HermesSettings,
    api_key: str,
    *,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> str | None:
    """Use only the explicit candidate contract's read-only models endpoint."""
    try:
        with httpx.Client(
            base_url=settings.base_url.rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            invalid = client.get(
                "v1/models",
                headers={"Authorization": "Bearer rejected-probe-" + uuid4().hex},
            )
            if invalid.status_code not in {401, 403}:
                return "wrong_key_not_rejected"
            valid = client.get("v1/models", headers={"Authorization": f"Bearer {api_key.strip()}"})
            if valid.status_code in {401, 403}:
                return "configured_key_rejected"
            if valid.status_code != 200:
                return "models_contract_unavailable"
            payload = valid.json()
            if (
                not isinstance(payload, dict)
                or payload.get("object") != "list"
                or not isinstance(payload.get("data"), list)
                or not any(
                    isinstance(model, dict)
                    and isinstance(model.get("id"), str)
                    and model["id"].strip()
                    for model in payload["data"]
                )
            ):
                return "models_response_invalid"
    except httpx.TimeoutException:
        return "http_timeout"
    except httpx.RequestError:
        return "http_transport_failed"
    except ValueError:
        return "models_response_invalid"
    return None


def _failed(result: dict[str, Any], stage: str, code: str) -> dict[str, Any]:
    result.update(failed_stage=stage, error=code)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.getenv("CF_GATEWAY_CONFIG", "config/config.yaml"))
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--check-auth", action="store_true", help="GET /v1/models; no model call")
    parser.add_argument(
        "--allow-model-call", action="store_true", help="Opt in to a potentially billable POST"
    )
    parser.add_argument(
        "--check-replay", action="store_true", help="Repeat that POST; may incur another charge"
    )
    parser.add_argument("--profile-reference")
    parser.add_argument("--profile-revision", type=int)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        result = diagnose(
            settings.hermes,
            environ=os.environ,
            check_auth=args.check_auth,
            allow_model_call=args.allow_model_call,
            check_replay=args.check_replay,
            profile_reference=args.profile_reference,
            profile_revision=args.profile_revision,
            timeout=args.timeout,
        )
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        # Parser errors may include lines containing credentials or private URLs.
        result = {"ok": False, "failed_stage": "configuration", "error": "config_load_failed"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
