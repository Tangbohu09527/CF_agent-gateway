"""Durable, claim-scoped handoff; ciphertext is never an ORM repr/log field."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class InboundHostBinding(Base):
    __tablename__ = "inbound_host_bindings"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_inbound_host_session"),
        UniqueConstraint("dispatch_id", "claim_token_hash", name="uq_inbound_host_claim"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dispatch_id: Mapped[int] = mapped_column(ForeignKey("hermes_dispatch_records.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("inbound_media_jobs.id"))
    ai_thread_id: Mapped[str] = mapped_column(ForeignKey("ai_threads.id"), index=True)
    claim_token_hash: Mapped[str] = mapped_column(String(64))
    claim_epoch: Mapped[str] = mapped_column(String(36))
    session_id: Mapped[str] = mapped_column(String(64))
    parent_session_id: Mapped[str | None] = mapped_column(String(255))
    preparation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_digest: Mapped[str | None] = mapped_column(String(64))
    runtime_config_digest: Mapped[str | None] = mapped_column(String(64))
    profile_reference: Mapped[str] = mapped_column(String(255))
    profile_revision: Mapped[int] = mapped_column()
    host_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), default="preparing")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grant_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grant_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    host_instance_id: Mapped[str | None] = mapped_column(String(128))
    host_nonce_hash: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(128))
    host_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    events_connected: Mapped[bool] = mapped_column(default=False)
    sequence: Mapped[int] = mapped_column(default=1)
    reason: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
