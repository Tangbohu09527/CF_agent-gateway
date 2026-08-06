from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.task.model.errors import (
    HermesDispatchAdmissionError,
    HermesDispatchStateConflictError,
    HermesDispatchTargetConflictError,
)
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus

HERMES_DISPATCH_IDEMPOTENCY_NAMESPACE = "v1:hermes-chat:message"
MAX_CLAIM_TOKEN_LENGTH = 255
MAX_ERROR_CODE_LENGTH = 128


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

    def claim(self, record_id: int, *, claim_token: str) -> HermesDispatchRecord:
        claim_token = _validated_required_string(
            claim_token,
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record_id,
                HermesDispatchRecord.status == HermesDispatchStatus.QUEUED,
            )
            .values(
                status=HermesDispatchStatus.RUNNING,
                attempt_count=HermesDispatchRecord.attempt_count + 1,
                claim_token=claim_token,
                claimed_at=func.now(),
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
    ) -> HermesDispatchRecord:
        return self._complete(
            record_id,
            claim_token=claim_token,
            status=HermesDispatchStatus.FAILED,
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

    def _complete(
        self,
        record_id: int,
        *,
        claim_token: str,
        status: HermesDispatchStatus,
        error_code: str | None,
    ) -> HermesDispatchRecord:
        claim_token = _validated_required_string(
            claim_token,
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record_id,
                HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
                HermesDispatchRecord.claim_token == claim_token,
            )
            .values(
                status=status,
                claim_token=None,
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
