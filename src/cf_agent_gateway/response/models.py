from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cf_agent_gateway.database import Base


class ResponseStatus(StrEnum):
    QUEUED = "queued"
    GENERATED = "generated"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ResponsePartKind(StrEnum):
    TEXT = "text"
    ARTIFACT_REF = "artifact_ref"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class ResponseRecord(Base):
    __tablename__ = "hermes_responses"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_hermes_response_idempotency_key"),
        UniqueConstraint("message_id", name="uq_hermes_response_message"),
        CheckConstraint(
            "status IN ('queued', 'generated', 'delivering', 'delivered', 'failed', 'uncertain')",
            name="ck_hermes_response_status",
        ),
        CheckConstraint("part_count > 0", name="ck_hermes_response_positive_part_count"),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_hermes_response_content_sha256",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_hermes_response_nonempty_idempotency_key",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_hermes_response_nonempty_error_code",
        ),
        Index("ix_hermes_response_status_created", "status", "created_at", "response_id"),
        Index("ix_hermes_response_thread", "ai_thread_id", "created_at", "response_id"),
    )

    response_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("employee_workspaces.id", ondelete="RESTRICT")
    )
    ai_thread_id: Mapped[str] = mapped_column(ForeignKey("ai_threads.id", ondelete="RESTRICT"))
    content_sha256: Mapped[str] = mapped_column(String(64))
    part_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[ResponseStatus] = mapped_column(
        Enum(
            ResponseStatus,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="hermes_response_status",
        ),
        default=ResponseStatus.QUEUED,
        server_default=ResponseStatus.QUEUED.value,
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivering_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uncertain_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    parts: Mapped[list[ResponsePartRecord]] = relationship(
        back_populates="response",
        cascade="all, delete-orphan",
        order_by="ResponsePartRecord.ordinal",
    )


class ResponsePartRecord(Base):
    __tablename__ = "hermes_response_parts"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_hermes_response_part_nonnegative_ordinal"),
        CheckConstraint(
            "part_type IN ('text', 'artifact_ref')",
            name="ck_hermes_response_part_type",
        ),
        CheckConstraint(
            "(part_type = 'text' AND text IS NOT NULL AND length(text) > 0 "
            "AND artifact_id IS NULL) OR "
            "(part_type = 'artifact_ref' AND text IS NULL AND artifact_id IS NOT NULL "
            "AND length(trim(artifact_id)) > 0)",
            name="ck_hermes_response_part_shape",
        ),
        Index("ix_hermes_response_part_artifact", "artifact_id"),
    )

    response_id: Mapped[str] = mapped_column(
        ForeignKey("hermes_responses.response_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_type: Mapped[ResponsePartKind] = mapped_column(
        Enum(
            ResponsePartKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="hermes_response_part_kind",
        )
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    response: Mapped[ResponseRecord] = relationship(back_populates="parts")
