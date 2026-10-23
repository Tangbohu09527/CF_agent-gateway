"""Bounded, content-free message-chain observations in the candidate database."""

import os
from copy import deepcopy

from cf_agent_gateway.management_console.live_models import LiveObservation
from cf_agent_gateway.management_console.service import iso, now

REASONS = frozenset(
    {
        "source_identity_unverified",
        "source_account_mismatch",
        "source_account_changed_during_collect",
        "source_chat_identity_invalid",
        "source_conversation_mismatch",
        "source_messages_connection_failed",
        "source_messages_read_failed",
        "source_messages_contract_invalid",
        "source_messages_all_failed",
        "source_connection_failed",
        "source_http_error",
        "source_auth_denied",
        "source_read_failed",
        "source_contract_invalid",
        "source_pagination_limit_partial",
        "source_pagination_not_advancing",
        "invalid_message_identity_or_schema",
        "candidate_commit_failed",
        "archive_source_check_failed",
        "collector_internal_error",
        "wechat_not_verified_online",
        "archive_owner_not_verified",
        "production_collector_running",
        "bot_offline",
        "source_identity_unavailable",
        "archive_read_failed",
        "archive_not_available",
        "archive_backend_not_supported",
    }
)


def safe_reason(value):
    return str(value) if str(value) in REASONS else "archive_source_check_failed"


def record(session, source, stage, state, reason=None, **counts):
    """Never accepts message bodies, arbitrary errors, URLs, identities or secrets."""
    assert stage in {"login", "ownership", "upstream", "normalization", "persistence"}
    assert state in {"normal", "partial", "blocked", "error", "disabled", "unknown"}
    reason = safe_reason(reason) if reason else None
    row = session.get(LiveObservation, "message-chain:" + source)
    if row is None:
        row = LiveObservation(key="message-chain:" + source, value={})
        session.add(row)
    value = deepcopy(row.value or {})
    at = iso(now())
    stages = value.setdefault("stages", {})
    previous = stages.get(stage, {})
    stages[stage] = {
        "state": state,
        "reason": reason,
        "observed_at": at,
        "last_success_at": at if state == "normal" else previous.get("last_success_at"),
        "evidence": "candidate_archive_only" if stage != "login" else "native_auth_process",
        **{
            k: int(v)
            for k, v in counts.items()
            if k in {"conversations", "failed_conversations", "invalid_rows", "messages"}
        },
    }
    incidents = value.setdefault("incidents", [])
    for incident in incidents:
        if (
            incident["stage"] == stage
            and not incident.get("recovered_at")
            and (state in {"normal", "disabled"} or incident["reason"] != reason)
        ):
            incident["recovered_at"] = at
    if state in {"partial", "blocked", "error"}:
        incident = next(
            (
                x
                for x in reversed(incidents)
                if x["stage"] == stage and x["reason"] == reason and not x.get("recovered_at")
            ),
            None,
        )
        if incident:
            incident["last_at"], incident["count"] = at, incident["count"] + 1
        else:
            incidents.append(
                {
                    "stage": stage,
                    "reason": reason,
                    "first_at": at,
                    "last_at": at,
                    "count": 1,
                    "recovered_at": None,
                    "revision": os.environ.get("CANDIDATE_REVISION", "development"),
                }
            )
    value["incidents"] = incidents[-12:]
    row.value, row.observed_at = value, now()
