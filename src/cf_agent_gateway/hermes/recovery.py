from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.hermes.models import HermesDispatchOutcome, HermesOperationStatus
from cf_agent_gateway.hermes.response import HermesResponseProcessor
from cf_agent_gateway.hermes.service import HermesDispatcher
from cf_agent_gateway.hermes.store import DEFAULT_RECOVERY_BATCH_SIZE, HermesLedgerStore
from cf_agent_gateway.workspace.models import EmployeeWorkspace

logger = logging.getLogger(__name__)


class DatabaseSessionFactory(Protocol):
    def __call__(self) -> Session: ...


class HermesDispatcherFactory(Protocol):
    def __call__(self, session: Session, /) -> HermesDispatcher: ...


class HermesResponseProcessorFactory(Protocol):
    def __call__(self, session: Session, /) -> HermesResponseProcessor: ...


@dataclass(frozen=True, slots=True)
class HermesRecoveryResult:
    dispatch_candidates: int
    dispatch_recovered: int
    dispatch_failed: int
    delivery_candidates: int
    delivery_recovered: int
    delivery_failed: int


class HermesRecoveryService:
    """Drain durable Hermes and delivery retries independently of source replay."""

    def __init__(
        self,
        session_factory: DatabaseSessionFactory,
        dispatcher_factory: HermesDispatcherFactory,
        response_processor_factory: HermesResponseProcessorFactory,
        *,
        batch_size: int = DEFAULT_RECOVERY_BATCH_SIZE,
        clock: Callable[[], datetime] | None = None,
        lease_guard: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher_factory = dispatcher_factory
        self._response_processor_factory = response_processor_factory
        self._batch_size = batch_size
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._lease_guard = lease_guard

    def drain(self) -> HermesRecoveryResult:
        self._ensure_active()
        due_at = self._clock()
        with self._session_factory() as session:
            ledger = HermesLedgerStore(session)
            dispatch_ids = ledger.list_due_dispatch_ids(
                now=due_at,
                limit=self._batch_size,
            )

        dispatch_recovered = 0
        for dispatch_id in dispatch_ids:
            self._ensure_active()
            dispatch_recovered += self._recover_dispatch(dispatch_id)

        self._ensure_active()
        with self._session_factory() as session:
            delivery_ids = HermesLedgerStore(session).list_due_delivery_ids(
                now=due_at,
                limit=self._batch_size,
            )
        delivery_recovered = 0
        for dispatch_id in delivery_ids:
            self._ensure_active()
            delivery_recovered += self._recover_delivery(dispatch_id)
        return HermesRecoveryResult(
            dispatch_candidates=len(dispatch_ids),
            dispatch_recovered=dispatch_recovered,
            dispatch_failed=len(dispatch_ids) - dispatch_recovered,
            delivery_candidates=len(delivery_ids),
            delivery_recovered=delivery_recovered,
            delivery_failed=len(delivery_ids) - delivery_recovered,
        )

    def _ensure_active(self) -> None:
        if self._lease_guard is not None:
            self._lease_guard()

    def _recover_dispatch(self, dispatch_id: int) -> bool:
        message_id: int | None = None
        try:
            with self._session_factory() as session:
                record = HermesLedgerStore(session).get_dispatch_by_id(dispatch_id)
                if record is None:
                    self._log_failure(
                        operation="dispatch",
                        dispatch_id=dispatch_id,
                        message_id=None,
                        error_code="dispatch_record_missing",
                    )
                    return False
                message_id = record.message_id
                workspace = session.get(EmployeeWorkspace, record.workspace_id)
                if workspace is None:
                    self._log_failure(
                        operation="dispatch",
                        dispatch_id=dispatch_id,
                        message_id=message_id,
                        error_code="workspace_missing",
                    )
                    return False

                admission = AdmissionOutcome(
                    message_id=record.message_id,
                    admitted=True,
                    should_create_task=True,
                    reason=AdmissionReason.ALLOWED,
                    enterprise_identity_id=workspace.enterprise_identity_id,
                    workspace_id=record.workspace_id,
                    ai_thread_id=record.ai_thread_id,
                )
                self._dispatcher_factory(session).dispatch(admission)
        except Exception as error:
            self._defer_candidate(operation="dispatch", dispatch_id=dispatch_id)
            self._log_failure(
                operation="dispatch",
                dispatch_id=dispatch_id,
                message_id=message_id,
                error_code=_safe_error_code(error),
            )
            return False

        self._log_recovered(
            operation="dispatch",
            dispatch_id=dispatch_id,
            message_id=message_id,
        )
        return True

    def _recover_delivery(self, dispatch_id: int) -> bool:
        message_id: int | None = None
        try:
            with self._session_factory() as session:
                record = HermesLedgerStore(session).get_dispatch_by_id(dispatch_id)
                if record is None:
                    self._log_failure(
                        operation="delivery",
                        dispatch_id=dispatch_id,
                        message_id=None,
                        error_code="dispatch_record_missing",
                    )
                    return False
                message_id = record.message_id
                if (
                    record.status is not HermesOperationStatus.SUCCEEDED
                    or record.assistant_content is None
                ):
                    self._log_failure(
                        operation="delivery",
                        dispatch_id=dispatch_id,
                        message_id=message_id,
                        error_code="dispatch_record_not_deliverable",
                    )
                    return False

                response = HermesDispatchOutcome(
                    message_id=record.message_id,
                    workspace_id=record.workspace_id,
                    ai_thread_id=record.ai_thread_id,
                    assistant_content=record.assistant_content,
                )
                self._response_processor_factory(session).handle(response)
        except Exception as error:
            self._defer_candidate(operation="delivery", dispatch_id=dispatch_id)
            self._log_failure(
                operation="delivery",
                dispatch_id=dispatch_id,
                message_id=message_id,
                error_code=_safe_error_code(error),
            )
            return False

        self._log_recovered(
            operation="delivery",
            dispatch_id=dispatch_id,
            message_id=message_id,
        )
        return True

    def _defer_candidate(self, *, operation: str, dispatch_id: int) -> None:
        try:
            with self._session_factory() as session:
                ledger = HermesLedgerStore(session)
                if operation == "dispatch":
                    ledger.defer_dispatch_recovery(dispatch_id, now=self._clock())
                else:
                    ledger.defer_delivery_recovery(dispatch_id, now=self._clock())
        except Exception:
            logger.error(
                "Hermes recovery candidate deferral failed",
                extra={
                    "fields": {
                        "operation": operation,
                        "dispatch_id": dispatch_id,
                        "error_code": "recovery_candidate_deferral_failed",
                    }
                },
            )

    @staticmethod
    def _log_failure(
        *,
        operation: str,
        dispatch_id: int,
        message_id: int | None,
        error_code: str,
    ) -> None:
        logger.warning(
            "Hermes recovery item failed",
            extra={
                "fields": {
                    "operation": operation,
                    "dispatch_id": dispatch_id,
                    "message_id": message_id,
                    "error_code": error_code,
                }
            },
        )

    @staticmethod
    def _log_recovered(
        *,
        operation: str,
        dispatch_id: int,
        message_id: int | None,
    ) -> None:
        logger.info(
            "Hermes recovery item processed",
            extra={
                "fields": {
                    "operation": operation,
                    "dispatch_id": dispatch_id,
                    "message_id": message_id,
                }
            },
        )


def _safe_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if (
        isinstance(code, str)
        and 0 < len(code) <= 128
        and all(character.isalnum() or character in {"_", "-", "."} for character in code)
    ):
        return code
    return type(error).__name__
