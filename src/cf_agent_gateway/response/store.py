from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryStatus
from cf_agent_gateway.hermes.models import (
    ArtifactRefPart,
    HermesDispatchOutcome,
    ResponseEnvelope,
    TextPart,
)
from cf_agent_gateway.response.errors import (
    ResponseConflictError,
    ResponseValidationError,
)
from cf_agent_gateway.response.models import (
    ResponsePartKind,
    ResponsePartRecord,
    ResponseRecord,
    ResponseStatus,
)

RESPONSE_IDEMPOTENCY_NAMESPACE = "v1:hermes-response:message"
DELIVERY_IDEMPOTENCY_NAMESPACE = "v1:response-delivery"


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    channel: str
    account_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", _required_string(self.channel, "channel", 64))
        object.__setattr__(
            self,
            "account_id",
            _required_string(self.account_id, "account_id", 255),
        )
        object.__setattr__(
            self,
            "conversation_id",
            _required_string(self.conversation_id, "conversation_id", 255),
        )


def build_response_idempotency_key(message_id: int) -> str:
    message_id = _positive_message_id(message_id)
    return f"{RESPONSE_IDEMPOTENCY_NAMESPACE}:{message_id}"


def build_stable_response_id(message_id: int) -> str:
    message_id = _positive_message_id(message_id)
    return f"legacy-response:message:{message_id}"


