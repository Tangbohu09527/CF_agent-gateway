from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class HermesDispatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    DEAD = "dead"


class HermesDispatchRecoveryAction(StrEnum):
    RETRY_APPROVED = "retry_approved"
    MARK_DEAD = "mark_dead"
    CONFIRM_SUCCESS = "confirm_success"


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
            "status IN ('queued', 'running', 'success', 'failed', 'uncertain', 'dead')",
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
            "manual_retry_approved = false OR status = 'failed'",
            name="ck_hermes_dispatch_manual_retry_status",
        ),
        CheckConstraint(
            "reconciliation_failure_count >= 0",
            name="ck_hermes_dispatch_reconciliation_failure_count",
        ),
        CheckConstraint(
            "(reconciliation_failure_count = 0 "
            "AND reconciliation_next_attempt_at IS NULL "
            "AND reconciliation_quarantined_at IS NULL "
            "AND reconciliation_last_error_code IS NULL) OR "
            "(reconciliation_failure_count BETWEEN 1 AND 4 "
            "AND reconciliation_next_attempt_at IS NOT NULL "
            "AND reconciliation_quarantined_at IS NULL "
            "AND reconciliation_last_error_code IS NOT NULL) OR "
            "(reconciliation_failure_count >= 5 "
            "AND reconciliation_next_attempt_at IS NULL "
            "AND reconciliation_quarantined_at IS NOT NULL "
            "AND reconciliation_last_error_code IS NOT NULL)",
            name="ck_hermes_dispatch_reconciliation_state",
        ),
        CheckConstraint(
            "(status = 'queued' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL "
            "AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'running' AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'success' AND claim_token IS NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(status IN ('failed', 'uncertain', 'dead') AND claim_token IS NULL "
            "AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL)",
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
        Index(
            "ix_hermes_dispatch_context_timeline",
            "ai_thread_id",
            "status",
            "id",
        ),
        Index(
            "ix_hermes_dispatch_claim",
            "status",
            "lease_expires_at",
            "created_at",
            "id",
        ),
        Index(
            "ix_hermes_dispatch_fifo",
            "ai_thread_id",
            "created_at",
            "id",
            "status",
        ),
        Index(
            "ix_hermes_dispatch_reconciliation",
            "status",
            "reconciliation_quarantined_at",
            "reconciliation_next_attempt_at",
            "id",
        ),
        Index(
            "uq_hermes_dispatch_running_thread",
            "ai_thread_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
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
    manual_retry_approved: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    claim_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reconciliation_failure_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    reconciliation_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    reconciliation_quarantined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    reconciliation_last_error_code: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HermesDispatchRecoveryAudit(Base):
    __tablename__ = "hermes_dispatch_recovery_audits"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_record_id",
            "action",
            "reference",
            name="uq_dispatch_recovery_reference",
        ),
        CheckConstraint(
            "action IN ('retry_approved', 'mark_dead', 'confirm_success')",
            name="ck_dispatch_recovery_action",
        ),
        CheckConstraint(
            "before_status IN ('queued', 'running', 'success', 'failed', 'uncertain', 'dead')",
            name="ck_dispatch_recovery_before_status",
        ),
        CheckConstraint(
            "after_status IN ('queued', 'running', 'success', 'failed', 'uncertain', 'dead')",
            name="ck_dispatch_recovery_after_status",
        ),
        CheckConstraint(
            "(action = 'retry_approved' AND before_status = 'uncertain' "
            "AND after_status = 'failed' AND evidence_dispatch_response_id IS NULL) OR "
            "(action = 'mark_dead' AND before_status = 'uncertain' "
            "AND after_status = 'dead' AND evidence_dispatch_response_id IS NULL) OR "
            "(action = 'confirm_success' AND before_status = 'uncertain' "
            "AND after_status = 'success' AND evidence_dispatch_response_id IS NOT NULL)",
            name="ck_dispatch_recovery_action_transition",
        ),
        CheckConstraint("length(trim(operator)) > 0", name="ck_dispatch_recovery_operator"),
        CheckConstraint("length(trim(reference)) > 0", name="ck_dispatch_recovery_reference"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_dispatch_recovery_reason"),
        Index(
            "ix_dispatch_recovery_record_created",
            "dispatch_record_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_record_id: Mapped[int] = mapped_column(
        ForeignKey("hermes_dispatch_records.id", ondelete="RESTRICT")
    )
    action: Mapped[HermesDispatchRecoveryAction] = mapped_column(
        Enum(
            HermesDispatchRecoveryAction,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="hermes_dispatch_recovery_action",
        )
    )
    operator: Mapped[str] = mapped_column(String(128))
    reference: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(1024))
    before_status: Mapped[HermesDispatchStatus] = mapped_column(
        Enum(
            HermesDispatchStatus,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="hermes_dispatch_recovery_before_status",
        )
    )
    after_status: Mapped[HermesDispatchStatus] = mapped_column(
        Enum(
            HermesDispatchStatus,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="hermes_dispatch_recovery_after_status",
        )
    )
    before_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_dispatch_response_id: Mapped[int | None] = mapped_column(
        ForeignKey("hermes_dispatch_responses.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
