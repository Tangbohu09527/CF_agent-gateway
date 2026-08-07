from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat import (
    WechatAPIError,
    WechatResponseError,
    WechatTimeoutError,
    WechatTransportError,
)
from cf_agent_gateway.artifact import (
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactRepository,
    ArtifactStatus,
    ArtifactStorageError,
)
from cf_agent_gateway.delivery.errors import (
    PermanentDeliveryError,
    RetryableDeliveryError,
    UncertainDeliveryError,
)
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryStatus
from cf_agent_gateway.delivery.outbox import DeliveryOutboxStore
from cf_agent_gateway.response.models import ResponsePartKind, ResponsePartRecord


class ChannelDeliverySender(Protocol):
    """Account-scoped sender used by the delivery worker.

    Text-only mocks only need ``send_text`` until an artifact part is encountered.
    Image and file delivery additionally use the Media Adapter V2 ``send_media`` method.
    """

    @property
    def account_id(self) -> str: ...

    def send_text(self, conversation_id: str, content: str) -> object | None: ...

    def send_media(
        self,
        conversation_id: str,
        media_type: str,
        data: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> object | None: ...


class ChannelDeliverySenderFactory(Protocol):
    def __call__(self, *, account_id: str) -> ChannelDeliverySender: ...


class DeliveryFailureKind(StrEnum):
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class DeliveryRunResult:
    delivery_id: int
    response_id: str
    status: DeliveryStatus
    next_part_ordinal: int
    attempt_count: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryBatchResult:
    deliveries: tuple[DeliveryRunResult, ...]

    @property
    def processed(self) -> int:
        return len(self.deliveries)

    @property
    def delivered(self) -> int:
        return sum(result.status is DeliveryStatus.DELIVERED for result in self.deliveries)

    @property
    def failed(self) -> int:
        return sum(result.status is DeliveryStatus.FAILED for result in self.deliveries)

    @property
    def uncertain(self) -> int:
        return sum(result.status is DeliveryStatus.UNCERTAIN for result in self.deliveries)


class ChannelDeliveryWorker:
    """Deliver one response at a time without changing Hermes dispatch records."""

    def __init__(
        self,
        session: Session,
        sender_factory: ChannelDeliverySenderFactory,
        *,
        channel: str = "wechat",
        artifact_repository: ArtifactRepository | None = None,
        outbox_store: DeliveryOutboxStore | None = None,
        max_attempts: int = 3,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        claim_timeout_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
        error_classifier: Callable[[Exception], DeliveryFailureKind] | None = None,
    ) -> None:
        if not isinstance(channel, str) or not channel.strip() or len(channel.strip()) > 64:
            raise ValueError("channel must be a non-empty string of at most 64 characters")
        if any(ord(character) < 0x20 for character in channel.strip()):
            raise ValueError("channel contains invalid characters")
        channel = channel.strip()
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        self._retry_base_seconds = _nonnegative_finite(
            retry_base_seconds,
            "retry_base_seconds",
        )
        self._retry_max_seconds = _nonnegative_finite(
            retry_max_seconds,
            "retry_max_seconds",
        )
        if self._retry_max_seconds < self._retry_base_seconds:
            raise ValueError("retry_max_seconds must be at least retry_base_seconds")
        self._claim_timeout_seconds = _positive_finite(
            claim_timeout_seconds,
            "claim_timeout_seconds",
        )
        self._session = session
        self._sender_factory = sender_factory
        self._artifact_repository = artifact_repository
        self._channel = channel
        self._outbox = outbox_store if outbox_store is not None else DeliveryOutboxStore(session)
        self._max_attempts = max_attempts
        self._clock = clock if clock is not None else _utc_now
        self._error_classifier = (
            error_classifier if error_classifier is not None else classify_delivery_error
        )

    def run_once(self) -> DeliveryRunResult | None:
        now = _aware_utc(self._clock())
        self._outbox.recover_stale_claims(
            channel=self._channel,
            stale_before=now - timedelta(seconds=self._claim_timeout_seconds),
            now=now,
        )
        claim_token = str(uuid4())
        delivery = self._outbox.claim_next(
            channel=self._channel,
            claim_token=claim_token,
            now=now,
        )
        if delivery is None:
            return None

        sender: ChannelDeliverySender | None = None
        try:
            while delivery.status is DeliveryStatus.DELIVERING:
                ordinal = delivery.next_part_ordinal
                attempt = self._outbox.start_attempt(
                    delivery.id,
                    claim_token=claim_token,
                    part_ordinal=ordinal,
                )
                try:
                    try:
                        part = self._outbox.get_part(delivery.response_id, ordinal)
                    except Exception as error:
                        self._session.rollback()
                        raise RetryableDeliveryError("response part lookup failed") from error
                    if part is None:
                        raise PermanentDeliveryError("response part is missing")
                    if sender is None:
                        try:
                            sender = self._sender_factory(account_id=delivery.account_id)
                        except (
                            PermanentDeliveryError,
                            RetryableDeliveryError,
                            UncertainDeliveryError,
                        ):
                            raise
                        except Exception as error:
                            raise RetryableDeliveryError("sender initialization failed") from error
                        try:
                            sender_account_id = sender.account_id
                        except Exception as error:
                            raise PermanentDeliveryError("sender account is unavailable") from error
                        if sender_account_id != delivery.account_id:
                            raise PermanentDeliveryError("sender account does not match delivery")
                    raw_receipt = self._deliver_part(delivery, part, sender)
                except Exception as error:
                    return self._record_failure(
                        delivery,
                        claim_token=claim_token,
                        attempt_id=attempt.id,
                        attempt_number=attempt.attempt_number,
                        error=error,
                    )

                receipt_payload = _receipt_payload(raw_receipt)
                provider_message_id = _provider_message_id(receipt_payload)
                try:
                    delivery = self._outbox.mark_part_delivered(
                        delivery.id,
                        claim_token=claim_token,
                        attempt_id=attempt.id,
                        part_ordinal=ordinal,
                        receipt_payload=receipt_payload,
                        provider_message_id=provider_message_id,
                        completed_at=_aware_utc(self._clock()),
                    )
                except Exception:
                    # The provider accepted the part, but the local commit is ambiguous.
                    self._session.rollback()
                    existing_receipt = self._outbox.get_receipt(delivery.id, ordinal)
                    if existing_receipt is not None:
                        delivery = self._outbox.get(delivery.id) or delivery
                        continue
                    current = self._outbox.get(delivery.id)
                    if current is not None and current.status is not DeliveryStatus.DELIVERING:
                        return _run_result(
                            current,
                            error_code="delivery_receipt_persistence_uncertain",
                        )
                    delivery = self._outbox.mark_attempt_uncertain(
                        delivery.id,
                        claim_token=claim_token,
                        attempt_id=attempt.id,
                        error_code="delivery_receipt_persistence_uncertain",
                    )
                    return _run_result(
                        delivery,
                        error_code="delivery_receipt_persistence_uncertain",
                    )
            return _run_result(delivery)
        finally:
            if sender is not None:
                close = getattr(sender, "close", None)
                if callable(close):
                    with suppress(Exception):
                        close()

    def run_until_idle(self, *, max_deliveries: int = 100) -> DeliveryBatchResult:
        if (
            isinstance(max_deliveries, bool)
            or not isinstance(max_deliveries, int)
            or max_deliveries <= 0
        ):
            raise ValueError("max_deliveries must be a positive integer")
        results: list[DeliveryRunResult] = []
        while len(results) < max_deliveries:
            result = self.run_once()
            if result is None:
                break
            results.append(result)
        return DeliveryBatchResult(deliveries=tuple(results))

    def _deliver_part(
        self,
        delivery: DeliveryOutboxRecord,
        part: ResponsePartRecord,
        sender: ChannelDeliverySender,
    ) -> object | None:
        if part.part_type is ResponsePartKind.TEXT:
            if part.text is None:
                raise PermanentDeliveryError("text response part is invalid")
            return sender.send_text(delivery.conversation_id, part.text)
        if part.part_type is not ResponsePartKind.ARTIFACT_REF or part.artifact_id is None:
            raise PermanentDeliveryError("response part type is unsupported")
        if self._artifact_repository is None:
            raise PermanentDeliveryError("artifact repository is not configured")

        artifact = self._artifact_repository.get(part.artifact_id)
        if artifact is None:
            raise PermanentDeliveryError("artifact does not exist")
        if artifact.response_id != delivery.response_id:
            raise PermanentDeliveryError("artifact belongs to a different response")
        if artifact.status is ArtifactStatus.CREATED:
            raise RetryableDeliveryError("artifact is not ready")
        if artifact.status is not ArtifactStatus.READY:
            raise PermanentDeliveryError("artifact is unavailable")
        try:
            content = self._artifact_repository.read(artifact.artifact_id)
        except ArtifactIntegrityError:
            raise PermanentDeliveryError("artifact integrity validation failed") from None
        except ArtifactStorageError as error:
            raise RetryableDeliveryError("artifact storage is unavailable") from error

        send_media = getattr(sender, "send_media", None)
        if not callable(send_media):
            raise PermanentDeliveryError("sender does not support media delivery")
        if artifact.kind is ArtifactKind.IMAGE:
            return send_media(
                delivery.conversation_id,
                ArtifactKind.IMAGE.value,
                content,
                artifact.mime_type,
            )
        return send_media(
            delivery.conversation_id,
            ArtifactKind.FILE.value,
            content,
            artifact.mime_type,
            artifact.filename,
        )

    def _record_failure(
        self,
        delivery: DeliveryOutboxRecord,
        *,
        claim_token: str,
        attempt_id: int,
        error: Exception,
        attempt_number: int,
    ) -> DeliveryRunResult:
        failure_kind = self._error_classifier(error)
        error_code = delivery_error_code(error)
        if failure_kind is DeliveryFailureKind.RETRYABLE and attempt_number < self._max_attempts:
            delay = min(
                self._retry_max_seconds,
                self._retry_base_seconds * (2 ** max(0, attempt_number - 1)),
            )
            current = self._outbox.mark_attempt_retryable(
                delivery.id,
                claim_token=claim_token,
                attempt_id=attempt_id,
                error_code=error_code,
                available_at=_aware_utc(self._clock()) + timedelta(seconds=delay),
            )
            return _run_result(current, error_code=error_code)
        if failure_kind is DeliveryFailureKind.RETRYABLE:
            error_code = "delivery_attempts_exhausted"
            current = self._outbox.mark_attempt_failed(
                delivery.id,
                claim_token=claim_token,
                attempt_id=attempt_id,
                error_code=error_code,
            )
            return _run_result(current, error_code=error_code)
        if failure_kind is DeliveryFailureKind.PERMANENT:
            current = self._outbox.mark_attempt_failed(
                delivery.id,
                claim_token=claim_token,
                attempt_id=attempt_id,
                error_code=error_code,
            )
            return _run_result(current, error_code=error_code)
        current = self._outbox.mark_attempt_uncertain(
            delivery.id,
            claim_token=claim_token,
            attempt_id=attempt_id,
            error_code=error_code,
        )
        return _run_result(current, error_code=error_code)


def classify_delivery_error(error: Exception) -> DeliveryFailureKind:
    if isinstance(error, RetryableDeliveryError):
        return DeliveryFailureKind.RETRYABLE
    if isinstance(error, PermanentDeliveryError):
        return DeliveryFailureKind.PERMANENT
    if isinstance(error, UncertainDeliveryError):
        return DeliveryFailureKind.UNCERTAIN
    if isinstance(error, WechatAPIError):
        if error.status_code == 429:
            return DeliveryFailureKind.RETRYABLE
        if 500 <= error.status_code < 600:
            return DeliveryFailureKind.UNCERTAIN
        return DeliveryFailureKind.PERMANENT
    if isinstance(error, (WechatTimeoutError, WechatTransportError, WechatResponseError)):
        return DeliveryFailureKind.UNCERTAIN
    if isinstance(error, ValueError):
        return DeliveryFailureKind.PERMANENT
    return DeliveryFailureKind.UNCERTAIN


def delivery_error_code(error: Exception) -> str:
    if isinstance(error, WechatAPIError):
        return f"{error.code}:http_{error.status_code}"
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return code[:128]
    return "unexpected_delivery_error"


def _receipt_payload(value: object | None) -> dict[str, object]:
    if value is None:
        return {"accepted": True}
    if isinstance(value, Mapping):
        try:
            serialized = json.dumps(dict(value), ensure_ascii=True, separators=(",", ":"))
            normalized = json.loads(serialized)
        except (TypeError, ValueError, OverflowError):
            return {"accepted": True}
        if isinstance(normalized, dict):
            return normalized
    return {"accepted": True}


def _provider_message_id(receipt: dict[str, object]) -> str | None:
    for field_name in ("message_id", "messageId", "localId", "id"):
        value = receipt.get(field_name)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            normalized = str(value)
            if normalized and len(normalized) <= 255:
                return normalized
    return None


def _run_result(
    delivery: DeliveryOutboxRecord,
    *,
    error_code: str | None = None,
) -> DeliveryRunResult:
    return DeliveryRunResult(
        delivery_id=delivery.id,
        response_id=delivery.response_id,
        status=delivery.status,
        next_part_ordinal=delivery.next_part_ordinal,
        attempt_count=delivery.attempt_count,
        error_code=error_code,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _nonnegative_finite(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    return normalized


def _positive_finite(value: object, field_name: str) -> float:
    normalized = _nonnegative_finite(value, field_name)
    if normalized <= 0:
        raise ValueError(f"{field_name} must be a finite positive number")
    return normalized
