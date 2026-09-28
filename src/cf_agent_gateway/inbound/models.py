from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base

TERMINAL_STATES = ("ready", "unsupported", "failed", "timed_out")


class InboundMediaJob(Base):
    __tablename__ = "inbound_media_jobs"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_inbound_media_message"),
        UniqueConstraint("dispatch_id", name="uq_inbound_media_dispatch"),
        CheckConstraint(
            "state IN ('pending', 'fetching', 'publishing', 'ready', "
            "'unsupported', 'failed', 'timed_out')",
            name="ck_inbound_media_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_inbound_media_attempts"),
        CheckConstraint(
            "(claim_token IS NULL AND lease_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND state IN ('fetching', 'publishing'))",
            name="ck_inbound_media_lease",
        ),
        CheckConstraint(
            "state != 'ready' OR (attachment_id IS NOT NULL AND manifest IS NOT NULL "
            "AND claim_token IS NULL)",
            name="ck_inbound_media_ready",
        ),
        Index("ix_inbound_media_due", "state", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="RESTRICT"))
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("hermes_dispatch_records.id", ondelete="RESTRICT")
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    attempt_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    manifest: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"))
    read_token_hash: Mapped[str | None] = mapped_column(String(64))
    read_claim_token: Mapped[str | None] = mapped_column(String(255))
    read_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
