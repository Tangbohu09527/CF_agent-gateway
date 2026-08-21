from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from uuid import uuid4

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.runtime.models import RuntimeWorkerStatus

logger = logging.getLogger(__name__)

WECHAT_WORKER_NAME = "wechat"


class WorkerLeaseError(RuntimeError):
    code = "worker_lease_error"


class WorkerLeaseHeldError(WorkerLeaseError):
    code = "worker_lease_held"

    def __init__(self, worker_name: str) -> None:
        self.worker_name = worker_name
        super().__init__(f"an active lease already exists for worker: {worker_name}")


class WorkerLeaseLostError(WorkerLeaseError):
    code = "worker_lease_lost"

    def __init__(self, worker_name: str) -> None:
        self.worker_name = worker_name
        super().__init__(f"worker lease was lost: {worker_name}")


def poll_result_error_code(result: PollResult) -> str | None:
    if result.failures:
        return result.failures[0].code
    if not result.logged_in:
        return "wechat_not_logged_in"
    if result.chats_failed > 0:
        return "poll_chats_failed"
    return None


class DatabaseWorkerStatusReporter:
    """Own a singleton worker lease and persist heartbeat/cycle status."""

    def __init__(
        self,
        database_url: str,
        *,
        hermes_enabled: bool,
        heartbeat_interval_seconds: float,
        heartbeat_stale_after_seconds: float,
        worker_name: str = WECHAT_WORKER_NAME,
        engine_factory: Callable[[str], Engine] = create_database_engine,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_url = database_url
        self._hermes_enabled = hermes_enabled
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._heartbeat_stale_after_seconds = heartbeat_stale_after_seconds
        self._worker_name = worker_name
        self._engine_factory = engine_factory
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._instance_id = uuid4().hex
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._heartbeat_stop = Event()
        self._lease_lost = Event()
        self._lifecycle_lock = Lock()
        self._heartbeat_thread: Thread | None = None

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._heartbeat_thread is not None:
                raise RuntimeError("worker status reporter is already started")
            engine = self._engine_factory(self._database_url)
            try:
                initialize_database(engine)
                self._engine = engine
                self._session_factory = create_database_session_factory(engine)
                self._acquire_lease()
            except Exception:
                self._session_factory = None
                self._engine = None
                engine.dispose()
                raise

            self._heartbeat_stop.clear()
            self._lease_lost.clear()
            try:
                heartbeat_thread = Thread(
                    target=self._heartbeat_loop,
                    name=f"{self._worker_name}-heartbeat",
                    daemon=True,
                )
                self._heartbeat_thread = heartbeat_thread
                heartbeat_thread.start()
            except Exception:
                self._heartbeat_thread = None
                self._heartbeat_stop.set()
                with suppress(Exception):
                    if self._session_factory is not None and not self._lease_lost.is_set():
                        self._update_owned(
                            state="stopped",
                            heartbeat_at=self._now(),
                        )
                engine = self._engine
                self._session_factory = None
                self._engine = None
                with suppress(Exception):
                    if engine is not None:
                        engine.dispose()
                raise

    def stop(self) -> None:
        with self._lifecycle_lock:
            heartbeat_thread = self._heartbeat_thread
            self._heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=self._heartbeat_interval_seconds + 1.0)
            self._heartbeat_thread = None

            if self._session_factory is not None and not self._lease_lost.is_set():
                self._update_owned(
                    state="stopped",
                    heartbeat_at=self._now(),
                )
            if self._engine is not None:
                self._engine.dispose()
            self._session_factory = None
            self._engine = None

    def ensure_active(self) -> None:
        if self._lease_lost.is_set():
            raise WorkerLeaseLostError(self._worker_name)
        if not self._update_owned(heartbeat_at=self._now()):
            raise WorkerLeaseLostError(self._worker_name)

    def cycle_started(self) -> None:
        self.ensure_active()
        now = self._now()
        self._update_owned(
            state="polling",
            heartbeat_at=now,
            last_cycle_started_at=now,
        )
        self.ensure_active()

    def cycle_succeeded(self, result: PollResult) -> None:
        now = self._now()
        failure_code = poll_result_error_code(result)
        healthy = result.logged_in and result.chats_failed == 0 and failure_code is None
        values: dict[str, object] = {
            "state": "idle" if healthy else "degraded",
            "heartbeat_at": now,
            "last_cycle_completed_at": now,
            "last_error_code": failure_code,
            "source_logged_in": result.logged_in,
            "chats_failed": result.chats_failed,
            "messages_seen": result.messages_seen,
            "messages_processed": result.messages_processed,
        }
        if healthy:
            values["last_success_at"] = now
        self._update_owned(**values)
        self.ensure_active()

    def cycle_failed(self, error_code: str) -> None:
        now = self._now()
        self._update_owned(
            state="degraded",
            heartbeat_at=now,
            last_cycle_completed_at=now,
            last_error_code=error_code,
        )
        self.ensure_active()

    def _acquire_lease(self) -> None:
        session_factory = self._require_session_factory()
        now = self._now()
        stale_before = now - timedelta(seconds=self._heartbeat_stale_after_seconds)
        values = {
            "instance_id": self._instance_id,
            "process_id": os.getpid(),
            "state": "starting",
            "hermes_enabled": self._hermes_enabled,
            "delivery_enabled": self._hermes_enabled,
            "started_at": now,
            "heartbeat_at": now,
            "last_cycle_started_at": None,
            "last_cycle_completed_at": None,
            "last_success_at": None,
            "last_error_code": None,
            "source_logged_in": None,
            "chats_failed": 0,
            "messages_seen": 0,
            "messages_processed": 0,
        }
        try:
            with session_factory.begin() as session:
                result = session.execute(
                    update(RuntimeWorkerStatus)
                    .where(RuntimeWorkerStatus.worker_name == self._worker_name)
                    .where(RuntimeWorkerStatus.state == "stopped")
                    .values(**values)
                )
                if result.rowcount == 1:
                    return

                result = session.execute(
                    update(RuntimeWorkerStatus)
                    .where(RuntimeWorkerStatus.worker_name == self._worker_name)
                    .where(RuntimeWorkerStatus.heartbeat_at < stale_before)
                    .values(**values)
                )
                if result.rowcount == 1:
                    logger.warning(
                        "stale worker lease recovered",
                        extra={
                            "fields": {
                                "worker_name": self._worker_name,
                                "recovery_action": "lease_replaced",
                            }
                        },
                    )
                    return

                existing = session.scalar(
                    select(RuntimeWorkerStatus).where(
                        RuntimeWorkerStatus.worker_name == self._worker_name
                    )
                )
                if existing is not None:
                    raise WorkerLeaseHeldError(self._worker_name)
                session.add(
                    RuntimeWorkerStatus(
                        worker_name=self._worker_name,
                        **values,
                    )
                )
        except IntegrityError:
            raise WorkerLeaseHeldError(self._worker_name) from None

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self._heartbeat_interval_seconds):
            if not self._update_owned(heartbeat_at=self._now()):
                return

    def _update_owned(self, **values: object) -> bool:
        session_factory = self._require_session_factory()
        try:
            with session_factory.begin() as session:
                result = session.execute(
                    update(RuntimeWorkerStatus)
                    .where(RuntimeWorkerStatus.worker_name == self._worker_name)
                    .where(RuntimeWorkerStatus.instance_id == self._instance_id)
                    .values(**values)
                )
                if result.rowcount != 1:
                    self._lease_lost.set()
                    return False
        except SQLAlchemyError:
            logger.error(
                "worker heartbeat update failed",
                extra={"fields": {"error_code": "worker_status_storage_failed"}},
            )
            self._lease_lost.set()
            return False
        return True

    def _require_session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            raise RuntimeError("worker status reporter is not started")
        return self._session_factory

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
