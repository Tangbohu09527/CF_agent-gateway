from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class HermesOperationStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


_OPERATION_STATE_CONSTRAINT = (
    "(status = 'succeeded' AND lease_token IS NULL AND lease_expires_at IS NULL) OR "
    "(status = 'in_progress' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
    "(status = 'failed' AND lease_token IS NULL AND lease_expires_at IS NULL)"
)


class HermesDispatchRecord(Base):
    __tablename__ = "hermes_dispatch_records"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_hermes_dispatch_message"),
        CheckConstraint("attempt_count >= 1", name="ck_hermes_dispatch_attempt_count"),
        CheckConstraint(_OPERATION_STATE_CONSTRAINT, name="ck_hermes_dispatch_state"),
        CheckConstraint(
            "status != 'succeeded' OR assistant_content IS NOT NULL",
            name="ck_hermes_dispatch_succeeded_content",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("employee_workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    ai_thread_id: Mapped[str] = mapped_column(
        ForeignKey("ai_threads.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[HermesOperationStatus] = mapped_column(
        Enum(
            HermesOperationStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="hermes_operation_status",
        ),
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_hermes_thread_id: Mapped[str] = mapped_column(String(255))
    result_hermes_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assistant_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HermesDeliveryRecord(Base):
    __tablename__ = "hermes_delivery_records"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_hermes_delivery_message"),
        CheckConstraint("attempt_count >= 1", name="ck_hermes_delivery_attempt_count"),
        CheckConstraint(_OPERATION_STATE_CONSTRAINT, name="ck_hermes_delivery_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    ai_thread_id: Mapped[str] = mapped_column(
        ForeignKey("ai_threads.id", ondelete="RESTRICT"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(String(255))
    content_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[HermesOperationStatus] = mapped_column(
        Enum(
            HermesOperationStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="hermes_operation_status",
        ),
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HermesRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HermesUserMessage(HermesRequestModel):
    role: Literal["user"] = "user"
    content: StrictStr = Field(min_length=1)


class HermesChatCompletionRequest(HermesRequestModel):
    model: StrictStr = Field(min_length=1)
    messages: list[HermesUserMessage] = Field(min_length=1)


class HermesResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class HermesAssistantMessage(HermesResponseModel):
    role: Literal["assistant"]
    content: StrictStr = Field(min_length=1)


class HermesChatCompletionChoice(HermesResponseModel):
    message: HermesAssistantMessage


class HermesChatCompletionResponse(HermesResponseModel):
    choices: list[HermesChatCompletionChoice] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class HermesChatResult:
    assistant_content: str
    hermes_thread_id: str


@dataclass(frozen=True, slots=True)
class HermesDispatchOutcome:
    message_id: int
    workspace_id: str
    ai_thread_id: str
    assistant_content: str


@dataclass(frozen=True, slots=True)
class HermesResponseDeliveryOutcome:
    message_id: int
    ai_thread_id: str
    conversation_id: str
