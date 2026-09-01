from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class DeliveryAttemptStatus(StrEnum):
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class DeliveryOutboxRecord(Base):
    __tablename__ = "delivery_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_delivery_outbox_idempotency_key"),
        UniqueConstraint(
            "id",
            "response_id",
            name="uq_delivery_outbox_id_response",
        ),
        UniqueConstraint(
            "response_id",
            "channel",
            "target_key",
            name="uq_delivery_outbox_response_target",
        ),
        CheckConstraint(
            "status IN ('queued', 'delivering', 'delivered', 'failed', 'uncertain')",
            name="ck_delivery_outbox_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_delivery_outbox_attempt_count"),
        CheckConstraint("next_part_ordinal >= 0", name="ck_delivery_outbox_next_part"),
        CheckConstraint("length(target_key) = 64", name="ck_delivery_outbox_target_key"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_delivery_outbox_nonempty_idempotency_key",
        ),
        CheckConstraint(
            "claim_token IS NULL OR length(trim(claim_token)) > 0",
            name="ck_delivery_outbox_nonempty_claim_token",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_delivery_outbox_nonempty_error_code",
        ),
        Index("ix_delivery_outbox_queue", "status", "available_at", "created_at", "id"),
        Index("ix_delivery_outbox_response", "response_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    response_id: Mapped[str] = mapped_column(
        ForeignKey("hermes_responses.response_id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(255))
    conversation_id: Mapped[str] = mapped_column(String(255))
    target_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(
            DeliveryStatus,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="delivery_outbox_status",
        ),
        default=DeliveryStatus.QUEUED,
        server_default=DeliveryStatus.QUEUED.value,
    )
    next_part_ordinal: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claim_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "part_ordinal",
            "attempt_number",
            name="uq_delivery_attempt_part_number",
        ),
        UniqueConstraint(
            "id",
            "delivery_id",
            "part_ordinal",
            name="uq_delivery_attempt_identity",
        ),
        CheckConstraint("part_ordinal >= 0", name="ck_delivery_attempt_part_ordinal"),
        CheckConstraint("attempt_number > 0", name="ck_delivery_attempt_positive_number"),
        CheckConstraint(
            "status IN ('delivering', 'delivered', 'failed', 'uncertain')",
            name="ck_delivery_attempt_status",
        ),
        Index("ix_delivery_attempt_delivery", "delivery_id", "part_ordinal", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("delivery_outbox.id", ondelete="CASCADE"))
    part_ordinal: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer)
    provider_idempotency_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[DeliveryAttemptStatus] = mapped_column(
        Enum(
            DeliveryAttemptStatus,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="delivery_attempt_status",
        ),
        default=DeliveryAttemptStatus.DELIVERING,
        server_default=DeliveryAttemptStatus.DELIVERING.value,
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryReceipt(Base):
    __tablename__ = "delivery_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["delivery_id", "response_id"],
            ["delivery_outbox.id", "delivery_outbox.response_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "delivery_id", "part_ordinal"],
            [
                "delivery_attempts.id",
                "delivery_attempts.delivery_id",
                "delivery_attempts.part_ordinal",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["response_id", "part_ordinal"],
            ["hermes_response_parts.response_id", "hermes_response_parts.ordinal"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("attempt_id", name="uq_delivery_receipt_attempt"),
        CheckConstraint("part_ordinal >= 0", name="ck_delivery_receipt_part_ordinal"),
    )

    delivery_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    response_id: Mapped[str] = mapped_column(String(255))
    attempt_id: Mapped[int] = mapped_column(Integer)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_payload: Mapped[dict[str, object]] = mapped_column(JSON(none_as_null=True))
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
