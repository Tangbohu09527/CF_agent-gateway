from __future__ import annotations

import json
import logging
from configparser import ConfigParser
from pathlib import Path

import yaml

from cf_agent_gateway.logging import JsonFormatter

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.prod.yml"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
ALEMBIC_CONFIG_PATH = ROOT / "alembic.ini"

DEFAULT_LOG_MAX_SIZE_BYTES = 64 * 1024 * 1024
DEFAULT_LOG_MAX_FILES = 10
RETENTION_DAYS = 7
POLLING_INTERVAL_SECONDS = 3
PRODUCTION_CHAT_COUNT = 21
PRODUCTION_VISIBLE_MESSAGES = 95
STABLE_NONEMPTY_CHAT_COUNTS = (9, 14, 20, 50, 1, 1)
STEADY_STATE_REPEATED_INFO_RECORDS_PER_CYCLE = 0
CONTINUITY_EMPTY_CHAT_COUNT = 5
CONTINUITY_STEADY_REPEATED_WARNING_RECORDS_PER_CYCLE = 0
CONTINUITY_STEADY_REPEATED_INFO_RECORDS_PER_CYCLE = 0
CONTINUITY_REMINDERS_PER_HOUR = 0
BUSINESS_ACTIVE_CHATS_PER_CYCLE = 2
HISTORY_SHAPE_CHANGES_PER_HOUR = 1
CONTINUITY_STATE_CHANGES_PER_HOUR = 1
CHECKPOINT_TRANSITIONS_PER_HOUR = 2
WORKER_RESTARTS_PER_DAY = 1
RECORD_SAFETY_MARGIN_BYTES = 128
USABLE_CAPACITY_RATIO = 0.90
RUNTIME_SERVICES = (
    "heartbeat-init",
    "migration",
    "gateway",
    "worker",
    "dispatch-worker",
    "delivery-worker",
)


