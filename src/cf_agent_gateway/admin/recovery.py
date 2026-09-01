from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
from cf_agent_gateway.response.errors import (
    ResponseConflictError,
    ResponseValidationError,
)
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.response.store import ResponseStore
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecoveryAction,
    HermesDispatchRecoveryAudit,
    HermesDispatchStatus,
)


class DispatchRecoveryError(RuntimeError):
    code = "dispatch_recovery_error"


class DispatchRecoveryNotFoundError(DispatchRecoveryError):
    code = "dispatch_record_not_found"


class DispatchRecoveryStateConflictError(DispatchRecoveryError):
    code = "dispatch_recovery_state_conflict"


class DispatchRecoveryEvidenceError(DispatchRecoveryError):
    code = "dispatch_recovery_evidence_missing"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class DispatchRecoveryReferenceConflictError(DispatchRecoveryError):
    code = "dispatch_recovery_reference_conflict"


@dataclass(frozen=True, slots=True)
class DispatchRecoveryInspection:
    dispatch_record_id: int
    status: HermesDispatchStatus
    message_id: int
    ai_thread_id: str
    attempt_count: int
    last_error_code: str | None
    has_dispatch_response: bool
    has_hermes_response: bool
    has_delivery: bool
    blocks_following_dispatch: bool
    created_at: datetime
    updated_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    lease_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class DispatchRecoveryResult:
    dispatch_record_id: int
    action: HermesDispatchRecoveryAction
    status: HermesDispatchStatus
    audit_id: int
    idempotent: bool


