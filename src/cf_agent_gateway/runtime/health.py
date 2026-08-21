from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import suppress
from datetime import UTC, datetime
from threading import BoundedSemaphore
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat import AgentWechatClient
from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import DATABASE_CONNECT_TIMEOUT_SECONDS
from cf_agent_gateway.hermes.models import (
    HermesDeliveryRecord,
    HermesDispatchRecord,
    HermesOperationStatus,
)
from cf_agent_gateway.runtime.models import RuntimeWorkerStatus
from cf_agent_gateway.runtime.status import WECHAT_WORKER_NAME

HEALTH_PROBE_TIMEOUT_SECONDS = DATABASE_CONNECT_TIMEOUT_SECONDS
MAX_CONCURRENT_HEALTH_PROBES = 2
_HEALTH_PROBE_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_HEALTH_PROBES,
    thread_name_prefix="health-probe",
)
_HEALTH_PROBE_SLOTS = BoundedSemaphore(MAX_CONCURRENT_HEALTH_PROBES)

HealthStatus = Literal["ok", "degraded", "disabled"]
ConnectionStatus = Literal["reachable", "unreachable", "not_checked"]


class HealthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatabaseHealth(HealthModel):
    status: HealthStatus


class WorkerHealth(HealthModel):
    status: HealthStatus
    state: str | None = None
    heartbeat_at: datetime | None = None
    last_cycle_started_at: datetime | None = None
    last_success_at: datetime | None = None
    stale_after_seconds: float
    cycle_stale_after_seconds: float
    configuration_matches: bool | None = None
    last_error_code: str | None = None


class OperationCounts(HealthModel):
    in_progress: int = 0
    stale: int = 0
    failed: int = 0
    succeeded: int = 0
    missing: int = 0


class QueueHealth(HealthModel):
    status: HealthStatus
    mode: Literal["inline_durable"] = "inline_durable"
    in_progress: int = 0
    stale: int = 0
    failed: int = 0
    missing: int = 0


class DependencyHealth(HealthModel):
    status: HealthStatus
    enabled: bool
    connection: ConnectionStatus
    operations: OperationCounts


class HealthComponents(HealthModel):
    database: DatabaseHealth
    worker: WorkerHealth
    queue: QueueHealth
    hermes: DependencyHealth
    delivery: DependencyHealth


class HealthResponse(HealthModel):
    status: Literal["ok", "degraded"]
    components: HealthComponents


Probe = Callable[[Settings], ConnectionStatus]


def check_runtime_health(
    session: Session,
    settings: Settings,
    *,
    clock: Callable[[], datetime] | None = None,
    hermes_probe: Probe | None = None,
    delivery_probe: Probe | None = None,
) -> HealthResponse:
    """Build a bounded, side-effect-free readiness view of the V2 runtime."""

    now = _as_utc((clock or (lambda: datetime.now(UTC)))())
    database_ok = True
    worker_status: RuntimeWorkerStatus | None = None
    dispatch_counts = OperationCounts()
    delivery_counts = OperationCounts()
    sqlite_timeout_configured = False
    try:
        sqlite_timeout_configured = _configure_database_health_timeout(session)
        session.execute(select(1))
        worker_status = session.get(RuntimeWorkerStatus, WECHAT_WORKER_NAME)
        dispatch_counts = _operation_counts(session, HermesDispatchRecord, now)
        delivery_counts = _operation_counts(session, HermesDeliveryRecord, now)
        delivery_counts = delivery_counts.model_copy(
            update={"missing": _missing_delivery_count(session)}
        )
    except SQLAlchemyError:
        database_ok = False
        with suppress(Exception):
            session.rollback()
    finally:
        if sqlite_timeout_configured:
            with suppress(Exception):
                session.execute(text("PRAGMA busy_timeout=30000"))

    connections = _probe_dependencies(
        settings,
        hermes_probe=hermes_probe or _probe_hermes,
        delivery_probe=delivery_probe or _probe_delivery,
    )
    worker = _worker_health(
        settings,
        worker_status,
        database_ok=database_ok,
        now=now,
    )
    queue = _queue_health(
        enabled=settings.hermes.enabled,
        database_ok=database_ok,
        dispatch=dispatch_counts,
        delivery=delivery_counts,
    )
    hermes = _dependency_health(
        enabled=settings.hermes.enabled,
        database_ok=database_ok,
        connection=connections["hermes"],
        operations=dispatch_counts,
    )
    delivery_enabled = settings.wechat.enabled and settings.hermes.enabled
    delivery = _dependency_health(
        enabled=delivery_enabled,
        database_ok=database_ok,
        connection=connections["delivery"],
        operations=delivery_counts,
    )
    components = HealthComponents(
        database=DatabaseHealth(status="ok" if database_ok else "degraded"),
        worker=worker,
        queue=queue,
        hermes=hermes,
        delivery=delivery,
    )
    overall_status = (
        "degraded"
        if any(
            component.status == "degraded"
            for component in (
                components.database,
                components.worker,
                components.queue,
                components.hermes,
                components.delivery,
            )
        )
        else "ok"
    )
    return HealthResponse(status=overall_status, components=components)


