from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class RuntimeWorkerStatus(Base):
    """Durable lease and operational status for a singleton runtime worker."""

    __tablename__ = "runtime_worker_status"
    __table_args__ = (
        CheckConstraint(
            "state IN ('starting', 'idle', 'polling', 'degraded', 'stopped')",
            name="ck_runtime_worker_status_state",
        ),
    )

    worker_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    hermes_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    delivery_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_cycle_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_cycle_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_logged_in: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    chats_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
