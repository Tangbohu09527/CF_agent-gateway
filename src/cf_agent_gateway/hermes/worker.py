from __future__ import annotations

import logging
import math
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import TIMEOUT_MAX, Event, Thread
from time import monotonic
from typing import Protocol
from uuid import uuid4

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.hermes.outbox import dispatch_error_code, dispatch_failure_status
from cf_agent_gateway.hermes.response import HermesResponseProcessor
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.response.store import DeliveryTarget, ResponseStore
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchStateConflictError,
    HermesDispatchStatus,
)

logger = logging.getLogger(__name__)
RECONCILIATION_BATCH_SIZE = 25
RECONCILIATION_SCAN_INTERVAL_SECONDS = 5.0
RECONCILIATION_BACKOFF_BASE_SECONDS = 30.0
RECONCILIATION_BACKOFF_MAX_SECONDS = 300.0
RECONCILIATION_QUARANTINE_FAILURES = 5
RECONCILIATION_ERROR_CODE = "dispatch_reconciliation_candidate_invalid"

DispatcherFactory = Callable[[Session], "HermesRecordDispatcher"]
ResponseProcessorFactory = Callable[[Session], HermesResponseProcessor]


class HermesRecordDispatcher(Protocol):
    def dispatch_record(self, record: HermesDispatchRecord) -> HermesDispatchOutcome: ...


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    record_id: int
    claim_token: str
    ai_thread_id: str
    idempotency_key: str
    attempt_count: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class DispatchProcessResult:
    record_id: int
    status: HermesDispatchStatus
    response_id: int | None = None
    error_code: str | None = None
    delivery_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationFailureState:
    record_id: int
    failure_count: int
    next_attempt_at: datetime | None
    quarantined_at: datetime | None


