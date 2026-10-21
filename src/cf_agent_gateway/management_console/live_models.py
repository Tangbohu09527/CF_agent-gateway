"""Candidate-only operational records. Never applied to the external archive."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class LiveObservation(Base):
    __tablename__ = "console_live_observations"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ArchiveControl(Base):
    __tablename__ = "console_archive_controls"
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    account_id: Mapped[str] = mapped_column(String(255))
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(128))
    # A first observation records identities only. It never imports an old window.
    baselined_chats: Mapped[dict] = mapped_column(JSON, default=dict)


class ArchiveObservation(Base):
    __tablename__ = "console_archive_observations"
    __table_args__ = (UniqueConstraint("source_id", "account_id", "event_id"),)
    __mapper_args__ = {"eager_defaults": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(255))
    event_id: Mapped[str] = mapped_column(String(255))
    conversation_id: Mapped[str] = mapped_column(String(255))
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    upstream_readable_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
