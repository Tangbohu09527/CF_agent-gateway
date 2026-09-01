from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.delivery.errors import DeliveryStateConflictError
from cf_agent_gateway.delivery.models import (
    DeliveryAttempt,
    DeliveryAttemptStatus,
    DeliveryOutboxRecord,
    DeliveryReceipt,
    DeliveryStatus,
)
from cf_agent_gateway.response.models import ResponsePartRecord, ResponseRecord, ResponseStatus

MAX_CLAIM_TOKEN_LENGTH = 255
MAX_ERROR_CODE_LENGTH = 128


class DeliveryOutboxStore:
    """CAS-backed delivery queue with a durable per-part receipt ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, delivery_id: int) -> DeliveryOutboxRecord | None:
        statement = (
            select(DeliveryOutboxRecord)
            .where(DeliveryOutboxRecord.id == delivery_id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def get_for_response(self, response_id: str) -> DeliveryOutboxRecord | None:
        statement = (
            select(DeliveryOutboxRecord)
            .where(DeliveryOutboxRecord.response_id == response_id)
            .order_by(DeliveryOutboxRecord.id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def get_part(self, response_id: str, ordinal: int) -> ResponsePartRecord | None:
        return self._session.get(ResponsePartRecord, (response_id, ordinal))

    def get_receipt(self, delivery_id: int, part_ordinal: int) -> DeliveryReceipt | None:
        return self._session.get(DeliveryReceipt, (delivery_id, part_ordinal))

    def list_attempts(self, delivery_id: int) -> list[DeliveryAttempt]:
        statement = (
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery_id)
            .order_by(DeliveryAttempt.id)
        )
        return list(self._session.scalars(statement))

    def list_receipts(self, delivery_id: int) -> list[DeliveryReceipt]:
        statement = (
            select(DeliveryReceipt)
            .where(DeliveryReceipt.delivery_id == delivery_id)
            .order_by(DeliveryReceipt.part_ordinal)
        )
        return list(self._session.scalars(statement))

    def claim_next(
        self,
        *,
        channel: str,
        claim_token: str,
        now: datetime | None = None,
    ) -> DeliveryOutboxRecord | None:
        channel = _required_string(
            channel,
            field_name="channel",
            max_length=64,
        )
        claim_token = _required_string(
            claim_token,
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        current_time = _aware_utc(now if now is not None else datetime.now(UTC), "now")
        for _ in range(8):
            delivery_id = self._session.scalar(
                select(DeliveryOutboxRecord.id)
                .where(
                    DeliveryOutboxRecord.status == DeliveryStatus.QUEUED,
                    DeliveryOutboxRecord.channel == channel,
                    DeliveryOutboxRecord.available_at <= current_time,
                )
                .order_by(
                    DeliveryOutboxRecord.available_at,
                    DeliveryOutboxRecord.created_at,
                    DeliveryOutboxRecord.id,
                )
                .limit(1)
            )
            if delivery_id is None:
                self._session.rollback()
                return None

            statement = (
                update(DeliveryOutboxRecord)
                .where(
                    DeliveryOutboxRecord.id == delivery_id,
                    DeliveryOutboxRecord.status == DeliveryStatus.QUEUED,
                    DeliveryOutboxRecord.channel == channel,
                    DeliveryOutboxRecord.available_at <= current_time,
                )
                .values(
                    status=DeliveryStatus.DELIVERING,
                    attempt_count=DeliveryOutboxRecord.attempt_count + 1,
                    claim_token=claim_token,
                    claimed_at=current_time,
                    completed_at=None,
                    last_error_code=None,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            try:
                result = self._session.execute(statement)
                if result.rowcount != 1:
                    self._session.rollback()
                    continue
                response_id = self._session.scalar(
                    select(DeliveryOutboxRecord.response_id).where(
                        DeliveryOutboxRecord.id == delivery_id
                    )
                )
                response_result = self._session.execute(
                    update(ResponseRecord)
                    .where(
                        ResponseRecord.response_id == response_id,
                        ResponseRecord.status.in_(
                            (ResponseStatus.GENERATED, ResponseStatus.DELIVERING)
                        ),
                    )
                    .values(
                        status=ResponseStatus.DELIVERING,
                        delivering_at=func.coalesce(
                            ResponseRecord.delivering_at,
                            current_time,
                        ),
                        last_error_code=None,
                        updated_at=current_time,
                    )
                    .execution_options(synchronize_session=False)
                )
                if response_result.rowcount != 1:
                    self._session.rollback()
                    raise DeliveryStateConflictError(
                        delivery_id=delivery_id, expected_status=ResponseStatus.GENERATED.value
                    )
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            return self._required(delivery_id)
        return None

    def recover_stale_claims(
        self,
        *,
        channel: str,
        stale_before: datetime,
        now: datetime | None = None,
    ) -> list[DeliveryOutboxRecord]:
        """Requeue stale pre-send claims and quarantine stale in-flight attempts."""

        channel = _required_string(channel, field_name="channel", max_length=64)
        cutoff = _aware_utc(stale_before, "stale_before")
        current_time = _aware_utc(now if now is not None else datetime.now(UTC), "now")
        if cutoff > current_time:
            raise ValueError("stale_before must not be later than now")

        delivery_ids = list(
            self._session.scalars(
                select(DeliveryOutboxRecord.id)
                .where(
                    DeliveryOutboxRecord.channel == channel,
                    DeliveryOutboxRecord.status == DeliveryStatus.DELIVERING,
                    DeliveryOutboxRecord.claim_token.is_not(None),
                    DeliveryOutboxRecord.claimed_at.is_not(None),
                    DeliveryOutboxRecord.claimed_at <= cutoff,
                )
                .order_by(DeliveryOutboxRecord.claimed_at, DeliveryOutboxRecord.id)
            )
        )
        recovered: list[DeliveryOutboxRecord] = []
        for delivery_id in delivery_ids:
            delivery = self.get(delivery_id)
            if (
                delivery is None
                or delivery.status is not DeliveryStatus.DELIVERING
                or delivery.claim_token is None
            ):
                continue
            claim_token = delivery.claim_token
            active_attempt = self._active_attempt(
                delivery.id,
                delivery.next_part_ordinal,
            )
            has_in_flight_attempt = active_attempt is not None
            error_code = "delivery_claim_expired"
            delivery_values: dict[str, object] = {
                "status": (
                    DeliveryStatus.UNCERTAIN if has_in_flight_attempt else DeliveryStatus.QUEUED
                ),
                "available_at": current_time,
                "claim_token": None,
                "claimed_at": None,
                "completed_at": current_time if has_in_flight_attempt else None,
                "last_error_code": error_code if has_in_flight_attempt else None,
                "updated_at": current_time,
            }
            try:
                delivery_result = self._session.execute(
                    update(DeliveryOutboxRecord)
                    .where(
                        DeliveryOutboxRecord.id == delivery.id,
                        DeliveryOutboxRecord.channel == channel,
                        DeliveryOutboxRecord.status == DeliveryStatus.DELIVERING,
                        DeliveryOutboxRecord.claim_token == claim_token,
                        DeliveryOutboxRecord.claimed_at <= cutoff,
                    )
                    .values(**delivery_values)
                    .execution_options(synchronize_session=False)
                )
                if delivery_result.rowcount != 1:
                    self._session.rollback()
                    continue

                if active_attempt is not None:
                    attempt_result = self._session.execute(
                        update(DeliveryAttempt)
                        .where(
                            DeliveryAttempt.id == active_attempt.id,
                            DeliveryAttempt.status == DeliveryAttemptStatus.DELIVERING,
                        )
                        .values(
                            status=DeliveryAttemptStatus.UNCERTAIN,
                            error_code=error_code,
                            completed_at=current_time,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if attempt_result.rowcount != 1:
                        self._session.rollback()
                        continue
                    response_status = ResponseStatus.UNCERTAIN
                    response_values: dict[str, object] = {
                        "status": response_status,
                        "uncertain_at": current_time,
                        "last_error_code": error_code,
                        "updated_at": current_time,
                    }
                else:
                    response_status = (
                        ResponseStatus.GENERATED
                        if delivery.next_part_ordinal == 0
                        else ResponseStatus.DELIVERING
                    )
                    response_values = {
                        "status": response_status,
                        "last_error_code": None,
                        "updated_at": current_time,
                    }
                response_result = self._session.execute(
                    update(ResponseRecord)
                    .where(
                        ResponseRecord.response_id == delivery.response_id,
                        ResponseRecord.status == ResponseStatus.DELIVERING,
                    )
                    .values(**response_values)
                    .execution_options(synchronize_session=False)
                )
                if response_result.rowcount != 1:
                    self._session.rollback()
                    raise DeliveryStateConflictError(
                        delivery_id=delivery.id,
                        expected_status=ResponseStatus.DELIVERING.value,
                    )
                self._session.commit()
            except DeliveryStateConflictError:
                raise
            except Exception:
                self._session.rollback()
                raise
            recovered.append(self._required(delivery.id))
        return recovered

    def start_attempt(
        self,
        delivery_id: int,
        *,
        claim_token: str,
        part_ordinal: int,
    ) -> DeliveryAttempt:
        delivery = self._claimed(delivery_id, claim_token=claim_token)
        if part_ordinal != delivery.next_part_ordinal:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status=f"next_part_{delivery.next_part_ordinal}",
            )
        existing_attempt = self._active_attempt(delivery_id, part_ordinal)
        if existing_attempt is not None:
            return existing_attempt
        attempt_number = (
            self._session.scalar(
                select(func.coalesce(func.max(DeliveryAttempt.attempt_number), 0)).where(
                    DeliveryAttempt.delivery_id == delivery_id,
                    DeliveryAttempt.part_ordinal == part_ordinal,
                )
            )
            or 0
        ) + 1
        attempt = DeliveryAttempt(
            delivery_id=delivery_id,
            part_ordinal=part_ordinal,
            attempt_number=attempt_number,
            provider_idempotency_key=_provider_idempotency_key(
                delivery.idempotency_key,
                part_ordinal,
            ),
            status=DeliveryAttemptStatus.DELIVERING,
        )
        self._session.add(attempt)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing_attempt = self._active_attempt(delivery_id, part_ordinal)
            if existing_attempt is not None:
                return existing_attempt
            raise
        except Exception:
            self._session.rollback()
            raise
        return attempt

    def mark_part_delivered(
        self,
        delivery_id: int,
        *,
        claim_token: str,
        attempt_id: int,
        part_ordinal: int,
        receipt_payload: dict[str, object],
        provider_message_id: str | None,
        completed_at: datetime | None = None,
    ) -> DeliveryOutboxRecord:
        delivery = self._claimed(delivery_id, claim_token=claim_token)
        if part_ordinal != delivery.next_part_ordinal:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status=f"next_part_{delivery.next_part_ordinal}",
            )
        attempt = self._required_attempt(attempt_id, delivery_id, part_ordinal)
        if attempt.status is not DeliveryAttemptStatus.DELIVERING:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status=DeliveryAttemptStatus.DELIVERING.value,
            )
        if self.get_receipt(delivery_id, part_ordinal) is not None:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status="receipt_missing",
            )

        now = completed_at if completed_at is not None else datetime.now(UTC)
        response = self._session.get(ResponseRecord, delivery.response_id)
        if response is None:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status="response_present",
            )
        final_part = part_ordinal + 1 == response.part_count
        receipt = DeliveryReceipt(
            delivery_id=delivery_id,
            part_ordinal=part_ordinal,
            response_id=delivery.response_id,
            attempt_id=attempt.id,
            provider_message_id=provider_message_id,
            receipt_payload=receipt_payload,
            delivered_at=now,
        )
        self._session.add(receipt)
        attempt.status = DeliveryAttemptStatus.DELIVERED
        attempt.provider_message_id = provider_message_id
        attempt.receipt_payload = receipt_payload
        attempt.completed_at = now
        values: dict[str, object] = {
            "next_part_ordinal": part_ordinal + 1,
            "updated_at": now,
        }
        if final_part:
            values.update(
                status=DeliveryStatus.DELIVERED,
                claim_token=None,
                claimed_at=None,
                completed_at=now,
                last_error_code=None,
            )
        statement = (
            update(DeliveryOutboxRecord)
            .where(
                DeliveryOutboxRecord.id == delivery_id,
                DeliveryOutboxRecord.status == DeliveryStatus.DELIVERING,
                DeliveryOutboxRecord.claim_token == claim_token,
                DeliveryOutboxRecord.next_part_ordinal == part_ordinal,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                raise DeliveryStateConflictError(
                    delivery_id=delivery_id,
                    expected_status=DeliveryStatus.DELIVERING.value,
                )
            if final_part:
                response_result = self._session.execute(
                    update(ResponseRecord)
                    .where(
                        ResponseRecord.response_id == delivery.response_id,
                        ResponseRecord.status == ResponseStatus.DELIVERING,
                    )
                    .values(
                        status=ResponseStatus.DELIVERED,
                        delivered_at=now,
                        last_error_code=None,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if response_result.rowcount != 1:
                    self._session.rollback()
                    raise DeliveryStateConflictError(
                        delivery_id=delivery_id,
                        expected_status=ResponseStatus.DELIVERING.value,
                    )
            self._session.commit()
        except DeliveryStateConflictError:
            raise
        except Exception:
            self._session.rollback()
            raise
        return self._required(delivery_id)

    def mark_attempt_retryable(
        self,
        delivery_id: int,
        *,
        claim_token: str,
        attempt_id: int,
        error_code: str,
        available_at: datetime,
    ) -> DeliveryOutboxRecord:
        return self._finish_attempt(
            delivery_id,
            claim_token=claim_token,
            attempt_id=attempt_id,
            attempt_status=DeliveryAttemptStatus.FAILED,
            delivery_status=DeliveryStatus.QUEUED,
            response_status=None,
            error_code=error_code,
            available_at=available_at,
        )

    def mark_attempt_failed(
        self,
        delivery_id: int,
        *,
        claim_token: str,
        attempt_id: int,
        error_code: str,
    ) -> DeliveryOutboxRecord:
        return self._finish_attempt(
            delivery_id,
            claim_token=claim_token,
            attempt_id=attempt_id,
            attempt_status=DeliveryAttemptStatus.FAILED,
            delivery_status=DeliveryStatus.FAILED,
            response_status=ResponseStatus.FAILED,
            error_code=error_code,
        )

    def mark_attempt_uncertain(
        self,
        delivery_id: int,
        *,
        claim_token: str,
        attempt_id: int,
        error_code: str,
    ) -> DeliveryOutboxRecord:
        return self._finish_attempt(
            delivery_id,
            claim_token=claim_token,
            attempt_id=attempt_id,
            attempt_status=DeliveryAttemptStatus.UNCERTAIN,
            delivery_status=DeliveryStatus.UNCERTAIN,
            response_status=ResponseStatus.UNCERTAIN,
            error_code=error_code,
        )

    def retry(
        self,
        delivery_id: int,
        *,
        allow_uncertain: bool = False,
        available_at: datetime | None = None,
    ) -> DeliveryOutboxRecord:
        delivery = self._required(delivery_id)
        allowed = {DeliveryStatus.FAILED}
        if allow_uncertain:
            allowed.add(DeliveryStatus.UNCERTAIN)
        if delivery.status not in allowed:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status="failed_or_explicitly_allowed_uncertain",
            )
        now = datetime.now(UTC)
        scheduled_at = _aware_utc(available_at, "available_at") if available_at is not None else now
        response_status = (
            ResponseStatus.DELIVERING
            if delivery.next_part_ordinal > 0
            else ResponseStatus.GENERATED
        )
        expected_response_status = (
            ResponseStatus.UNCERTAIN
            if delivery.status is DeliveryStatus.UNCERTAIN
            else ResponseStatus.FAILED
        )
        try:
            delivery_result = self._session.execute(
                update(DeliveryOutboxRecord)
                .where(
                    DeliveryOutboxRecord.id == delivery_id,
                    DeliveryOutboxRecord.status == delivery.status,
                )
                .values(
                    status=DeliveryStatus.QUEUED,
                    available_at=scheduled_at,
                    claim_token=None,
                    claimed_at=None,
                    completed_at=None,
                    last_error_code=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if delivery_result.rowcount != 1:
                self._session.rollback()
                raise DeliveryStateConflictError(
                    delivery_id=delivery_id,
                    expected_status=delivery.status.value,
                )
            response_result = self._session.execute(
                update(ResponseRecord)
                .where(
                    ResponseRecord.response_id == delivery.response_id,
                    ResponseRecord.status == expected_response_status,
                )
                .values(
                    status=response_status,
                    failed_at=None,
                    uncertain_at=None,
                    last_error_code=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if response_result.rowcount != 1:
                self._session.rollback()
                raise DeliveryStateConflictError(
                    delivery_id=delivery_id,
                    expected_status=expected_response_status.value,
                )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return self._required(delivery_id)

    def _finish_attempt(
        self,
        delivery_id: int,
        *,
        claim_token: str,
        attempt_id: int,
        attempt_status: DeliveryAttemptStatus,
        delivery_status: DeliveryStatus,
        response_status: ResponseStatus | None,
        error_code: str,
        available_at: datetime | None = None,
    ) -> DeliveryOutboxRecord:
        delivery = self._claimed(delivery_id, claim_token=claim_token)
        attempt = self._required_attempt(
            attempt_id,
            delivery_id,
            delivery.next_part_ordinal,
        )
        if attempt.status is not DeliveryAttemptStatus.DELIVERING:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status=DeliveryAttemptStatus.DELIVERING.value,
            )
        normalized_error = _required_string(
            error_code,
            field_name="error_code",
            max_length=MAX_ERROR_CODE_LENGTH,
        )
        now = datetime.now(UTC)
        attempt.status = attempt_status
        attempt.error_code = normalized_error
        attempt.completed_at = now
        values: dict[str, object] = {
            "status": delivery_status,
            "claim_token": None,
            "claimed_at": None,
            "completed_at": None if delivery_status is DeliveryStatus.QUEUED else now,
            "last_error_code": normalized_error,
            "updated_at": now,
        }
        if available_at is not None:
            values["available_at"] = _aware_utc(available_at, "available_at")
        try:
            result = self._session.execute(
                update(DeliveryOutboxRecord)
                .where(
                    DeliveryOutboxRecord.id == delivery_id,
                    DeliveryOutboxRecord.status == DeliveryStatus.DELIVERING,
                    DeliveryOutboxRecord.claim_token == claim_token,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                self._session.rollback()
                raise DeliveryStateConflictError(
                    delivery_id=delivery_id,
                    expected_status=DeliveryStatus.DELIVERING.value,
                )
            if response_status is not None:
                response_values: dict[str, object] = {
                    "status": response_status,
                    "last_error_code": normalized_error,
                    "updated_at": now,
                }
                if response_status is ResponseStatus.FAILED:
                    response_values["failed_at"] = now
                elif response_status is ResponseStatus.UNCERTAIN:
                    response_values["uncertain_at"] = now
                response_result = self._session.execute(
                    update(ResponseRecord)
                    .where(
                        ResponseRecord.response_id == delivery.response_id,
                        ResponseRecord.status == ResponseStatus.DELIVERING,
                    )
                    .values(**response_values)
                    .execution_options(synchronize_session=False)
                )
                if response_result.rowcount != 1:
                    self._session.rollback()
                    raise DeliveryStateConflictError(
                        delivery_id=delivery_id,
                        expected_status=ResponseStatus.DELIVERING.value,
                    )
            self._session.commit()
        except DeliveryStateConflictError:
            raise
        except Exception:
            self._session.rollback()
            raise
        return self._required(delivery_id)

    def _claimed(self, delivery_id: int, *, claim_token: str) -> DeliveryOutboxRecord:
        claim_token = _required_string(
            claim_token,
            field_name="claim_token",
            max_length=MAX_CLAIM_TOKEN_LENGTH,
        )
        delivery = self._required(delivery_id)
        if delivery.status is not DeliveryStatus.DELIVERING or delivery.claim_token != claim_token:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status=DeliveryStatus.DELIVERING.value,
            )
        return delivery

    def _required(self, delivery_id: int) -> DeliveryOutboxRecord:
        delivery = self.get(delivery_id)
        if delivery is None:
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status="present",
            )
        return delivery

    def _active_attempt(
        self,
        delivery_id: int,
        part_ordinal: int,
    ) -> DeliveryAttempt | None:
        statement = (
            select(DeliveryAttempt)
            .where(
                DeliveryAttempt.delivery_id == delivery_id,
                DeliveryAttempt.part_ordinal == part_ordinal,
                DeliveryAttempt.status == DeliveryAttemptStatus.DELIVERING,
            )
            .order_by(DeliveryAttempt.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def _required_attempt(
        self,
        attempt_id: int,
        delivery_id: int,
        part_ordinal: int,
    ) -> DeliveryAttempt:
        attempt = self._session.get(DeliveryAttempt, attempt_id)
        if (
            attempt is None
            or attempt.delivery_id != delivery_id
            or attempt.part_ordinal != part_ordinal
        ):
            raise DeliveryStateConflictError(
                delivery_id=delivery_id,
                expected_status="matching_attempt",
            )
        return attempt


def _provider_idempotency_key(delivery_key: str, part_ordinal: int) -> str:
    return f"{delivery_key}:part:{part_ordinal}"


def _required_string(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