class ResponseStore:
    """Persist a generated response and its delivery job in one transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_generated(
        self,
        outcome: HermesDispatchOutcome,
        *,
        target: DeliveryTarget,
    ) -> tuple[ResponseRecord, DeliveryOutboxRecord, bool]:
        envelope = _response_envelope(outcome)
        idempotency_key = build_response_idempotency_key(outcome.message_id)
        content_sha256 = _content_sha256(envelope)
        target_key = _target_key(target)
        delivery_key = _delivery_idempotency_key(idempotency_key, target_key)

        existing = self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            delivery = self._delivery_for(existing.response_id, target)
            self._require_compatible(
                existing,
                delivery,
                outcome=outcome,
                envelope=envelope,
                content_sha256=content_sha256,
                target=target,
                target_key=target_key,
            )
            return existing, delivery, False

        now = datetime.now(UTC)
        response = ResponseRecord(
            response_id=envelope.response_id,
            idempotency_key=idempotency_key,
            message_id=outcome.message_id,
            workspace_id=outcome.workspace_id,
            ai_thread_id=outcome.ai_thread_id,
            content_sha256=content_sha256,
            part_count=len(envelope.parts),
            status=ResponseStatus.QUEUED,
        )
        response.parts = [
            _part_record(envelope.response_id, ordinal, part)
            for ordinal, part in enumerate(envelope.parts)
        ]
        delivery = DeliveryOutboxRecord(
            idempotency_key=delivery_key,
            response_id=envelope.response_id,
            channel=target.channel,
            account_id=target.account_id,
            conversation_id=target.conversation_id,
            target_key=target_key,
            status=DeliveryStatus.QUEUED,
            available_at=now,
        )
        response.status = ResponseStatus.GENERATED
        response.generated_at = now
        self._session.add(response)
        try:
            self._session.flush()
            self._session.add(delivery)
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                delivery = self._delivery_for(existing.response_id, target)
                self._require_compatible(
                    existing,
                    delivery,
                    outcome=outcome,
                    envelope=envelope,
                    content_sha256=content_sha256,
                    target=target,
                    target_key=target_key,
                )
                return existing, delivery, False
            raise
        except Exception:
            self._session.rollback()
            raise
        return (
            self.get_required(response.response_id),
            self.get_delivery_required(delivery.id),
            True,
        )

    def save(
        self,
        outcome: HermesDispatchOutcome,
        *,
        target: DeliveryTarget,
    ) -> tuple[ResponseRecord, DeliveryOutboxRecord, bool]:
        return self.save_generated(outcome, target=target)

    def get(self, response_id: str) -> ResponseRecord | None:
        statement = (
            select(ResponseRecord)
            .where(ResponseRecord.response_id == response_id)
            .options(selectinload(ResponseRecord.parts))
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def get_required(self, response_id: str) -> ResponseRecord:
        response = self.get(response_id)
        if response is None:
            raise ResponseValidationError("response_not_found")
        return response

    def get_by_message_id(self, message_id: int) -> ResponseRecord | None:
        return self.get_by_idempotency_key(build_response_idempotency_key(message_id))

    def get_by_idempotency_key(self, idempotency_key: str) -> ResponseRecord | None:
        statement = (
            select(ResponseRecord)
            .where(ResponseRecord.idempotency_key == idempotency_key)
            .options(selectinload(ResponseRecord.parts))
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def get_delivery_required(self, delivery_id: int) -> DeliveryOutboxRecord:
        delivery = self._session.get(DeliveryOutboxRecord, delivery_id)
        if delivery is None:
            raise ResponseValidationError("delivery_not_found")
        self._session.refresh(delivery)
        return delivery

    def _delivery_for(
        self,
        response_id: str,
        target: DeliveryTarget,
    ) -> DeliveryOutboxRecord:
        statement = select(DeliveryOutboxRecord).where(
            DeliveryOutboxRecord.response_id == response_id,
            DeliveryOutboxRecord.channel == target.channel,
            DeliveryOutboxRecord.target_key == _target_key(target),
        )
        delivery = self._session.scalar(statement)
        if delivery is None:
            raise ResponseConflictError(
                idempotency_key=build_response_idempotency_key(
                    self.get_required(response_id).message_id
                )
            )
        return delivery

    @staticmethod
    def _require_compatible(
        response: ResponseRecord,
        delivery: DeliveryOutboxRecord,
        *,
        outcome: HermesDispatchOutcome,
        envelope: ResponseEnvelope,
        content_sha256: str,
        target: DeliveryTarget,
        target_key: str,
    ) -> None:
        stored_parts = tuple(
            (
                part.part_type.value,
                part.text,
                part.artifact_id,
            )
            for part in response.parts
        )
        expected_parts = tuple(
            (
                part.type,
                part.text if isinstance(part, TextPart) else None,
                part.artifact_id if isinstance(part, ArtifactRefPart) else None,
            )
            for part in envelope.parts
        )
        if (
            response.response_id != envelope.response_id
            or response.message_id != outcome.message_id
            or response.workspace_id != outcome.workspace_id
            or response.ai_thread_id != outcome.ai_thread_id
            or response.content_sha256 != content_sha256
            or response.part_count != len(envelope.parts)
            or stored_parts != expected_parts
            or delivery.channel != target.channel
            or delivery.account_id != target.account_id
            or delivery.conversation_id != target.conversation_id
            or delivery.target_key != target_key
        ):
            raise ResponseConflictError(idempotency_key=response.idempotency_key)


def _response_envelope(outcome: HermesDispatchOutcome) -> ResponseEnvelope:
    if not isinstance(outcome, HermesDispatchOutcome):
        raise ResponseValidationError("invalid_dispatch_outcome")
    if outcome.response is not None:
        return outcome.response
    if not isinstance(outcome.assistant_content, str) or not outcome.assistant_content:
        raise ResponseValidationError("empty_response")
    return ResponseEnvelope(
        response_id=build_stable_response_id(outcome.message_id),
        parts=(TextPart(text=outcome.assistant_content),),
    )


def _part_record(
    response_id: str,
    ordinal: int,
    part: TextPart | ArtifactRefPart,
) -> ResponsePartRecord:
    if isinstance(part, TextPart):
        return ResponsePartRecord(
            response_id=response_id,
            ordinal=ordinal,
            part_type=ResponsePartKind.TEXT,
            text=part.text,
        )
    if isinstance(part, ArtifactRefPart):
        return ResponsePartRecord(
            response_id=response_id,
            ordinal=ordinal,
            part_type=ResponsePartKind.ARTIFACT_REF,
            artifact_id=part.artifact_id,
        )
    raise ResponseValidationError("unsupported_response_part")


def _content_sha256(envelope: ResponseEnvelope) -> str:
    payload = envelope.model_dump(mode="json")
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _target_key(target: DeliveryTarget) -> str:
    canonical = json.dumps(
        {
            "account_id": target.account_id,
            "channel": target.channel,
            "conversation_id": target.conversation_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _delivery_idempotency_key(response_key: str, target_key: str) -> str:
    digest = hashlib.sha256(f"{response_key}:{target_key}".encode()).hexdigest()
    return f"{DELIVERY_IDEMPOTENCY_NAMESPACE}:{digest}"


def _positive_message_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ResponseValidationError("invalid_message_id")
    return value


def _required_string(value: object, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResponseValidationError(f"{field_name}_missing")
    normalized = value.strip()
    if len(normalized) > max_length or any(ord(character) < 0x20 for character in normalized):
        raise ResponseValidationError(f"{field_name}_invalid")
    return normalized