class DispatchRecoveryStore:
    """Resolve uncertain V2 dispatches with CAS and an in-transaction audit record."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def inspect(self, dispatch_record_id: int) -> DispatchRecoveryInspection | None:
        record = self._session.get(HermesDispatchRecord, dispatch_record_id)
        if record is None:
            return None
        raw_response = self._raw_response(record.id)
        response = self._normalized_response(record.message_id)
        delivery_exists = response is not None and bool(
            self._session.scalar(
                select(exists().where(DeliveryOutboxRecord.response_id == response.response_id))
            )
        )
        return DispatchRecoveryInspection(
            dispatch_record_id=record.id,
            status=record.status,
            message_id=record.message_id,
            ai_thread_id=record.ai_thread_id,
            attempt_count=record.attempt_count,
            last_error_code=record.last_error_code,
            has_dispatch_response=raw_response is not None,
            has_hermes_response=response is not None,
            has_delivery=delivery_exists,
            blocks_following_dispatch=self._blocks_following_dispatch(record),
            created_at=record.created_at,
            updated_at=record.updated_at,
            claimed_at=record.claimed_at,
            completed_at=record.completed_at,
            lease_expires_at=record.lease_expires_at,
        )

    def retry_approved(
        self,
        dispatch_record_id: int,
        *,
        operator: str,
        reference: str,
        reason: str,
    ) -> DispatchRecoveryResult:
        return self._resolve(
            dispatch_record_id,
            action=HermesDispatchRecoveryAction.RETRY_APPROVED,
            after_status=HermesDispatchStatus.FAILED,
            operator=operator,
            reference=reference,
            reason=reason,
        )

    def mark_dead(
        self,
        dispatch_record_id: int,
        *,
        operator: str,
        reference: str,
        reason: str,
    ) -> DispatchRecoveryResult:
        return self._resolve(
            dispatch_record_id,
            action=HermesDispatchRecoveryAction.MARK_DEAD,
            after_status=HermesDispatchStatus.DEAD,
            operator=operator,
            reference=reference,
            reason=reason,
        )

    def confirm_success(
        self,
        dispatch_record_id: int,
        *,
        operator: str,
        reference: str,
        reason: str,
    ) -> DispatchRecoveryResult:
        return self._resolve(
            dispatch_record_id,
            action=HermesDispatchRecoveryAction.CONFIRM_SUCCESS,
            after_status=HermesDispatchStatus.SUCCESS,
            operator=operator,
            reference=reference,
            reason=reason,
            require_success_evidence=True,
        )

    def _resolve(
        self,
        dispatch_record_id: int,
        *,
        action: HermesDispatchRecoveryAction,
        after_status: HermesDispatchStatus,
        operator: str,
        reference: str,
        reason: str,
        require_success_evidence: bool = False,
    ) -> DispatchRecoveryResult:
        existing = self._audit_for(dispatch_record_id, action, reference)
        if existing is not None:
            return self._idempotent_result(
                existing,
                operator=operator,
                reason=reason,
            )

        record = self._session.get(HermesDispatchRecord, dispatch_record_id)
        if record is None:
            raise DispatchRecoveryNotFoundError()
        if record.status is not HermesDispatchStatus.UNCERTAIN:
            raise DispatchRecoveryStateConflictError()

        evidence_id: int | None = None
        if require_success_evidence:
            evidence_id = self._verified_success_evidence(record).id

        values: dict[str, object] = {
            "status": after_status,
            "manual_retry_approved": action is HermesDispatchRecoveryAction.RETRY_APPROVED,
            "updated_at": func.now(),
        }
        if after_status is HermesDispatchStatus.SUCCESS:
            values["last_error_code"] = None

        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == dispatch_record_id,
                HermesDispatchRecord.status == HermesDispatchStatus.UNCERTAIN,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        audit = HermesDispatchRecoveryAudit(
            dispatch_record_id=dispatch_record_id,
            action=action,
            operator=operator,
            reference=reference,
            reason=reason,
            before_status=HermesDispatchStatus.UNCERTAIN,
            after_status=after_status,
            before_error_code=record.last_error_code,
            evidence_dispatch_response_id=evidence_id,
        )
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                replay = self._audit_for(dispatch_record_id, action, reference)
                if replay is not None:
                    return self._idempotent_result(
                        replay,
                        operator=operator,
                        reason=reason,
                    )
                raise DispatchRecoveryStateConflictError()
            self._session.add(audit)
            self._session.commit()
        except DispatchRecoveryError:
            raise
        except IntegrityError:
            self._session.rollback()
            replay = self._audit_for(dispatch_record_id, action, reference)
            if replay is not None:
                return self._idempotent_result(
                    replay,
                    operator=operator,
                    reason=reason,
                )
            raise DispatchRecoveryStateConflictError() from None
        except Exception:
            self._session.rollback()
            raise

        self._session.refresh(audit)
        return DispatchRecoveryResult(
            dispatch_record_id=dispatch_record_id,
            action=action,
            status=after_status,
            audit_id=audit.id,
            idempotent=False,
        )

    def _verified_success_evidence(
        self,
        record: HermesDispatchRecord,
    ) -> HermesDispatchResponse:
        raw_response = self._raw_response(record.id)
        if raw_response is None:
            raise DispatchRecoveryEvidenceError("dispatch_response_missing")
        try:
            outcome = HermesDispatchResponseStore.to_outcome(raw_response, record)
        except Exception:
            raise DispatchRecoveryEvidenceError("dispatch_response_invalid") from None

        response = self._normalized_response(record.message_id)
        try:
            verified = ResponseStore(self._session).verify_generated(outcome)
        except (ResponseConflictError, ResponseValidationError):
            raise DispatchRecoveryEvidenceError("hermes_response_mismatch") from None
        if response is not None and (
            verified is None or verified.response_id != response.response_id
        ):
            raise DispatchRecoveryEvidenceError("hermes_response_mismatch")
        return raw_response

    def _blocks_following_dispatch(self, record: HermesDispatchRecord) -> bool:
        if record.status not in (
            HermesDispatchStatus.QUEUED,
            HermesDispatchStatus.RUNNING,
            HermesDispatchStatus.FAILED,
            HermesDispatchStatus.UNCERTAIN,
        ):
            return False
        current = aliased(HermesDispatchRecord)
        following = aliased(HermesDispatchRecord)
        is_later = or_(
            following.created_at > current.created_at,
            and_(
                following.created_at == current.created_at,
                following.id > current.id,
            ),
        )
        statement = select(
            exists().where(
                current.id == record.id,
                following.ai_thread_id == current.ai_thread_id,
                is_later,
                following.status.in_(
                    (
                        HermesDispatchStatus.QUEUED,
                        HermesDispatchStatus.RUNNING,
                        HermesDispatchStatus.FAILED,
                        HermesDispatchStatus.UNCERTAIN,
                    )
                ),
            )
        )
        return bool(self._session.scalar(statement))

    def _raw_response(self, dispatch_record_id: int) -> HermesDispatchResponse | None:
        return self._session.scalar(
            select(HermesDispatchResponse).where(
                HermesDispatchResponse.dispatch_record_id == dispatch_record_id
            )
        )

    def _normalized_response(self, message_id: int) -> ResponseRecord | None:
        return self._session.scalar(
            select(ResponseRecord).where(ResponseRecord.message_id == message_id)
        )

    def _audit_for(
        self,
        dispatch_record_id: int,
        action: HermesDispatchRecoveryAction,
        reference: str,
    ) -> HermesDispatchRecoveryAudit | None:
        return self._session.scalar(
            select(HermesDispatchRecoveryAudit).where(
                HermesDispatchRecoveryAudit.dispatch_record_id == dispatch_record_id,
                HermesDispatchRecoveryAudit.action == action,
                HermesDispatchRecoveryAudit.reference == reference,
            )
        )

    @staticmethod
    def _idempotent_result(
        audit: HermesDispatchRecoveryAudit,
        *,
        operator: str,
        reason: str,
    ) -> DispatchRecoveryResult:
        if audit.operator != operator or audit.reason != reason:
            raise DispatchRecoveryReferenceConflictError()
        return DispatchRecoveryResult(
            dispatch_record_id=audit.dispatch_record_id,
            action=audit.action,
            status=audit.after_status,
            audit_id=audit.id,
            idempotent=True,
        )