def _docker_json_file_bytes(
    message: str,
    *,
    level: int,
    fields: dict[str, object] | None = None,
) -> int:
    record = logging.LogRecord(
        name="cf_agent_gateway.runtime.worker",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    record.process = 99_999_999
    if fields is not None:
        record.fields = fields  # type: ignore[attr-defined]
    application_line = JsonFormatter(service="cf-agent-gateway-worker").format(record)
    docker_line = json.dumps(
        {
            "log": f"{application_line}\n",
            "stream": "stderr",
            "time": "2026-09-01T00:00:00.000000000Z",
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return len(f"{docker_line}\n".encode()) + RECORD_SAFETY_MARGIN_BYTES


def retention_model() -> dict[str, float | int]:
    def chat_summary_bytes(
        *,
        messages_seen: int,
        messages_processed: int,
        messages_new: int,
        messages_duplicate: int,
        messages_skipped_checkpoint: int,
        messages_skipped_self: int,
        messages_failed: int,
        messages_without_server_id: int,
        succeeded: bool = True,
        failure_count: int = 0,
    ) -> int:
        return _docker_json_file_bytes(
            "poll chat completed",
            level=logging.INFO,
            fields={
                "source_account_id_ref": "source_account:sha256:0123456789abcdef",
                "conversation_id_ref": "conversation:sha256:fedcba9876543210",
                "succeeded": succeeded,
                "failure_count": failure_count,
                "messages_seen": messages_seen,
                "messages_processed": messages_processed,
                "messages_new": messages_new,
                "messages_duplicate": messages_duplicate,
                "messages_skipped_checkpoint": messages_skipped_checkpoint,
                "messages_skipped_self": messages_skipped_self,
                "messages_failed": messages_failed,
                "messages_without_server_id": messages_without_server_id,
                "bootstrapped": False,
            },
        )

    active_chat_summary_bytes = chat_summary_bytes(
        messages_seen=999,
        messages_processed=999,
        messages_new=999,
        messages_duplicate=999,
        messages_skipped_checkpoint=999,
        messages_skipped_self=999,
        messages_failed=999,
        messages_without_server_id=999,
    )
    active_cycle_summary_bytes = _docker_json_file_bytes(
        "poll cycle completed",
        level=logging.INFO,
        fields={
            "logged_in": True,
            "chats_seen": 999,
            "chats_succeeded": 999,
            "chats_failed": 0,
            "messages_seen": 999,
            "messages_processed": 999,
            "messages_new": 999,
            "messages_duplicate": 999,
            "messages_skipped_checkpoint": 999,
            "messages_skipped_self": 999,
            "messages_failed": 999,
            "messages_without_server_id": 999,
            "bootstrapped_chats": 0,
            "failure_count": 0,
        },
    )
    stable_history_chat_bytes = sum(
        chat_summary_bytes(
            messages_seen=count,
            messages_processed=0,
            messages_new=0,
            messages_duplicate=0,
            messages_skipped_checkpoint=count,
            messages_skipped_self=0,
            messages_failed=0,
            messages_without_server_id=0,
        )
        for count in STABLE_NONEMPTY_CHAT_COUNTS
    )
    stable_history_cycle_bytes = _docker_json_file_bytes(
        "poll cycle completed",
        level=logging.INFO,
        fields={
            "logged_in": True,
            "chats_seen": PRODUCTION_CHAT_COUNT,
            "chats_succeeded": PRODUCTION_CHAT_COUNT,
            "chats_failed": 0,
            "messages_seen": PRODUCTION_VISIBLE_MESSAGES,
            "messages_processed": 0,
            "messages_new": 0,
            "messages_duplicate": 0,
            "messages_skipped_checkpoint": PRODUCTION_VISIBLE_MESSAGES,
            "messages_skipped_self": 0,
            "messages_failed": 0,
            "messages_without_server_id": 0,
            "bootstrapped_chats": 0,
            "failure_count": 0,
        },
    )
    continuity_warning_bytes = _docker_json_file_bytes(
        "checkpoint continuity unverified",
        level=logging.WARNING,
        fields={
            "source_account_id_ref": "source_account:sha256:0123456789abcdef",
            "conversation_id_ref": "conversation:sha256:fedcba9876543210",
            "old_checkpoint": 9_223_372_036_854_775_807,
            "remote_first_local_id": 0,
            "remote_latest_local_id": 0,
            "old_generation": 999_999,
            "new_generation": 999_999,
            "anchor_match": None,
            "recovery_action": "stop_chat_visible_window_empty",
            "cas_result": None,
        },
    )
    continuity_chat_summary_bytes = chat_summary_bytes(
        messages_seen=0,
        messages_processed=0,
        messages_new=0,
        messages_duplicate=0,
        messages_skipped_checkpoint=0,
        messages_skipped_self=0,
        messages_failed=0,
        messages_without_server_id=0,
        succeeded=False,
        failure_count=1,
    )
    continuity_cycle_summary_bytes = _docker_json_file_bytes(
        "poll cycle completed",
        level=logging.INFO,
        fields={
            "logged_in": True,
            "chats_seen": CONTINUITY_EMPTY_CHAT_COUNT,
            "chats_succeeded": 0,
            "chats_failed": CONTINUITY_EMPTY_CHAT_COUNT,
            "messages_seen": 0,
            "messages_processed": 0,
            "messages_new": 0,
            "messages_duplicate": 0,
            "messages_skipped_checkpoint": 0,
            "messages_skipped_self": 0,
            "messages_failed": 0,
            "messages_without_server_id": 0,
            "bootstrapped_chats": 0,
            "failure_count": CONTINUITY_EMPTY_CHAT_COUNT,
        },
    )
    checkpoint_transition_bytes = _docker_json_file_bytes(
        "checkpoint regression live suffix started",
        level=logging.WARNING,
        fields={
            "source_account_id_ref": "source_account:sha256:0123456789abcdef",
            "conversation_id_ref": "conversation:sha256:fedcba9876543210",
            "old_checkpoint": 9_223_372_036_854_775_807,
            "remote_first_local_id": 9_223_372_036_854_775_807,
            "remote_latest_local_id": 9_223_372_036_854_775_807,
            "old_generation": 999_999,
            "new_generation": 1_000_000,
            "anchor_match": True,
            "recovery_action": "process_live_suffix_after_empty_window",
            "cas_result": True,
            "messages_skipped": 999,
        },
    )
    worker_started_bytes = _docker_json_file_bytes(
        "worker started",
        level=logging.INFO,
        fields={"polling_interval_seconds": POLLING_INTERVAL_SECONDS},
    )
    worker_stopped_bytes = _docker_json_file_bytes("worker stopped", level=logging.INFO)

    cycles = RETENTION_DAYS * 24 * 60 * 60 // POLLING_INTERVAL_SECONDS
    steady_state_repeated_info_bytes = cycles * STEADY_STATE_REPEATED_INFO_RECORDS_PER_CYCLE
    business_activity_bytes = cycles * (
        BUSINESS_ACTIVE_CHATS_PER_CYCLE * active_chat_summary_bytes + active_cycle_summary_bytes
    )
    history_change_burst_bytes = stable_history_chat_bytes + stable_history_cycle_bytes
    continuity_change_burst_bytes = (
        CONTINUITY_EMPTY_CHAT_COUNT * (continuity_warning_bytes + continuity_chat_summary_bytes)
        + continuity_cycle_summary_bytes
    )
    hourly_history_change_bytes = (
        RETENTION_DAYS * 24 * HISTORY_SHAPE_CHANGES_PER_HOUR * history_change_burst_bytes
    )
    hourly_continuity_change_bytes = (
        RETENTION_DAYS * 24 * CONTINUITY_STATE_CHANGES_PER_HOUR * continuity_change_burst_bytes
    )
    periodic_continuity_reminder_bytes = (
        RETENTION_DAYS * 24 * CONTINUITY_REMINDERS_PER_HOUR * continuity_change_burst_bytes
    )
    hourly_checkpoint_bytes = (
        RETENTION_DAYS * 24 * CHECKPOINT_TRANSITIONS_PER_HOUR * checkpoint_transition_bytes
    )
    daily_restart_bytes = (
        RETENTION_DAYS
        * WORKER_RESTARTS_PER_DAY
        * (
            worker_stopped_bytes
            + worker_started_bytes
            + history_change_burst_bytes
            + continuity_change_burst_bytes
        )
    )
    initial_evidence_bytes = checkpoint_transition_bytes + worker_stopped_bytes
    modeled_bytes = (
        steady_state_repeated_info_bytes
        + business_activity_bytes
        + hourly_history_change_bytes
        + hourly_continuity_change_bytes
        + periodic_continuity_reminder_bytes
        + hourly_checkpoint_bytes
        + daily_restart_bytes
        + initial_evidence_bytes
    )
    configured_capacity_bytes = DEFAULT_LOG_MAX_SIZE_BYTES * DEFAULT_LOG_MAX_FILES
    maximum_compose_disk_bytes = configured_capacity_bytes * len(RUNTIME_SERVICES)
    usable_capacity_bytes = int(configured_capacity_bytes * USABLE_CAPACITY_RATIO)
    modeled_bytes_per_day = modeled_bytes / RETENTION_DAYS
    estimated_retention_days = usable_capacity_bytes / modeled_bytes_per_day
    return {
        "cycles": cycles,
        "steady_state_repeated_info_records_per_cycle": (
            STEADY_STATE_REPEATED_INFO_RECORDS_PER_CYCLE
        ),
        "continuity_steady_repeated_warning_records_per_cycle": (
            CONTINUITY_STEADY_REPEATED_WARNING_RECORDS_PER_CYCLE
        ),
        "continuity_steady_repeated_info_records_per_cycle": (
            CONTINUITY_STEADY_REPEATED_INFO_RECORDS_PER_CYCLE
        ),
        "continuity_reminders_per_hour": CONTINUITY_REMINDERS_PER_HOUR,
        "business_info_records_per_cycle": BUSINESS_ACTIVE_CHATS_PER_CYCLE + 1,
        "history_first_change_info_records": len(STABLE_NONEMPTY_CHAT_COUNTS) + 1,
        "continuity_first_change_warning_records": CONTINUITY_EMPTY_CHAT_COUNT,
        "continuity_first_change_info_records": CONTINUITY_EMPTY_CHAT_COUNT + 1,
        "modeled_bytes": modeled_bytes,
        "modeled_bytes_per_day": modeled_bytes_per_day,
        "configured_capacity_bytes": configured_capacity_bytes,
        "maximum_compose_disk_bytes": maximum_compose_disk_bytes,
        "usable_capacity_bytes": usable_capacity_bytes,
        "estimated_retention_days": estimated_retention_days,
        "initial_evidence_bytes": initial_evidence_bytes,
    }


def test_production_compose_uses_overridable_json_file_retention_defaults() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]

    for service_name in RUNTIME_SERVICES:
        assert services[service_name]["logging"] == {
            "driver": "json-file",
            "options": {
                "max-size": "${CF_GATEWAY_LOG_MAX_SIZE:-64m}",
                "max-file": "${CF_GATEWAY_LOG_MAX_FILES:-10}",
            },
        }

    environment = dict(
        line.split("=", 1)
        for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert environment["CF_GATEWAY_LOG_MAX_SIZE"] == "64m"
    assert environment["CF_GATEWAY_LOG_MAX_FILES"] == "10"


def test_alembic_file_config_keeps_routine_context_logs_below_warning() -> None:
    config = ConfigParser()
    config.read(ALEMBIC_CONFIG_PATH, encoding="utf-8")

    assert config["logger_alembic"]["level"] == "WARN"


def test_retention_model_keeps_worker_stop_and_checkpoint_transition_for_seven_days() -> None:
    model = retention_model()

    assert model["initial_evidence_bytes"] > 0
    assert model["cycles"] == 201_600
    assert model["steady_state_repeated_info_records_per_cycle"] == 0
    assert model["continuity_steady_repeated_warning_records_per_cycle"] == 0
    assert model["continuity_steady_repeated_info_records_per_cycle"] == 0
    assert model["continuity_reminders_per_hour"] == 0
    assert model["business_info_records_per_cycle"] == 3
    assert model["history_first_change_info_records"] == 7
    assert model["continuity_first_change_warning_records"] == 5
    assert model["continuity_first_change_info_records"] == 6
    assert model["maximum_compose_disk_bytes"] == 3_840 * 1024 * 1024
    assert model["modeled_bytes"] <= model["usable_capacity_bytes"]
    assert model["estimated_retention_days"] >= RETENTION_DAYS