def _worker_health(
    settings: Settings,
    status: RuntimeWorkerStatus | None,
    *,
    database_ok: bool,
    now: datetime,
) -> WorkerHealth:
    stale_after = settings.runtime.heartbeat_stale_after_seconds
    cycle_stale_after = settings.runtime.cycle_stale_after_seconds
    if not settings.wechat.enabled:
        return WorkerHealth(
            status="disabled",
            stale_after_seconds=stale_after,
            cycle_stale_after_seconds=cycle_stale_after,
        )
    if not database_ok or status is None:
        return WorkerHealth(
            status="degraded",
            stale_after_seconds=stale_after,
            cycle_stale_after_seconds=cycle_stale_after,
        )

    heartbeat_at = _as_utc(status.heartbeat_at)
    last_cycle_started_at = (
        _as_utc(status.last_cycle_started_at) if status.last_cycle_started_at else None
    )
    last_success_at = _as_utc(status.last_success_at) if status.last_success_at else None
    heartbeat_stale = (now - heartbeat_at).total_seconds() > stale_after
    success_stale = (
        last_success_at is None or (now - last_success_at).total_seconds() > cycle_stale_after
    )
    polling_stale = status.state == "polling" and (
        last_cycle_started_at is None
        or (now - last_cycle_started_at).total_seconds() > cycle_stale_after
    )
    healthy_state = status.state in {"idle", "polling"}
    expected_delivery_enabled = settings.wechat.enabled and settings.hermes.enabled
    configuration_matches = (
        status.hermes_enabled == settings.hermes.enabled
        and status.delivery_enabled == expected_delivery_enabled
    )
    healthy = (
        not heartbeat_stale
        and not success_stale
        and not polling_stale
        and healthy_state
        and configuration_matches
        and status.last_error_code is None
    )
    return WorkerHealth(
        status="ok" if healthy else "degraded",
        state=status.state,
        heartbeat_at=heartbeat_at,
        last_cycle_started_at=last_cycle_started_at,
        last_success_at=last_success_at,
        stale_after_seconds=stale_after,
        cycle_stale_after_seconds=cycle_stale_after,
        configuration_matches=configuration_matches,
        last_error_code=status.last_error_code,
    )


def _operation_counts(
    session: Session,
    model: type[HermesDispatchRecord] | type[HermesDeliveryRecord],
    now: datetime,
) -> OperationCounts:
    grouped = dict(
        session.execute(select(model.status, func.count(model.id)).group_by(model.status)).all()
    )
    stale = session.scalar(
        select(func.count(model.id)).where(
            model.status == HermesOperationStatus.IN_PROGRESS,
            model.lease_expires_at <= now,
        )
    )
    return OperationCounts(
        in_progress=grouped.get(HermesOperationStatus.IN_PROGRESS, 0),
        stale=stale or 0,
        failed=grouped.get(HermesOperationStatus.FAILED, 0),
        succeeded=grouped.get(HermesOperationStatus.SUCCEEDED, 0),
    )


def _queue_health(
    *,
    enabled: bool,
    database_ok: bool,
    dispatch: OperationCounts,
    delivery: OperationCounts,
) -> QueueHealth:
    if not enabled:
        return QueueHealth(status="disabled")
    in_progress = dispatch.in_progress + delivery.in_progress
    stale = dispatch.stale + delivery.stale
    failed = dispatch.failed + delivery.failed
    missing = delivery.missing
    healthy = database_ok and stale == 0 and failed == 0 and missing == 0
    return QueueHealth(
        status="ok" if healthy else "degraded",
        in_progress=in_progress,
        stale=stale,
        failed=failed,
        missing=missing,
    )


