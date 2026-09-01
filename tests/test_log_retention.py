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
ACTIVE_CHATS_PER_CYCLE = 2
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
    chat_summary_bytes = _docker_json_file_bytes(
        "poll chat completed",
        level=logging.INFO,
        fields={
            "source_account_id_ref": "source_account:sha256:0123456789abcdef",
            "conversation_id_ref": "conversation:sha256:fedcba9876543210",
            "succeeded": True,
            "failure_count": 0,
            "messages_seen": 999,
            "messages_processed": 999,
            "messages_new": 999,
            "messages_duplicate": 999,
            "messages_skipped_checkpoint": 999,
            "messages_skipped_self": 999,
            "messages_failed": 0,
            "messages_without_server_id": 999,
            "bootstrapped": False,
        },
    )
    cycle_summary_bytes = _docker_json_file_bytes(
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
            "messages_failed": 0,
            "messages_without_server_id": 999,
            "bootstrapped_chats": 0,
            "failure_count": 0,
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
    routine_bytes = cycles * (ACTIVE_CHATS_PER_CYCLE * chat_summary_bytes + cycle_summary_bytes)
    hourly_checkpoint_bytes = RETENTION_DAYS * 24 * 2 * checkpoint_transition_bytes
    daily_restart_bytes = RETENTION_DAYS * (worker_stopped_bytes + worker_started_bytes)
    initial_evidence_bytes = checkpoint_transition_bytes + worker_stopped_bytes
    modeled_bytes = (
        routine_bytes + hourly_checkpoint_bytes + daily_restart_bytes + initial_evidence_bytes
    )
    configured_capacity_bytes = DEFAULT_LOG_MAX_SIZE_BYTES * DEFAULT_LOG_MAX_FILES
    usable_capacity_bytes = int(configured_capacity_bytes * USABLE_CAPACITY_RATIO)
    modeled_bytes_per_day = modeled_bytes / RETENTION_DAYS
    estimated_retention_days = usable_capacity_bytes / modeled_bytes_per_day
    return {
        "cycles": cycles,
        "modeled_bytes": modeled_bytes,
        "modeled_bytes_per_day": modeled_bytes_per_day,
        "configured_capacity_bytes": configured_capacity_bytes,
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
    assert model["modeled_bytes"] <= model["usable_capacity_bytes"]
    assert model["estimated_retention_days"] >= RETENTION_DAYS
