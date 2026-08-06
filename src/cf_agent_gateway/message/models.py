from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cf_agent_gateway.database import Base
from cf_agent_gateway.message.enums import DeliveryAttemptStatus, MessageDirection


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _message_direction_default(context: object) -> str:
    parameters = context.get_current_parameters()  # type: ignore[attr-defined]
    if parameters.get("sender_type") == "system":
        return MessageDirection.SYSTEM.value
    if parameters.get("is_self") is True:
        return MessageDirection.OUTBOUND.value
    return MessageDirection.INBOUND.value


def _occurred_at_default(context: object) -> datetime:
    parameters = context.get_current_parameters()  # type: ignore[attr-defined]
    return parameters["timestamp"]


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
        CheckConstraint(
            "sender_type IN ('human', 'system')",
            name="ck_message_sender_type",
        ),
        CheckConstraint(
            "sender_type = 'system' OR (sender_id IS NOT NULL AND length(trim(sender_id)) > 0)",
            name="ck_message_human_sender_id",
        ),
        CheckConstraint(
            "direction IN ('inbound', 'outbound', 'assistant', 'system')",
            name="ck_message_direction",
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
    sender_type: Mapped[str] = mapped_column(String(16))
    sender_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_type: Mapped[str] = mapped_column(String(64))
    raw_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_local_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_server_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_message_id_is_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    reply_context: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    reply_to_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direction: Mapped[str] = mapped_column(
        String(16),
        default=_message_direction_default,
        server_default=text("'inbound'"),
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_occurred_at_default
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    raw_payload: Mapped[MessageRawPayload | None] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        single_parent=True,
        uselist=False,
    )
    delivery_attempts: Mapped[list[MessageDeliveryAttempt]] = relationship(
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


class MessageRawPayload(Base):
    __tablename__ = "message_raw_payloads"
    __table_args__ = (UniqueConstraint("message_id", name="uq_message_raw_payload_message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    payload: Mapped[dict[str, object]] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped[Message] = relationship(back_populates="raw_payload")


class MessageDeliveryAttempt(Base):
    __tablename__ = "message_delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "attempt_number",
            name="uq_message_delivery_attempt_message_attempt",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="ck_message_delivery_attempt_positive_number",
        ),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name="ck_message_delivery_attempt_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16),
        default=DeliveryAttemptStatus.PENDING.value,
        server_default=text("'pending'"),
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped[Message] = relationship(back_populates="delivery_attempts")
