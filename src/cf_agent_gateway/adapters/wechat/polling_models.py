from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base

MAX_CHECKPOINT_LOCAL_ID = 2**63 - 1


class BootstrapMode(StrEnum):
    LATEST = "latest"
    BACKFILL = "backfill"


class PollFailureStage(StrEnum):
    AUTH = "auth"
    RECOVERY = "recovery"
    LIST_CHATS = "list_chats"
    PARSE_CHAT = "parse_chat"
    LIST_MESSAGES = "list_messages"
    VALIDATE_MESSAGE = "validate_message"
    NORMALIZE = "normalize"
    POLL_CHAT = "poll_chat"
    SINK = "sink"
    CHECKPOINT = "checkpoint"


class PollFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: PollFailureStage
    code: str
    conversation_id: str | None = None
    local_id: int | None = None


class ChatPollResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str | None = None
    conversation_name: str | None = None
    succeeded: bool
    messages_seen: int = 0
    messages_processed: int = 0
    messages_skipped_by_checkpoint: int = 0
    bootstrapped: bool = False
    failures: list[PollFailure] = Field(default_factory=list)


class PollResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_account_id: str | None = None
    logged_in: bool
    chats_seen: int = 0
    chats_succeeded: int = 0
    chats_failed: int = 0
    messages_seen: int = 0
    messages_processed: int = 0
    messages_skipped_by_checkpoint: int = 0
    bootstrapped_chats: int = 0
    failures: list[PollFailure] = Field(default_factory=list)
    chat_results: list[ChatPollResult] = Field(default_factory=list)


class WechatSyncCheckpoint(Base):
    __tablename__ = "wechat_sync_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "source_account_id",
            "conversation_id",
            name="uq_wechat_sync_checkpoint_account_conversation",
        ),
        CheckConstraint(
            "last_local_id >= 0",
            name="ck_wechat_sync_checkpoint_nonnegative_local_id",
        ),
        CheckConstraint(
            "regression_generation >= 0",
            name="ck_wechat_sync_checkpoint_nonnegative_generation",
        ),
        CheckConstraint(
            "last_message_fingerprint IS NULL OR length(last_message_fingerprint) = 64",
            name="ck_wechat_sync_checkpoint_fingerprint_length",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_account_id: Mapped[str] = mapped_column(String(255))
    conversation_id: Mapped[str] = mapped_column(String(255))
    last_local_id: Mapped[int] = mapped_column(BigInteger)
    regression_generation: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
    )
    last_message_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    initialized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
