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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class HermesDispatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class HermesDispatchRecord(Base):
    __tablename__ = "hermes_dispatch_records"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_hermes_dispatch_idempotency_key",
        ),
        UniqueConstraint(
            "message_id",
            name="uq_hermes_dispatch_message",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'success', 'failed', 'uncertain')",
            name="ck_hermes_dispatch_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_hermes_dispatch_nonnegative_attempt_count",
        ),
        CheckConstraint(
            "(status = 'queued' AND attempt_count = 0) OR "
            "(status != 'queued' AND attempt_count > 0)",
            name="ck_hermes_dispatch_status_attempt_count",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_hermes_dispatch_nonempty_idempotency_key",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_hermes_dispatch_nonempty_error_code",
        ),
        CheckConstraint(
            "claim_token IS NULL OR length(trim(claim_token)) > 0",
            name="ck_hermes_dispatch_nonempty_claim_token",
        ),
        CheckConstraint(
            "(status = 'queued' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'running' AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'success' AND claim_token IS NULL AND claimed_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(status IN ('failed', 'uncertain') AND claim_token IS NULL "
            "AND claimed_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NOT NULL)",
            name="ck_hermes_dispatch_state_fields",
        ),
        Index(
            "ix_hermes_dispatch_queue",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_hermes_dispatch_thread_queue",
            "ai_thread_id",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), index=True
    )
    enterprise_identity_id: Mapped[str] = mapped_column(
        ForeignKey("enterprise_identities.id", ondelete="RESTRICT")
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("employee_workspaces.id", ondelete="RESTRICT")
    )
    ai_thread_id: Mapped[str] = mapped_column(ForeignKey("ai_threads.id", ondelete="RESTRICT"))
    status: Mapped[HermesDispatchStatus] = mapped_column(
        Enum(
            HermesDispatchStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="hermes_dispatch_status",
        ),
        default=HermesDispatchStatus.QUEUED,
        server_default=HermesDispatchStatus.QUEUED.value,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claim_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
