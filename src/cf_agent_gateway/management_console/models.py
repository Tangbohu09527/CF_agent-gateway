from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class DirectorySource(Base):
    __tablename__ = "console_sources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    account_id: Mapped[str | None] = mapped_column(String(255))
    bot_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="not_synced")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(128))
    coverage: Mapped[str] = mapped_column(Text, default="尚未同步")
    sync_details: Mapped[dict] = mapped_column(JSON, default=dict)


class DirectoryEntry(Base):
    __tablename__ = "console_directory"
    __table_args__ = (UniqueConstraint("source_id", "account_id", "username"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("console_sources.id"), index=True)
    account_id: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(255))
    nick_name: Mapped[str] = mapped_column(String(512), default="")
    remark: Mapped[str | None] = mapped_column(String(512))
    alias: Mapped[str | None] = mapped_column(String(255))
    small_head_url: Mapped[str | None] = mapped_column(Text)
    contact_type: Mapped[str] = mapped_column(String(32))
    discovery: Mapped[str] = mapped_column(String(32), default="contacts")
    last_message: Mapped[str | None] = mapped_column(Text)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unread_count: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConsoleSession(Base):
    __tablename__ = "console_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