def _dependency_health(
    *,
    enabled: bool,
    database_ok: bool,
    connection: ConnectionStatus,
    operations: OperationCounts,
) -> DependencyHealth:
    if not enabled:
        return DependencyHealth(
            status="disabled",
            enabled=False,
            connection="not_checked",
            operations=operations,
        )
    healthy = (
        database_ok
        and connection == "reachable"
        and operations.stale == 0
        and operations.failed == 0
        and operations.missing == 0
    )
    return DependencyHealth(
        status="ok" if healthy else "degraded",
        enabled=True,
        connection=connection,
        operations=operations,
    )


def _missing_delivery_count(session: Session) -> int:
    count = session.scalar(
        select(func.count(HermesDispatchRecord.id))
        .outerjoin(
            HermesDeliveryRecord,
            HermesDeliveryRecord.message_id == HermesDispatchRecord.message_id,
        )
        .where(
            HermesDispatchRecord.status == HermesOperationStatus.SUCCEEDED,
            HermesDeliveryRecord.id.is_(None),
        )
    )
    return count or 0


def _probe_dependencies(
    settings: Settings,
    *,
    hermes_probe: Probe,
    delivery_probe: Probe,
) -> dict[str, ConnectionStatus]:
    probes: dict[str, Probe] = {}
    if settings.hermes.enabled:
        probes["hermes"] = hermes_probe
    if settings.wechat.enabled and settings.hermes.enabled:
        probes["delivery"] = delivery_probe
    results: dict[str, ConnectionStatus] = {
        "hermes": "not_checked",
        "delivery": "not_checked",
    }
    if not probes:
        return results

    futures: dict[Future[ConnectionStatus], str] = {}
    for name, probe in probes.items():
        if not _HEALTH_PROBE_SLOTS.acquire(blocking=False):
            results[name] = "unreachable"
            continue
        try:
            future = _HEALTH_PROBE_EXECUTOR.submit(
                _run_bounded_probe,
                probe,
                settings,
            )
            future.add_done_callback(_release_cancelled_probe_slot)
        except Exception:
            _HEALTH_PROBE_SLOTS.release()
            results[name] = "unreachable"
        else:
            futures[future] = name

    done, not_done = wait(futures, timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
    for future in done:
        name = futures[future]
        try:
            results[name] = future.result()
        except Exception:
            results[name] = "unreachable"
    for future in not_done:
        results[futures[future]] = "unreachable"
        future.cancel()
    return results


def _probe_hermes(settings: Settings) -> ConnectionStatus:
    api_key = os.getenv(settings.hermes.api_key_env)
    if api_key is None or not api_key.strip():
        return "unreachable"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key.strip()}",
    }
    try:
        with httpx.Client(
            timeout=HEALTH_PROBE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            endpoint = settings.hermes.base_url.rstrip("/") + "/v1/chat/completions"
            response = client.head(endpoint, headers=headers)
    except (httpx.HTTPError, ValueError):
        return "unreachable"
    if 200 <= response.status_code < 300 or response.status_code == 405:
        return "reachable"
    return "unreachable"


def _probe_delivery(settings: Settings) -> ConnectionStatus:
    token = os.getenv(settings.wechat.token_env)
    if token is None or not token.strip():
        return "unreachable"
    client: AgentWechatClient | None = None
    try:
        client = AgentWechatClient(
            base_url=settings.wechat.base_url,
            token=token,
            timeout=HEALTH_PROBE_TIMEOUT_SECONDS,
        )
        auth_status = client.get_auth_status()
        if auth_status.status == "logged_in" and auth_status.logged_in_user:
            return "reachable"
    except Exception:
        return "unreachable"
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()
    return "unreachable"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _run_bounded_probe(probe: Probe, settings: Settings) -> ConnectionStatus:
    try:
        return probe(settings)
    finally:
        _HEALTH_PROBE_SLOTS.release()


def _release_cancelled_probe_slot(future: Future[ConnectionStatus]) -> None:
    if future.cancelled():
        _HEALTH_PROBE_SLOTS.release()


def _configure_database_health_timeout(session: Session) -> bool:
    dialect_name = session.get_bind().dialect.name
    timeout_milliseconds = int(HEALTH_PROBE_TIMEOUT_SECONDS * 1000)
    if dialect_name == "sqlite":
        session.execute(text(f"PRAGMA busy_timeout={timeout_milliseconds}"))
        return True
    if dialect_name == "postgresql":
        session.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{timeout_milliseconds}ms"},
        )
    return False