class HermesDispatchWorker:
    """Claim and execute durable Hermes dispatches with per-thread FIFO."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        dispatcher_factory: DispatcherFactory,
        *,
        lease_seconds: float,
        retry_limit: int,
        response_processor_factory: ResponseProcessorFactory | None = None,
        reconcile_persisted_responses: bool = False,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
            or lease_seconds > TIMEOUT_MAX
        ):
            raise ValueError("lease_seconds must be a positive number")
        if isinstance(retry_limit, bool) or not isinstance(retry_limit, int) or retry_limit < 0:
            raise ValueError("retry_limit must be a non-negative integer")
        if not isinstance(reconcile_persisted_responses, bool):
            raise TypeError("reconcile_persisted_responses must be a boolean")
        self._session_factory = session_factory
        self._dispatcher_factory = dispatcher_factory
        self._response_processor_factory = response_processor_factory
        self._reconcile_persisted_responses = reconcile_persisted_responses
        self._reconciliation_cursor = 0
        self._next_reconciliation_scan_at = 0.0
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock or monotonic
        self._lease_seconds = float(lease_seconds)
        self._retry_limit = retry_limit

    def claim_once(self, *, now: datetime | None = None) -> DispatchClaim | None:
        """Atomically claim one eligible thread head and commit before returning."""

        token = str(uuid4())
        with self._session_factory() as session:
            record = HermesDispatchRecordStore(session).claim_next(
                claim_token=token,
                lease_seconds=self._lease_seconds,
                retry_limit=self._retry_limit,
                now=now,
            )
            if record is None:
                return None
            if record.claim_token is None or record.lease_expires_at is None:
                raise HermesDispatchStateConflictError(
                    record_id=record.id,
                    expected_status=HermesDispatchStatus.RUNNING,
                )
            claim = DispatchClaim(
                record_id=record.id,
                claim_token=record.claim_token,
                ai_thread_id=record.ai_thread_id,
                idempotency_key=record.idempotency_key,
                attempt_count=record.attempt_count,
                lease_expires_at=record.lease_expires_at,
            )
            logger.info(
                "dispatch claimed",
                extra={
                    "fields": {
                        "dispatch_record_id": claim.record_id,
                        "attempt_count": claim.attempt_count,
                    }
                },
            )
            return claim

    def process_claim(self, claim: DispatchClaim) -> DispatchProcessResult:
        """Execute one claim, persist its response, then invoke the delivery pipeline."""

        execution_session = self._session_factory()
        try:
            record = HermesDispatchRecordStore(execution_session).get(claim.record_id)
            if (
                record is None
                or record.status is not HermesDispatchStatus.RUNNING
                or record.claim_token != claim.claim_token
            ):
                raise HermesDispatchStateConflictError(
                    record_id=claim.record_id,
                    expected_status=HermesDispatchStatus.RUNNING,
                )
            dispatcher = self._dispatcher_factory(execution_session)
            with self._lease_heartbeat(claim):
                try:
                    outcome = dispatcher.dispatch_record(record)
                except Exception as error:
                    with suppress(Exception):
                        execution_session.rollback()
                    return self._record_failure(claim, error)

                execution_session.close()
                with self._session_factory() as completion_session:
                    response = HermesDispatchResponseStore(completion_session).complete_success(
                        claim.record_id,
                        claim_token=claim.claim_token,
                        outcome=outcome,
                    )
        finally:
            execution_session.close()

        delivery_error_code = self._deliver(outcome)
        result = DispatchProcessResult(
            record_id=claim.record_id,
            status=HermesDispatchStatus.SUCCESS,
            response_id=response.id,
            delivery_error_code=delivery_error_code,
        )
        self._log_result(result)
        return result

    def run_once(self, *, now: datetime | None = None) -> DispatchProcessResult | None:
        """Claim and synchronously process at most one dispatch."""

        self.reconcile_once(now=now)
        claim = self.claim_once(now=now)
        if claim is None:
            return None
        return self.process_claim(claim)

    def reconcile_once(self, *, now: datetime | None = None) -> bool:
        """Repair one persisted Hermes result without making another Hermes call."""

        if not self._reconcile_persisted_responses:
            return False
        reconciliation_now = _validated_reconciliation_now(now or self._clock())
        candidate_ids = self._reconciliation_candidate_ids(
            after_record_id=self._reconciliation_cursor,
            now=reconciliation_now,
        )
        if not candidate_ids and self._reconciliation_cursor:
            self._reconciliation_cursor = 0
            candidate_ids = self._reconciliation_candidate_ids(
                after_record_id=0,
                now=reconciliation_now,
            )
        for record_id in candidate_ids:
            self._reconciliation_cursor = record_id
            try:
                if self._reconcile_candidate(record_id):
                    return True
            except DBAPIError:
                raise
            except Exception:
                failure = self._defer_reconciliation_candidate(
                    record_id,
                    now=reconciliation_now,
                )
                if failure is not None:
                    self._log_reconciliation_failure(failure)
        return False

    def _reconciliation_candidate_ids(
        self,
        *,
        after_record_id: int,
        now: datetime,
    ) -> list[int]:
        with self._session_factory() as session:
            response_exists = exists(
                select(ResponseRecord.response_id)
                .where(ResponseRecord.message_id == HermesDispatchRecord.message_id)
                .correlate(HermesDispatchRecord)
            )
            delivery_exists = exists(
                select(DeliveryOutboxRecord.id)
                .join(
                    ResponseRecord,
                    ResponseRecord.response_id == DeliveryOutboxRecord.response_id,
                )
                .where(ResponseRecord.message_id == HermesDispatchRecord.message_id)
                .correlate(HermesDispatchRecord)
            )
            statement = (
                select(HermesDispatchRecord.id)
                .join(
                    HermesDispatchResponse,
                    HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
                )
                .join(Message, Message.id == HermesDispatchRecord.message_id)
                .where(
                    HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
                    HermesDispatchRecord.id > after_record_id,
                    Message.source == "wechat",
                    HermesDispatchRecord.reconciliation_quarantined_at.is_(None),
                    or_(
                        HermesDispatchRecord.reconciliation_next_attempt_at.is_(None),
                        HermesDispatchRecord.reconciliation_next_attempt_at <= now,
                    ),
                    or_(~response_exists, ~delivery_exists),
                )
                .order_by(HermesDispatchRecord.id)
                .limit(RECONCILIATION_BATCH_SIZE)
            )
            return list(session.scalars(statement))

    def _reconcile_candidate(self, record_id: int) -> bool:
        with self._session_factory() as session:
            statement = (
                select(HermesDispatchRecord, HermesDispatchResponse, Message)
                .join(
                    HermesDispatchResponse,
                    HermesDispatchResponse.dispatch_record_id == HermesDispatchRecord.id,
                )
                .join(Message, Message.id == HermesDispatchRecord.message_id)
                .where(
                    HermesDispatchRecord.id == record_id,
                    HermesDispatchRecord.status == HermesDispatchStatus.SUCCESS,
                    Message.source == "wechat",
                )
            )
            row = session.execute(statement).one_or_none()
            if row is None:
                return False
            record, raw_response, message = row
            outcome = HermesDispatchResponseStore.to_outcome(raw_response, record)
            _, delivery, created = ResponseStore(session).save_generated(
                outcome,
                target=DeliveryTarget(
                    channel=message.source,
                    account_id=message.source_account_id,
                    conversation_id=message.conversation_id,
                ),
            )
            self._clear_reconciliation_failure(session, record.id)
            logger.info(
                "dispatch response reconciled",
                extra={
                    "fields": {
                        "dispatch_record_id": record.id,
                        "message_id": record.message_id,
                        "delivery_id": delivery.id,
                        "created": created,
                        "recovery_action": "persisted_response_replayed",
                    }
                },
            )
            return created

    def _defer_reconciliation_candidate(
        self,
        record_id: int,
        *,
        now: datetime,
    ) -> ReconciliationFailureState | None:
        with self._session_factory() as session:
            record = session.get(HermesDispatchRecord, record_id)
            if (
                record is None
                or record.reconciliation_quarantined_at is not None
                or (
                    record.reconciliation_next_attempt_at is not None
                    and _aware_database_datetime(record.reconciliation_next_attempt_at) > now
                )
            ):
                return None
            failure_count = record.reconciliation_failure_count + 1
            quarantined_at = now if failure_count >= RECONCILIATION_QUARANTINE_FAILURES else None
            next_attempt_at = None
            if quarantined_at is None:
                delay_seconds = min(
                    RECONCILIATION_BACKOFF_MAX_SECONDS,
                    RECONCILIATION_BACKOFF_BASE_SECONDS * (2 ** (failure_count - 1)),
                )
                next_attempt_at = now + timedelta(seconds=delay_seconds)
            statement = (
                update(HermesDispatchRecord)
                .where(
                    HermesDispatchRecord.id == record_id,
                    HermesDispatchRecord.reconciliation_failure_count
                    == record.reconciliation_failure_count,
                    HermesDispatchRecord.reconciliation_quarantined_at.is_(None),
                    or_(
                        HermesDispatchRecord.reconciliation_next_attempt_at.is_(None),
                        HermesDispatchRecord.reconciliation_next_attempt_at <= now,
                    ),
                )
                .values(
                    reconciliation_failure_count=failure_count,
                    reconciliation_next_attempt_at=next_attempt_at,
                    reconciliation_quarantined_at=quarantined_at,
                    reconciliation_last_error_code=RECONCILIATION_ERROR_CODE,
                    updated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(statement)
            if result.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            return ReconciliationFailureState(
                record_id=record_id,
                failure_count=failure_count,
                next_attempt_at=next_attempt_at,
                quarantined_at=quarantined_at,
            )

    @staticmethod
    def _clear_reconciliation_failure(session: Session, record_id: int) -> None:
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record_id,
                HermesDispatchRecord.reconciliation_failure_count > 0,
            )
            .values(
                reconciliation_failure_count=0,
                reconciliation_next_attempt_at=None,
                reconciliation_quarantined_at=None,
                reconciliation_last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = session.execute(statement)
        if result.rowcount:
            session.commit()

    @staticmethod
    def _log_reconciliation_failure(failure: ReconciliationFailureState) -> None:
        quarantined = failure.quarantined_at is not None
        logger.log(
            logging.ERROR if quarantined else logging.WARNING,
            (
                "dispatch response reconciliation candidate quarantined"
                if quarantined
                else "dispatch response reconciliation candidate deferred"
            ),
            extra={
                "fields": {
                    "dispatch_record_id": failure.record_id,
                    "error_code": RECONCILIATION_ERROR_CODE,
                    "failure_count": failure.failure_count,
                    "next_attempt_at": (
                        failure.next_attempt_at.isoformat().replace("+00:00", "Z")
                        if failure.next_attempt_at is not None
                        else None
                    ),
                    "recovery_action": (
                        "candidate_quarantined" if quarantined else "candidate_backoff"
                    ),
                }
            },
        )

    def _reconcile_if_due(self) -> bool:
        now = float(self._monotonic_clock())
        if now < self._next_reconciliation_scan_at:
            return False
        self._next_reconciliation_scan_at = now + RECONCILIATION_SCAN_INTERVAL_SECONDS
        return self.reconcile_once()

    def run(
        self,
        *,
        stop_event: Event,
        concurrency: int,
        idle_poll_seconds: float = 0.25,
    ) -> None:
        """Continuously fill worker slots until shutdown, then drain active calls."""

        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        if (
            not math.isfinite(idle_poll_seconds)
            or idle_poll_seconds <= 0
            or idle_poll_seconds > TIMEOUT_MAX
        ):
            raise ValueError("idle_poll_seconds must be positive")

        active: set[Future[DispatchProcessResult]] = set()
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="hermes-dispatch",
        ) as executor:
            while not stop_event.is_set():
                try:
                    self._reconcile_if_due()
                except DBAPIError:
                    self._log_database_retry("reconciliation")
                    stop_event.wait(idle_poll_seconds)
                    continue
                except Exception:
                    logger.error(
                        "dispatch response reconciliation failed",
                        extra={"fields": {"error_code": "dispatch_reconciliation_failed"}},
                    )
                claimed = False
                while len(active) < concurrency and not stop_event.is_set():
                    try:
                        claim = self.claim_once()
                    except DBAPIError:
                        self._log_database_retry("claim")
                        stop_event.wait(idle_poll_seconds)
                        break
                    if claim is None:
                        break
                    active.add(executor.submit(self.process_claim, claim))
                    claimed = True

                if active:
                    completed, active = wait(
                        active,
                        timeout=idle_poll_seconds,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        self._log_future(future)
                elif not claimed:
                    stop_event.wait(idle_poll_seconds)

            for future in active:
                self._log_future(future)

    @staticmethod
    def _log_database_retry(operation: str) -> None:
        logger.warning(
            "dispatch database operation will retry",
            extra={
                "fields": {
                    "operation": operation,
                    "error_code": "dispatch_database_temporarily_unavailable",
                }
            },
        )

    def _record_failure(
        self,
        claim: DispatchClaim,
        error: Exception,
    ) -> DispatchProcessResult:
        error_code = dispatch_error_code(error)
        failure_status = dispatch_failure_status(error)
        with self._session_factory() as failure_session:
            store = HermesDispatchRecordStore(failure_session)
            if failure_status is HermesDispatchStatus.FAILED:
                record = store.mark_failed(
                    claim.record_id,
                    claim_token=claim.claim_token,
                    error_code=error_code,
                    retry_limit=self._retry_limit,
                )
            else:
                record = store.mark_uncertain(
                    claim.record_id,
                    claim_token=claim.claim_token,
                    error_code=error_code,
                )
        result = DispatchProcessResult(
            record_id=claim.record_id,
            status=record.status,
            error_code=error_code,
        )
        self._log_result(result)
        return result

    def _deliver(self, outcome: HermesDispatchOutcome) -> str | None:
        if self._response_processor_factory is None:
            return None
        with self._session_factory() as delivery_session:
            processor = self._response_processor_factory(delivery_session)
            try:
                processor.handle(outcome)
            except Exception as error:
                with suppress(Exception):
                    delivery_session.rollback()
                error_code = dispatch_error_code(error)
                logger.error(
                    "dispatch response delivery failed",
                    extra={
                        "fields": {
                            "message_id": outcome.message_id,
                            "error_code": error_code,
                        }
                    },
                )
                return error_code
        return None

    def _lease_heartbeat(self, claim: DispatchClaim) -> _LeaseHeartbeat:
        return _LeaseHeartbeat(
            self._session_factory,
            claim,
            lease_seconds=self._lease_seconds,
        )

    @staticmethod
    def _log_future(future: Future[DispatchProcessResult]) -> None:
        try:
            future.result()
        except Exception as error:
            logger.error(
                "dispatch processing failed",
                extra={
                    "fields": {
                        "error_code": "dispatch_processing_failed",
                        "exception_type": type(error).__name__,
                    }
                },
            )
            return

    @staticmethod
    def _log_result(result: DispatchProcessResult) -> None:
        event = {
            HermesDispatchStatus.SUCCESS: "dispatch success",
            HermesDispatchStatus.FAILED: "dispatch failed",
            HermesDispatchStatus.UNCERTAIN: "dispatch uncertain",
            HermesDispatchStatus.DEAD: "dispatch dead",
        }.get(result.status, "dispatch processed")
        level = (
            logging.INFO
            if result.status is HermesDispatchStatus.SUCCESS
            else logging.ERROR
            if result.status is HermesDispatchStatus.DEAD
            else logging.WARNING
        )
        logger.log(
            level,
            event,
            extra={
                "fields": {
                    "dispatch_record_id": result.record_id,
                    "status": result.status.value,
                    "error_code": result.error_code,
                    "delivery_error_code": result.delivery_error_code,
                }
            },
        )


class _LeaseHeartbeat:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        claim: DispatchClaim,
        *,
        lease_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._claim = claim
        self._lease_seconds = lease_seconds
        self._stop_event = Event()
        self._thread: Thread | None = None

    def __enter__(self) -> _LeaseHeartbeat:
        with self._session_factory() as session:
            HermesDispatchRecordStore(session).renew_lease(
                self._claim.record_id,
                claim_token=self._claim.claim_token,
                lease_seconds=self._lease_seconds,
            )

        interval = self._lease_seconds / 3
        self._thread = Thread(
            target=self._run,
            args=(interval,),
            name=f"dispatch-lease-{self._claim.record_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._lease_seconds))

    def _run(self, interval: float) -> None:
        while not self._stop_event.wait(interval):
            try:
                with self._session_factory() as session:
                    HermesDispatchRecordStore(session).renew_lease(
                        self._claim.record_id,
                        claim_token=self._claim.claim_token,
                        lease_seconds=self._lease_seconds,
                    )
            except Exception as error:
                logger.warning(
                    "worker lease lost",
                    extra={
                        "fields": {
                            "dispatch_record_id": self._claim.record_id,
                            "error_code": "dispatch_lease_renewal_failed",
                            "exception_type": type(error).__name__,
                        }
                    },
                )
                return


def _validated_reconciliation_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reconciliation clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _aware_database_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
