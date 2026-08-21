from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.hermes.models import (
    HermesDeliveryRecord,
    HermesDispatchRecord,
    HermesOperationStatus,
)

DEFAULT_OPERATION_LEASE = timedelta(seconds=120)
DEFAULT_RECOVERY_BATCH_SIZE = 100
MAX_RECOVERY_BATCH_SIZE = 1000


@dataclass(frozen=True, slots=True)
class DispatchLeaseClaim:
    record: HermesDispatchRecord
    acquired: bool


@dataclass(frozen=True, slots=True)
class DeliveryLeaseClaim:
    record: HermesDeliveryRecord
    acquired: bool


class HermesLedgerStore:
    """Persist idempotency and retry state around Hermes and delivery side effects."""

    def __init__(
        self,
        session: Session,
        *,
        lease_duration: timedelta = DEFAULT_OPERATION_LEASE,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._session = session
        self._lease_duration = lease_duration

    def get_dispatch(self, message_id: int) -> HermesDispatchRecord | None:
        statement = (
            select(HermesDispatchRecord)
            .where(HermesDispatchRecord.message_id == message_id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def list_due_dispatch_ids(
        self,
        *,
        now: datetime | None = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
    ) -> list[int]:
        """Snapshot failed or lease-expired dispatch rows without claiming them."""

        due_at = now or datetime.now(UTC)
        statement = (
            select(HermesDispatchRecord.id)
            .where(
                or_(
                    HermesDispatchRecord.status == HermesOperationStatus.FAILED,
                    and_(
                        HermesDispatchRecord.status == HermesOperationStatus.IN_PROGRESS,
                        HermesDispatchRecord.lease_expires_at <= due_at,
                    ),
                )
            )
            .order_by(HermesDispatchRecord.updated_at, HermesDispatchRecord.id)
            .limit(_bounded_recovery_limit(limit))
        )
        return list(self._session.scalars(statement))

    def get_dispatch_by_id(self, dispatch_id: int) -> HermesDispatchRecord | None:
        return self._session.get(HermesDispatchRecord, dispatch_id)

    def defer_dispatch_recovery(
        self,
        dispatch_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Move a failed recovery candidate behind older queued work."""

        result = self._session.execute(
            update(HermesDispatchRecord)
            .where(HermesDispatchRecord.id == dispatch_id)
            .values(updated_at=now or datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        return self._finish_recovery_deferral(matched=result.rowcount == 1)

    def defer_delivery_recovery(
        self,
        dispatch_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Move a failed delivery candidate behind older queued work."""

        dispatch = self.get_dispatch_by_id(dispatch_id)
        if dispatch is None:
            self._session.rollback()
            return False
        deferred_at = now or datetime.now(UTC)
        result = self._session.execute(
            update(HermesDeliveryRecord)
            .where(HermesDeliveryRecord.message_id == dispatch.message_id)
            .values(updated_at=deferred_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            result = self._session.execute(
                update(HermesDispatchRecord)
                .where(HermesDispatchRecord.id == dispatch_id)
                .values(updated_at=deferred_at)
                .execution_options(synchronize_session=False)
            )
        return self._finish_recovery_deferral(matched=result.rowcount == 1)

    def claim_dispatch(
        self,
        *,
        message_id: int,
        workspace_id: str,
        ai_thread_id: str,
        requested_hermes_thread_id: str,
        now: datetime | None = None,
    ) -> DispatchLeaseClaim:
        claimed_at = now or datetime.now(UTC)
        lease_token = str(uuid4())
        existing = self.get_dispatch(message_id)
        if existing is None:
            record = HermesDispatchRecord(
                message_id=message_id,
                workspace_id=workspace_id,
                ai_thread_id=ai_thread_id,
                status=HermesOperationStatus.IN_PROGRESS,
                attempt_count=1,
                lease_token=lease_token,
                lease_expires_at=claimed_at + self._lease_duration,
                requested_hermes_thread_id=requested_hermes_thread_id,
            )
            self._session.add(record)
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                existing = self.get_dispatch(message_id)
                if existing is None:
                    raise
            else:
                return DispatchLeaseClaim(record=record, acquired=True)

        if not self._dispatch_target_matches(existing, workspace_id, ai_thread_id):
            return DispatchLeaseClaim(record=existing, acquired=False)
        if existing.status is HermesOperationStatus.SUCCEEDED:
            return DispatchLeaseClaim(record=existing, acquired=False)

        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == existing.id,
                or_(
                    HermesDispatchRecord.status == HermesOperationStatus.FAILED,
                    and_(
                        HermesDispatchRecord.status == HermesOperationStatus.IN_PROGRESS,
                        HermesDispatchRecord.lease_expires_at <= claimed_at,
                    ),
                ),
            )
            .values(
                status=HermesOperationStatus.IN_PROGRESS,
                attempt_count=HermesDispatchRecord.attempt_count + 1,
                lease_token=lease_token,
                lease_expires_at=claimed_at + self._lease_duration,
                requested_hermes_thread_id=requested_hermes_thread_id,
                result_hermes_thread_id=None,
                assistant_content=None,
                last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        acquired = result.rowcount == 1
        if acquired:
            self._session.commit()
        else:
            self._session.rollback()
        record = self.get_dispatch(message_id)
        if record is None:
            raise RuntimeError("dispatch record disappeared after claim")
        return DispatchLeaseClaim(record=record, acquired=acquired)

    def complete_dispatch(
        self,
        record: HermesDispatchRecord,
        *,
        lease_token: str,
        result_hermes_thread_id: str,
        assistant_content: str,
    ) -> bool:
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record.id,
                HermesDispatchRecord.status == HermesOperationStatus.IN_PROGRESS,
                HermesDispatchRecord.lease_token == lease_token,
            )
            .values(
                status=HermesOperationStatus.SUCCEEDED,
                lease_token=None,
                lease_expires_at=None,
                result_hermes_thread_id=result_hermes_thread_id,
                assistant_content=assistant_content,
                last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        return self._finish_cas(record, matched=result.rowcount == 1)

    def fail_dispatch(
        self,
        record: HermesDispatchRecord,
        *,
        lease_token: str,
        error_code: str,
    ) -> bool:
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record.id,
                HermesDispatchRecord.status == HermesOperationStatus.IN_PROGRESS,
                HermesDispatchRecord.lease_token == lease_token,
            )
            .values(
                status=HermesOperationStatus.FAILED,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=error_code[:128],
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        return self._finish_cas(record, matched=result.rowcount == 1)

    def note_ambiguous_dispatch(
        self,
        record: HermesDispatchRecord,
        *,
        lease_token: str,
        error_code: str,
    ) -> bool:
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == record.id,
                HermesDispatchRecord.status == HermesOperationStatus.IN_PROGRESS,
                HermesDispatchRecord.lease_token == lease_token,
            )
            .values(
                last_error_code=error_code[:128],
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        return self._finish_cas(record, matched=result.rowcount == 1)

    def get_delivery(self, message_id: int) -> HermesDeliveryRecord | None:
        statement = (
            select(HermesDeliveryRecord)
            .where(HermesDeliveryRecord.message_id == message_id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def list_due_delivery_ids(
        self,
        *,
        now: datetime | None = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
    ) -> list[int]:
        """Snapshot successful dispatches whose delivery is missing or retryable."""

        due_at = now or datetime.now(UTC)
        statement = (
            select(HermesDispatchRecord.id)
            .outerjoin(
                HermesDeliveryRecord,
                HermesDeliveryRecord.message_id == HermesDispatchRecord.message_id,
            )
            .where(
                HermesDispatchRecord.status == HermesOperationStatus.SUCCEEDED,
                or_(
                    HermesDeliveryRecord.id.is_(None),
                    HermesDeliveryRecord.status == HermesOperationStatus.FAILED,
                    and_(
                        HermesDeliveryRecord.status == HermesOperationStatus.IN_PROGRESS,
                        HermesDeliveryRecord.lease_expires_at <= due_at,
                    ),
                ),
            )
            .order_by(
                func.coalesce(
                    HermesDeliveryRecord.updated_at,
                    HermesDispatchRecord.updated_at,
                ),
                HermesDispatchRecord.id,
            )
            .limit(_bounded_recovery_limit(limit))
        )
        return list(self._session.scalars(statement))

    def claim_delivery(
        self,
        *,
        message_id: int,
        ai_thread_id: str,
        conversation_id: str,
        content_sha256: str,
        now: datetime | None = None,
    ) -> DeliveryLeaseClaim:
        claimed_at = now or datetime.now(UTC)
        lease_token = str(uuid4())
        existing = self.get_delivery(message_id)
        if existing is None:
            record = HermesDeliveryRecord(
                message_id=message_id,
                ai_thread_id=ai_thread_id,
                conversation_id=conversation_id,
                content_sha256=content_sha256,
                status=HermesOperationStatus.IN_PROGRESS,
                attempt_count=1,
                lease_token=lease_token,
                lease_expires_at=claimed_at + self._lease_duration,
            )
            self._session.add(record)
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                existing = self.get_delivery(message_id)
                if existing is None:
                    raise
            else:
                return DeliveryLeaseClaim(record=record, acquired=True)

        if not self._delivery_target_matches(
            existing,
            ai_thread_id=ai_thread_id,
            conversation_id=conversation_id,
            content_sha256=content_sha256,
        ):
            return DeliveryLeaseClaim(record=existing, acquired=False)
        if existing.status is HermesOperationStatus.SUCCEEDED:
            return DeliveryLeaseClaim(record=existing, acquired=False)

        statement = (
            update(HermesDeliveryRecord)
            .where(
                HermesDeliveryRecord.id == existing.id,
                or_(
                    HermesDeliveryRecord.status == HermesOperationStatus.FAILED,
                    and_(
                        HermesDeliveryRecord.status == HermesOperationStatus.IN_PROGRESS,
                        HermesDeliveryRecord.lease_expires_at <= claimed_at,
                    ),
                ),
            )
            .values(
                status=HermesOperationStatus.IN_PROGRESS,
                attempt_count=HermesDeliveryRecord.attempt_count + 1,
                lease_token=lease_token,
                lease_expires_at=claimed_at + self._lease_duration,
                last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        acquired = result.rowcount == 1
        if acquired:
            self._session.commit()
        else:
            self._session.rollback()
        record = self.get_delivery(message_id)
        if record is None:
            raise RuntimeError("delivery record disappeared after claim")
        return DeliveryLeaseClaim(record=record, acquired=acquired)

    def complete_delivery(
        self,
        record: HermesDeliveryRecord,
        *,
        lease_token: str,
    ) -> bool:
        statement = (
            update(HermesDeliveryRecord)
            .where(
                HermesDeliveryRecord.id == record.id,
                HermesDeliveryRecord.status == HermesOperationStatus.IN_PROGRESS,
                HermesDeliveryRecord.lease_token == lease_token,
            )
            .values(
                status=HermesOperationStatus.SUCCEEDED,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        return self._finish_cas(record, matched=result.rowcount == 1)

    def fail_delivery(
        self,
        record: HermesDeliveryRecord,
        *,
        lease_token: str,
        error_code: str,
    ) -> bool:
        statement = (
            update(HermesDeliveryRecord)
            .where(
                HermesDeliveryRecord.id == record.id,
                HermesDeliveryRecord.status == HermesOperationStatus.IN_PROGRESS,
                HermesDeliveryRecord.lease_token == lease_token,
            )
            .values(
                status=HermesOperationStatus.FAILED,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=error_code[:128],
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        return self._finish_cas(record, matched=result.rowcount == 1)

    def note_ambiguous_delivery(
        self,
        record: HermesDeliveryRecord,
        *,
        lease_token: str,
        error_code: str,
    ) -> bool:
        statement = (
            update(HermesDeliveryRecord)
            .where(
                HermesDeliveryRecord.id == record.id,
                HermesDeliveryRecord.status == HermesOperationStatus.IN_PROGRESS,
                HermesDeliveryRecord.lease_token == lease_token,
            )
            .values(
                last_error_code=error_code[:128],
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        return self._finish_cas(record, matched=result.rowcount == 1)

    def _finish_cas(
        self,
        record: HermesDispatchRecord | HermesDeliveryRecord,
        *,
        matched: bool,
    ) -> bool:
        if matched:
            self._session.commit()
        else:
            self._session.rollback()
        self._session.refresh(record)
        return matched

    def _finish_recovery_deferral(self, *, matched: bool) -> bool:
        if matched:
            self._session.commit()
        else:
            self._session.rollback()
        return matched

    @staticmethod
    def _dispatch_target_matches(
        record: HermesDispatchRecord,
        workspace_id: str,
        ai_thread_id: str,
    ) -> bool:
        return record.workspace_id == workspace_id and record.ai_thread_id == ai_thread_id

    @staticmethod
    def _delivery_target_matches(
        record: HermesDeliveryRecord,
        *,
        ai_thread_id: str,
        conversation_id: str,
        content_sha256: str,
    ) -> bool:
        return (
            record.ai_thread_id == ai_thread_id
            and record.conversation_id == conversation_id
            and record.content_sha256 == content_sha256
        )


def _bounded_recovery_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return min(limit, MAX_RECOVERY_BATCH_SIZE)
