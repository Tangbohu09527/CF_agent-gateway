from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import TIMEOUT_MAX
from uuid import uuid4

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.task.model.errors import (
    HermesDispatchAdmissionError,
    HermesDispatchStateConflictError,
    HermesDispatchTargetConflictError,
)
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread

HERMES_DISPATCH_IDEMPOTENCY_NAMESPACE = "v1:hermes-chat:message"
MAX_CLAIM_TOKEN_LENGTH = 255
MAX_ERROR_CODE_LENGTH = 128
DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_RETRY_LIMIT = 3
_EXHAUSTED_LEASE_ERROR = "dispatch_lease_expired_retry_exhausted"
_BLOCKING_STATUSES = (
    HermesDispatchStatus.QUEUED,
    HermesDispatchStatus.RUNNING,
    HermesDispatchStatus.FAILED,
    HermesDispatchStatus.UNCERTAIN,
)


@dataclass(frozen=True, slots=True)
class _DispatchTarget:
    message_id: int
    enterprise_identity_id: str
    workspace_id: str
    ai_thread_id: str


def build_hermes_dispatch_idempotency_key(message_id: int) -> str:
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("message_id must be a positive integer")
    return f"{HERMES_DISPATCH_IDEMPOTENCY_NAMESPACE}:{message_id}"


class HermesDispatchRecordStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, admission: AdmissionOutcome) -> tuple[HermesDispatchRecord, bool]:
        target = _dispatch_target(admission)
        idempotency_key = build_hermes_dispatch_idempotency_key(target.message_id)
        existing = self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return self._compatible_record_or_raise(existing, target), False

        # PostgreSQL serializes creation per thread; SQLite serializes the insert write.
        self._session.execute(
            select(AIThread.id).where(AIThread.id == target.ai_thread_id).with_for_update()
        )
        existing = self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return self._compatible_record_or_raise(existing, target), False

        record = HermesDispatchRecord(
            idempotency_key=idempotency_key,
            message_id=target.message_id,
            enterprise_identity_id=target.enterprise_identity_id,
            workspace_id=target.workspace_id,
            ai_thread_id=target.ai_thread_id,
        )
        self._session.add(record)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._compatible_record_or_raise(existing, target), False
            raise
        except Exception:
            self._session.rollback()
            raise
        return record, True

    def get(self, record_id: int) -> HermesDispatchRecord | None:
        return self._reload(record_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> HermesDispatchRecord | None:
        statement = (
            select(HermesDispatchRecord)
            .where(HermesDispatchRecord.idempotency_key == idempotency_key)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def claim_next(
        self,
        *,
        claim_token: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        now: datetime | None = None,
    ) -> HermesDispatchRecord | None:
        token = _validated_required_string(
            claim_token if claim_token is not None else str(uuid4()),
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        lease = _validated_lease_seconds(lease_seconds)
        retries = _validated_retry_limit(retry_limit)
        claimed_at = _validated_now(now)
        self._finalize_exhausted(now=claimed_at, retry_limit=retries)

        for _ in range(32):
            statement = (
                select(HermesDispatchRecord.id)
                .where(
                    _claimable_predicate(
                        HermesDispatchRecord,
                        now=claimed_at,
                        retry_limit=retries,
                    ),
                    _thread_head_predicate(HermesDispatchRecord),
                    _thread_idle_predicate(HermesDispatchRecord),
                )
                .order_by(HermesDispatchRecord.created_at, HermesDispatchRecord.id)
                .limit(1)
            )
            record_id = self._session.scalar(statement)
            if record_id is None:
                return None
            try:
                return self.claim(
                    record_id,
                    claim_token=token,
                    lease_seconds=lease,
                    retry_limit=retries,
                    now=claimed_at,
                )
            except HermesDispatchStateConflictError:
                continue
        return None

    def claim(
        self,
        record_id: int,
        *,
        claim_token: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        now: datetime | None = None,
    ) -> HermesDispatchRecord:
        token = _validated_required_string(
            claim_token,
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        lease = _validated_lease_seconds(lease_seconds)
        retries = _validated_retry_limit(retry_limit)
        claimed_at = _validated_now(now)
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record_id,
                _claimable_predicate(
                    HermesDispatchRecord,
                    now=claimed_at,
                    retry_limit=retries,
                ),
                _thread_head_predicate(HermesDispatchRecord),
                _thread_idle_predicate(HermesDispatchRecord),
            )
            .values(
                status=HermesDispatchStatus.RUNNING,
                attempt_count=HermesDispatchRecord.attempt_count + 1,
                claim_token=token,
                claimed_at=claimed_at,
                lease_expires_at=claimed_at + timedelta(seconds=lease),
                completed_at=None,
                last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return self._apply_transition(
            statement,
            record_id=record_id,
            expected_status=HermesDispatchStatus.QUEUED,
        )

    def renew_lease(
        self,
        record_id: int,
        *,
        claim_token: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> HermesDispatchRecord:
        token = _validated_required_string(
            claim_token,
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        lease = _validated_lease_seconds(lease_seconds)
        renewed_at = _validated_now(now)
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record_id,
                HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
                HermesDispatchRecord.claim_token == token,
                HermesDispatchRecord.lease_expires_at > renewed_at,
            )
            .values(
                lease_expires_at=renewed_at + timedelta(seconds=lease),
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return self._apply_transition(
            statement,
            record_id=record_id,
            expected_status=HermesDispatchStatus.RUNNING,
        )

    def mark_success(self, record_id: int, *, claim_token: str) -> HermesDispatchRecord:
        return self._complete(
            record_id,
            claim_token=claim_token,
            status=HermesDispatchStatus.SUCCESS,
            error_code=None,
        )

    def mark_failed(
        self,
        record_id: int,
        *,
        claim_token: str,
        error_code: str,
        retry_limit: int | None = None,
    ) -> HermesDispatchRecord:
        status: HermesDispatchStatus | object = HermesDispatchStatus.FAILED
        if retry_limit is not None:
            max_attempts = _validated_retry_limit(retry_limit) + 1
            status = case(
                (
                    HermesDispatchRecord.attempt_count >= max_attempts,
                    HermesDispatchStatus.DEAD,
                ),
                else_=HermesDispatchStatus.FAILED,
            )
        return self._complete(
            record_id,
            claim_token=claim_token,
            status=status,
            error_code=_validated_required_string(
                error_code,
                field_name="error_code",
                max_length=MAX_ERROR_CODE_LENGTH,
            ),
        )

    def mark_uncertain(
        self,
        record_id: int,
        *,
        claim_token: str,
        error_code: str,
    ) -> HermesDispatchRecord:
        return self._complete(
            record_id,
            claim_token=claim_token,
            status=HermesDispatchStatus.UNCERTAIN,
            error_code=_validated_required_string(
                error_code,
                field_name="error_code",
                max_length=MAX_ERROR_CODE_LENGTH,
            ),
        )

    def mark_dead(
        self,
        record_id: int,
        *,
        claim_token: str,
        error_code: str,
    ) -> HermesDispatchRecord:
        return self._complete(
            record_id,
            claim_token=claim_token,
            status=HermesDispatchStatus.DEAD,
            error_code=_validated_required_string(
                error_code,
                field_name="error_code",
                max_length=MAX_ERROR_CODE_LENGTH,
            ),
        )

    def _complete(
        self,
        record_id: int,
        *,
        claim_token: str,
        status: HermesDispatchStatus | object,
        error_code: str | None,
    ) -> HermesDispatchRecord:
        token = _validated_required_string(
            claim_token,
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record_id,
                HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
                HermesDispatchRecord.claim_token == token,
                HermesDispatchRecord.lease_expires_at > func.now(),
            )
            .values(
                status=status,
                claim_token=None,
                lease_expires_at=None,
                completed_at=func.now(),
                last_error_code=error_code,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return self._apply_transition(
            statement,
            record_id=record_id,
            expected_status=HermesDispatchStatus.RUNNING,
        )

    def _finalize_exhausted(self, *, now: datetime, retry_limit: int) -> None:
        max_attempts = retry_limit + 1
        failed_statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.status == HermesDispatchStatus.FAILED,
                HermesDispatchRecord.attempt_count >= max_attempts,
            )
            .values(status=HermesDispatchStatus.DEAD, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        expired_statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
                HermesDispatchRecord.lease_expires_at <= now,
                HermesDispatchRecord.attempt_count >= max_attempts,
            )
            .values(
                status=HermesDispatchStatus.DEAD,
                claim_token=None,
                lease_expires_at=None,
                completed_at=func.now(),
                last_error_code=_EXHAUSTED_LEASE_ERROR,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        try:
            self._session.execute(failed_statement)
            self._session.execute(expired_statement)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def _apply_transition(
        self,
        statement: object,
        *,
        record_id: int,
        expected_status: HermesDispatchStatus,
    ) -> HermesDispatchRecord:
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                raise HermesDispatchStateConflictError(
                    record_id=record_id,
                    expected_status=expected_status,
                )
            self._session.commit()
        except HermesDispatchStateConflictError:
            raise
        except IntegrityError:
            self._session.rollback()
            raise HermesDispatchStateConflictError(
                record_id=record_id,
                expected_status=expected_status,
            ) from None
        except Exception:
            self._session.rollback()
            raise

        record = self._reload(record_id)
        if record is None:
            raise HermesDispatchStateConflictError(
                record_id=record_id,
                expected_status=expected_status,
            )
        return record

    def _reload(self, record_id: int) -> HermesDispatchRecord | None:
        statement = (
            select(HermesDispatchRecord)
            .where(HermesDispatchRecord.id == record_id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    @staticmethod
    def _compatible_record_or_raise(
        record: HermesDispatchRecord,
        target: _DispatchTarget,
    ) -> HermesDispatchRecord:
        if (
            record.message_id != target.message_id
            or record.enterprise_identity_id != target.enterprise_identity_id
            or record.workspace_id != target.workspace_id
            or record.ai_thread_id != target.ai_thread_id
        ):
            raise HermesDispatchTargetConflictError(
                idempotency_key=record.idempotency_key,
                existing_record_id=record.id,
            )
        return record


def _claimable_predicate(
    record: type[HermesDispatchRecord],
    *,
    now: datetime,
    retry_limit: int,
) -> object:
    max_attempts = retry_limit + 1
    return or_(
        record.status == HermesDispatchStatus.QUEUED,
        and_(
            record.status == HermesDispatchStatus.FAILED,
            record.attempt_count < max_attempts,
        ),
        and_(
            record.status == HermesDispatchStatus.RUNNING,
            record.lease_expires_at <= now,
            record.attempt_count < max_attempts,
        ),
    )


def _thread_head_predicate(record: type[HermesDispatchRecord]) -> object:
    prior = aliased(HermesDispatchRecord)
    is_prior = or_(
        prior.created_at < record.created_at,
        and_(prior.created_at == record.created_at, prior.id < record.id),
    )
    return ~exists(
        select(prior.id).where(
            prior.ai_thread_id == record.ai_thread_id,
            is_prior,
            prior.status.in_(_BLOCKING_STATUSES),
        )
    )


def _thread_idle_predicate(record: type[HermesDispatchRecord]) -> object:
    running = aliased(HermesDispatchRecord)
    return ~exists(
        select(running.id).where(
            running.ai_thread_id == record.ai_thread_id,
            running.id != record.id,
            running.status == HermesDispatchStatus.RUNNING,
        )
    )


def _dispatch_target(admission: AdmissionOutcome) -> _DispatchTarget:
    if not admission.admitted or admission.reason is not AdmissionReason.ALLOWED:
        raise HermesDispatchAdmissionError("admission_not_allowed")
    if not admission.should_create_task:
        raise HermesDispatchAdmissionError("task_not_requested")
    if isinstance(admission.message_id, bool) or not isinstance(admission.message_id, int):
        raise HermesDispatchAdmissionError("invalid_message_id")
    if admission.message_id <= 0:
        raise HermesDispatchAdmissionError("invalid_message_id")

    return _DispatchTarget(
        message_id=admission.message_id,
        enterprise_identity_id=_required_target_id(
            admission.enterprise_identity_id,
            reason="enterprise_identity_missing",
        ),
        workspace_id=_required_target_id(
            admission.workspace_id,
            reason="workspace_missing",
        ),
        ai_thread_id=_required_target_id(
            admission.ai_thread_id,
            reason="ai_thread_missing",
        ),
    )


def _required_target_id(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HermesDispatchAdmissionError(reason)
    return value


def _validated_required_string(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{field_name} is too long")
    return value


def _validated_lease_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("lease_seconds must be a finite positive number")
    lease_seconds = float(value)
    if not math.isfinite(lease_seconds) or lease_seconds <= 0 or lease_seconds > TIMEOUT_MAX:
        raise ValueError("lease_seconds must be a finite positive number")
    return lease_seconds


def _validated_retry_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("retry_limit must be a non-negative integer")
    return value


def _validated_now(value: datetime | None) -> datetime:
    now = value if value is not None else datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now
