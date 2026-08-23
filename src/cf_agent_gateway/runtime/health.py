from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from sqlalchemy import Engine, and_, exists, func, or_, select, text
from sqlalchemy.orm import Session, aliased

from cf_agent_gateway.adapters.wechat.polling_models import WechatSyncCheckpoint
from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import check_database_migrations
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryStatus
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.runtime.heartbeat import HeartbeatError, check_heartbeat
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Probe = Callable[[], bool]

DEFAULT_DATABASE_PROBE_INTERVAL_SECONDS = 5.0
DEFAULT_DATABASE_PROBE_MAX_AGE_SECONDS = 15.0
DEFAULT_DATABASE_PROBE_STOP_TIMEOUT_SECONDS = 1.0
DEFAULT_DELIVERY_STALE_SECONDS = 300.0
_MAX_OBSERVATION_FUTURE_SKEW_SECONDS = 5.0

WECHAT_HEARTBEAT_PATH_ENV = "CF_GATEWAY_WECHAT_HEARTBEAT_PATH"
DISPATCH_HEARTBEAT_PATH_ENV = "CF_GATEWAY_DISPATCH_HEARTBEAT_PATH"
DELIVERY_HEARTBEAT_PATH_ENV = "CF_GATEWAY_DELIVERY_HEARTBEAT_PATH"
RUNTIME_HEALTH_HEARTBEAT_MAX_AGE_ENV = "CF_GATEWAY_RUNTIME_HEARTBEAT_MAX_AGE_SECONDS"


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatPaths:
    wechat: str | None = None
    dispatch: str | None = None
    delivery: str | None = None

    @classmethod
    def from_environment(cls) -> WorkerHeartbeatPaths:
        return cls(
            wechat=_optional_environment_path(WECHAT_HEARTBEAT_PATH_ENV),
            dispatch=_optional_environment_path(DISPATCH_HEARTBEAT_PATH_ENV),
            delivery=_optional_environment_path(DELIVERY_HEARTBEAT_PATH_ENV),
        )


