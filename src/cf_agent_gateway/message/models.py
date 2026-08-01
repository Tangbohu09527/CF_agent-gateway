from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cf_agent_gateway.database import Base


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_account_id",
            "conversation_id",
            name="uq_conversation_source_account_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    source_account_id: Mapped[str] = mapped_column(String(255))
    conversation_id: Mapped[str] = mapped_column(String(255))
    conversation_type: Mapped[str] = mapped_column(String(64))
    conversation_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source", "source_account_id", "conversation_id"],
            [
                "conversations.source",
                "conversations.source_account_id",
                "conversations.conversation_id",
            ],
            name="fk_message_conversation",
            ondelete="CASCADE",
        ),
        UniqueConstraint("event_id", name="uq_message_event_id"),
        UniqueConstraint(
            "source",
            "source_account_id",
            "conversation_id",
            "source_message_id",
            name="uq_message_source_account_conversation_message",
        ),
        CheckConstraint(
            "(conversation_type != 'private' OR is_mentioned IS NULL) AND "
            "(conversation_type != 'group' OR is_mentioned IS NOT NULL)",
            name="ck_message_conversation_mention",
        ),
        Index(
            "ix_message_conversation_timestamp",
            "source",
            "source_account_id",
            "conversation_id",
            "timestamp",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64))
    source_account_id: Mapped[str] = mapped_column(String(255))
    source_message_id: Mapped[str] = mapped_column(String(255))
    conversation_id: Mapped[str] = mapped_column(String(255))
    conversation_type: Mapped[str] = mapped_column(String(64))
    is_mentioned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_self: Mapped[bool] = mapped_column(Boolean)
    sender_id: Mapped[str] = mapped_column(String(255))
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_type: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reply_to_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(255))
    file_size: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(String(1024))
    hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped[Message] = relationship(back_populates="attachments")
