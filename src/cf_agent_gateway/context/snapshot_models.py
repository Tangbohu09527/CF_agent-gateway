from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class ContextSnapshotRecord(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (
        CheckConstraint(
            "snapshot_version > 0",
            name="ck_context_snapshot_positive_version",
        ),
        CheckConstraint(
            "length(trim(summary)) > 0",
            name="ck_context_snapshot_nonempty_summary",
        ),
        CheckConstraint(
            "covered_until > 0",
            name="ck_context_snapshot_positive_covered_until",
        ),
    )

    thread_id: Mapped[str] = mapped_column(
        ForeignKey("ai_threads.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    covered_until: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