class RuntimeHealthService:
    """Build a redacted business-runtime snapshot from durable state and heartbeats."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        heartbeat_paths: WorkerHeartbeatPaths | None = None,
        heartbeat_max_age_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._heartbeat_paths = heartbeat_paths or WorkerHeartbeatPaths.from_environment()
        configured_max_age = (
            heartbeat_max_age_seconds
            if heartbeat_max_age_seconds is not None
            else os.getenv(RUNTIME_HEALTH_HEARTBEAT_MAX_AGE_ENV, "30")
        )
        self._heartbeat_max_age_seconds = _positive_seconds(
            configured_max_age,
            RUNTIME_HEALTH_HEARTBEAT_MAX_AGE_ENV,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def snapshot(self) -> dict[str, object]:
        now = _aware_utc(self._clock())
        components: dict[str, dict[str, object]] = {
            "database": {"status": "ok"},
            "migration_schema": {"status": "ok"},
        }
        database_ok = True
        try:
            if not _probe_database(self._engine):
                raise RuntimeError("database probe failed")
        except Exception:
            database_ok = False
            components["database"] = {"status": "unavailable"}

        schema_ok = False
        if database_ok:
            try:
                check_database_migrations(self._engine)
                schema_ok = True
            except Exception:
                components["migration_schema"] = {"status": "mismatch"}
        else:
            components["migration_schema"] = {"status": "unknown"}

        worker_components = {
            "wechat_worker": self._worker_heartbeat(
                self._heartbeat_paths.wechat,
                enabled=self._settings.wechat.enabled,
            ),
            "dispatch_worker": self._worker_heartbeat(
                self._heartbeat_paths.dispatch,
                enabled=self._settings.worker.enabled and self._settings.hermes.enabled,
            ),
            "delivery_worker": self._worker_heartbeat(
                self._heartbeat_paths.delivery,
                enabled=self._settings.wechat.enabled,
            ),
        }
        components.update(worker_components)
        components["wechat_auth"] = self._wechat_auth(worker_components["wechat_worker"])
        components["hermes"] = self._hermes(
            worker_components["dispatch_worker"],
            now=now,
        )

        dispatch = _empty_dispatch_metrics()
        delivery = _empty_delivery_metrics()
        checkpoint = {"status": "unknown", "unverified_checkpoint_count": None}
        if schema_ok:
            try:
                with Session(self._engine) as session:
                    dispatch = _dispatch_metrics(session, now=now)
                    delivery = _delivery_metrics(
                        session,
                        now=now,
                        missing_delivery=int(dispatch["missing_delivery"]),
                    )
                    checkpoint = _checkpoint_metrics(session)
            except Exception:
                components["database"] = {"status": "unavailable"}
        components["wechat_checkpoint_continuity"] = checkpoint

        unhealthy = any(
            components[name]["status"] != "ok" for name in ("database", "migration_schema")
        )
        degraded = (
            any(
                component.get("status")
                in {"degraded", "logged_out", "missing", "stale_or_invalid", "unknown"}
                for component in components.values()
            )
            or any(
                dispatch[key]
                for key in (
                    "failed",
                    "uncertain",
                    "dead",
                    "stale_running",
                    "blocked_threads",
                    "reconciliation_poison",
                )
            )
            or any(
                delivery[key]
                for key in ("failed", "uncertain", "stale_delivering", "missing_delivery")
            )
        )
        status = "unhealthy" if unhealthy else "degraded" if degraded else "healthy"
        return {
            "status": status,
            "checked_at": now.isoformat().replace("+00:00", "Z"),
            "components": components,
            "dispatch": dispatch,
            "delivery": delivery,
        }

    def _worker_heartbeat(self, path: str | None, *, enabled: bool) -> dict[str, object]:
        if not enabled:
            return {"status": "disabled"}
        if path is None:
            return {"status": "unknown"}
        try:
            payload = check_heartbeat(
                path,
                max_age_seconds=self._heartbeat_max_age_seconds,
                clock=self._clock,
            )
        except HeartbeatError:
            status = "stale_or_invalid" if Path(path).exists() else "missing"
            return {"status": status}
        result: dict[str, object] = {
            "status": "ok",
            "state": payload["state"],
            "updated_at": payload["updated_at"],
        }
        details = payload.get("details")
        if isinstance(details, dict):
            result["details"] = {
                key: value
                for key, value in details.items()
                if key
                in {
                    "phase",
                    "cycle_sequence",
                    "last_cycle_succeeded",
                    "wechat_auth",
                    "last_operation_succeeded",
                    "last_operation_at",
                }
            }
            if details.get("last_cycle_succeeded") is False:
                result["status"] = "degraded"
        return result

    def _wechat_auth(self, worker: dict[str, object]) -> dict[str, object]:
        if not self._settings.wechat.enabled:
            return {"status": "disabled"}
        details = worker.get("details")
        if isinstance(details, dict):
            auth = details.get("wechat_auth")
            if auth in {"logged_in", "logged_out"}:
                return {"status": auth}
        return {"status": "unknown"}

    def _hermes(
        self,
        worker: dict[str, object],
        *,
        now: datetime,
    ) -> dict[str, object]:
        if not self._settings.hermes.enabled:
            return {
                "status": "disabled",
                "configuration": "disabled",
                "connectivity": "disabled",
            }
        key = os.getenv(self._settings.hermes.api_key_env)
        configured = bool(self._settings.hermes.base_url and key and key.strip())
        if not configured:
            return {
                "status": "degraded",
                "configuration": "unconfigured",
                "connectivity": "unverified",
            }
        worker_healthy = worker.get("status") == "ok"
        details = worker.get("details")
        operation_succeeded = (
            details.get("last_operation_succeeded") if isinstance(details, dict) else None
        )
        operation_at = details.get("last_operation_at") if isinstance(details, dict) else None
        if not _observation_is_fresh(
            operation_at,
            now=now,
            max_age_seconds=self._heartbeat_max_age_seconds,
        ):
            operation_succeeded = None
        if operation_succeeded is True:
            connectivity = "last_operation_succeeded"
        elif operation_succeeded is False:
            connectivity = "last_operation_failed"
        else:
            connectivity = "no_recent_observation"
        return {
            "status": ("ok" if worker_healthy and operation_succeeded is not False else "degraded"),
            "configuration": "configured",
            "connectivity": connectivity,
        }


class DatabaseReadinessMonitor:
    """Cache database health without blocking HTTP readiness requests."""

    def __init__(
        self,
        engine: Engine,
        *,
        interval_seconds: float = DEFAULT_DATABASE_PROBE_INTERVAL_SECONDS,
        max_age_seconds: float = DEFAULT_DATABASE_PROBE_MAX_AGE_SECONDS,
        stop_timeout_seconds: float = DEFAULT_DATABASE_PROBE_STOP_TIMEOUT_SECONDS,
        clock: Clock = monotonic,
        probe: Probe | None = None,
    ) -> None:
        self._interval_seconds = _positive_seconds(interval_seconds, "interval_seconds")
        self._max_age_seconds = _positive_seconds(max_age_seconds, "max_age_seconds")
        self._stop_timeout_seconds = _positive_seconds(
            stop_timeout_seconds,
            "stop_timeout_seconds",
        )
        if self._max_age_seconds <= self._interval_seconds:
            raise ValueError("max_age_seconds must be greater than interval_seconds")

        self._clock = clock
        self._probe = probe if probe is not None else lambda: _probe_database(engine)
        self._stop_event = Event()
        self._state_lock = Lock()
        self._last_success = clock()
        self._last_probe_succeeded = True
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("database readiness monitor is already started")
        self._thread = Thread(
            target=self._run,
            name="database-readiness",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=self._stop_timeout_seconds)
        if thread.is_alive():
            logger.warning(
                "database readiness monitor did not stop",
                extra={"fields": {"error_code": "database_probe_stuck"}},
            )

    def is_ready(self) -> bool:
        now = self._clock()
        with self._state_lock:
            last_success = self._last_success
            last_probe_succeeded = self._last_probe_succeeded
        return last_probe_succeeded and now - last_success <= self._max_age_seconds

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                succeeded = self._probe()
            except Exception:
                succeeded = False

            now = self._clock()
            with self._state_lock:
                previous_succeeded = self._last_probe_succeeded
                self._last_probe_succeeded = succeeded
                if succeeded:
                    self._last_success = now

            if not succeeded and previous_succeeded:
                logger.warning(
                    "database readiness probe failed",
                    extra={"fields": {"error_code": "database_unavailable"}},
                )
            elif succeeded and not previous_succeeded:
                logger.info("database readiness probe recovered")


def _dispatch_metrics(session: Session, *, now: datetime) -> dict[str, object]:
    counts = {
        status.value: count
        for status, count in session.execute(
            select(HermesDispatchRecord.status, func.count(HermesDispatchRecord.id)).group_by(
                HermesDispatchRecord.status
            )
        )
    }
    uncertain_created_at = session.scalar(
        select(func.min(HermesDispatchRecord.completed_at)).where(
            HermesDispatchRecord.status == HermesDispatchStatus.UNCERTAIN
        )
    )
    oldest_backlog_at = session.scalar(
        select(func.min(HermesDispatchRecord.created_at)).where(
            HermesDispatchRecord.status.in_(
                (
                    HermesDispatchStatus.QUEUED,
                    HermesDispatchStatus.RUNNING,
                    HermesDispatchStatus.FAILED,
                    HermesDispatchStatus.UNCERTAIN,
                )
            )
        )
    )
    uncertain = aliased(HermesDispatchRecord)
    later = aliased(HermesDispatchRecord)
    is_later = or_(
        later.created_at > uncertain.created_at,
        and_(later.created_at == uncertain.created_at, later.id > uncertain.id),
    )
    blocked_threads = session.scalar(
        select(func.count(func.distinct(uncertain.ai_thread_id))).where(
            uncertain.status == HermesDispatchStatus.UNCERTAIN,
            exists(
                select(later.id).where(
                    later.ai_thread_id == uncertain.ai_thread_id,
                    is_later,
                    later.status.in_(
                        (
                            HermesDispatchStatus.QUEUED,
                            HermesDispatchStatus.RUNNING,
                            HermesDispatchStatus.FAILED,
                            HermesDispatchStatus.UNCERTAIN,
                        )
                    ),
                )
            ),
        )
    )
    missing_delivery = session.scalar(
        select(func.count(func.distinct(HermesDispatchRecord.id)))
        .join(
            HermesDispatchResponse,
            HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
        )
        .outerjoin(
            ResponseRecord,
            ResponseRecord.message_id == HermesDispatchRecord.message_id,
        )
        .outerjoin(
            DeliveryOutboxRecord,
            DeliveryOutboxRecord.response_id == ResponseRecord.response_id,
        )
        .where(
            HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
            DeliveryOutboxRecord.id.is_(None),
        )
    )
    normalized_response_exists = exists(
        select(ResponseRecord.response_id)
        .where(ResponseRecord.message_id == HermesDispatchRecord.message_id)
        .correlate(HermesDispatchRecord)
    )
    reconciliation_delivery_exists = exists(
        select(DeliveryOutboxRecord.id)
        .join(
            ResponseRecord,
            ResponseRecord.response_id == DeliveryOutboxRecord.response_id,
        )
        .where(ResponseRecord.message_id == HermesDispatchRecord.message_id)
        .correlate(HermesDispatchRecord)
    )
    reconciliation_filter = (
        HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
        Message.source == "wechat",
        or_(~normalized_response_exists, ~reconciliation_delivery_exists),
    )
    reconciliation_backlog = session.scalar(
        select(func.count(func.distinct(HermesDispatchRecord.id)))
        .join(
            HermesDispatchResponse,
            HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
        )
        .join(Message, Message.id == HermesDispatchRecord.message_id)
        .where(*reconciliation_filter)
    )
    reconciliation_deferred = session.scalar(
        select(func.count(func.distinct(HermesDispatchRecord.id)))
        .join(
            HermesDispatchResponse,
            HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
        )
        .join(Message, Message.id == HermesDispatchRecord.message_id)
        .where(
            *reconciliation_filter,
            HermesDispatchRecord.reconciliation_quarantined_at.is_(None),
            HermesDispatchRecord.reconciliation_next_attempt_at > now,
        )
    )
    reconciliation_poison = session.scalar(
        select(func.count(func.distinct(HermesDispatchRecord.id)))
        .join(
            HermesDispatchResponse,
            HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
        )
        .join(Message, Message.id == HermesDispatchRecord.message_id)
        .where(
            *reconciliation_filter,
            HermesDispatchRecord.reconciliation_quarantined_at.is_not(None),
        )
    )
    oldest_reconciliation_at = session.scalar(
        select(func.min(HermesDispatchRecord.created_at))
        .join(
            HermesDispatchResponse,
            HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
        )
        .join(Message, Message.id == HermesDispatchRecord.message_id)
        .where(*reconciliation_filter)
    )
    stale_running = session.scalar(
        select(func.count(HermesDispatchRecord.id)).where(
            HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
            HermesDispatchRecord.lease_expires_at <= now,
        )
    )
    return {
        "queued": counts.get(HermesDispatchStatus.QUEUED.value, 0),
        "running": counts.get(HermesDispatchStatus.RUNNING.value, 0),
        "failed": counts.get(HermesDispatchStatus.FAILED.value, 0),
        "uncertain": counts.get(HermesDispatchStatus.UNCERTAIN.value, 0),
        "dead": counts.get(HermesDispatchStatus.DEAD.value, 0),
        "stale_running": stale_running or 0,
        "blocked_threads": blocked_threads or 0,
        "missing_delivery": missing_delivery or 0,
        "reconciliation_backlog": reconciliation_backlog or 0,
        "reconciliation_deferred": reconciliation_deferred or 0,
        "reconciliation_poison": reconciliation_poison or 0,
        "oldest_uncertain_age_seconds": _age_seconds(now, uncertain_created_at),
        "oldest_backlog_age_seconds": _age_seconds(now, oldest_backlog_at),
        "oldest_reconciliation_age_seconds": _age_seconds(
            now,
            oldest_reconciliation_at,
        ),
    }


def _delivery_metrics(
    session: Session,
    *,
    now: datetime,
    missing_delivery: int,
) -> dict[str, object]:
    counts = {
        status.value: count
        for status, count in session.execute(
            select(DeliveryOutboxRecord.status, func.count(DeliveryOutboxRecord.id)).group_by(
                DeliveryOutboxRecord.status
            )
        )
    }
    oldest_backlog_at = session.scalar(
        select(func.min(DeliveryOutboxRecord.created_at)).where(
            DeliveryOutboxRecord.status.in_(
                (
                    DeliveryStatus.QUEUED,
                    DeliveryStatus.DELIVERING,
                    DeliveryStatus.FAILED,
                    DeliveryStatus.UNCERTAIN,
                )
            )
        )
    )
    stale_delivering = session.scalar(
        select(func.count(DeliveryOutboxRecord.id)).where(
            DeliveryOutboxRecord.status == DeliveryStatus.DELIVERING,
            DeliveryOutboxRecord.claimed_at
            <= now - timedelta(seconds=DEFAULT_DELIVERY_STALE_SECONDS),
        )
    )
    return {
        "queued": counts.get(DeliveryStatus.QUEUED.value, 0),
        "delivering": counts.get(DeliveryStatus.DELIVERING.value, 0),
        "delivered": counts.get(DeliveryStatus.DELIVERED.value, 0),
        "failed": counts.get(DeliveryStatus.FAILED.value, 0),
        "uncertain": counts.get(DeliveryStatus.UNCERTAIN.value, 0),
        "stale_delivering": stale_delivering or 0,
        "missing_delivery": missing_delivery,
        "oldest_backlog_age_seconds": _age_seconds(now, oldest_backlog_at),
    }


def _checkpoint_metrics(session: Session) -> dict[str, object]:
    count = session.scalar(
        select(func.count(WechatSyncCheckpoint.id)).where(
            WechatSyncCheckpoint.last_local_id > 0,
            WechatSyncCheckpoint.last_message_fingerprint.is_(None),
        )
    )
    value = count or 0
    return {
        "status": "degraded" if value else "ok",
        "unverified_checkpoint_count": value,
    }


def _empty_dispatch_metrics() -> dict[str, object]:
    return {
        "queued": 0,
        "running": 0,
        "failed": 0,
        "uncertain": 0,
        "dead": 0,
        "stale_running": 0,
        "blocked_threads": 0,
        "missing_delivery": 0,
        "reconciliation_backlog": 0,
        "reconciliation_deferred": 0,
        "reconciliation_poison": 0,
        "oldest_uncertain_age_seconds": None,
        "oldest_backlog_age_seconds": None,
        "oldest_reconciliation_age_seconds": None,
    }


def _empty_delivery_metrics() -> dict[str, object]:
    return {
        "queued": 0,
        "delivering": 0,
        "delivered": 0,
        "failed": 0,
        "uncertain": 0,
        "stale_delivering": 0,
        "missing_delivery": 0,
        "oldest_backlog_age_seconds": None,
    }


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return max(0.0, (now - normalized).total_seconds())


def _observation_is_fresh(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    if not isinstance(value, str):
        return False
    try:
        observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return False
    age_seconds = (now - observed_at.astimezone(UTC)).total_seconds()
    return -_MAX_OBSERVATION_FUTURE_SKEW_SECONDS <= age_seconds <= max_age_seconds


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime health clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _optional_environment_path(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _probe_database(engine: Engine) -> bool:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT 1")) == 1


def _positive_seconds(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive number")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a positive number") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive number")
    return seconds
