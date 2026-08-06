from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SkipValidation,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cf_agent_gateway.message.enums import MessageDirection

PreservedText = Annotated[str, StringConstraints(strip_whitespace=False)]
RawPayload = SkipValidation[dict[str, JsonValue]]
_RAW_PAYLOAD_ADAPTER = TypeAdapter(dict[str, JsonValue])


class MessageSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AttachmentMetadata(MessageSchema):
    filename: str = Field(min_length=1, max_length=255)
    file_type: str = Field(min_length=1, max_length=64)
    mime_type: str = Field(min_length=1, max_length=255)
    file_size: int = Field(ge=0)
    storage_path: str = Field(min_length=1, max_length=1024)
    hash: str = Field(min_length=1, max_length=255)


class ReplyContext(MessageSchema):
    source_local_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_server_id: str | None = Field(default=None, min_length=1, max_length=255)
    sender_id: str | None = Field(default=None, min_length=1, max_length=255)
    sender_name: str | None = Field(default=None, min_length=1, max_length=255)
    raw_type: StrictInt | None = None
    content: PreservedText | None = None


class MessageEvent(MessageSchema):
    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {
                    "required": ["timestamp"],
                    "properties": {"timestamp": {"type": "string", "format": "date-time"}},
                },
                {
                    "required": ["occurred_at"],
                    "properties": {"occurred_at": {"type": "string", "format": "date-time"}},
                },
            ]
        }
    )

    event_id: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=64)
    source_account_id: str = Field(min_length=1, max_length=255)
    source_message_id: str = Field(min_length=1, max_length=255)
    conversation_id: str = Field(min_length=1, max_length=255)
    conversation_type: Literal["private", "group"]
    is_mentioned: bool | None = None
    is_self: bool
    conversation_name: str | None = Field(default=None, max_length=255)
    sender_type: Literal["human", "system"] = "human"
    sender_id: str | None = Field(default=None, min_length=1, max_length=255)
    sender_name: str | None = Field(default=None, max_length=255)
    message_type: str = Field(min_length=1, max_length=64)
    raw_type: StrictInt | None = None
    content: PreservedText
    timestamp: datetime | None = None
    occurred_at: datetime | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    direction: MessageDirection | None = None
    source_local_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_server_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_message_id_is_fallback: bool = False
    reply_context: ReplyContext | None = None
    reply_to_message_id: str | None = Field(default=None, max_length=255)
    attachments: list[AttachmentMetadata] = Field(default_factory=list)
    raw_payload: RawPayload | None = None

    @model_validator(mode="before")
    @classmethod
    def populate_legacy_timestamp(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        if "timestamp" in value or "occurred_at" not in value:
            return value
        candidate = dict(value)
        candidate["timestamp"] = candidate["occurred_at"]
        return candidate

    @field_validator("raw_payload", mode="before")
    @classmethod
    def validate_raw_payload(cls, value: object) -> object:
        if value is None:
            return None
        validated = _RAW_PAYLOAD_ADAPTER.validate_python(value)
        json.dumps(validated, allow_nan=False)
        return validated

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.sender_type == "human" and self.sender_id is None:
            raise ValueError("sender_id is required for human senders")
        if self.conversation_type == "private":
            if self.is_mentioned is not None:
                raise ValueError("is_mentioned must be null for private conversations")
        elif self.is_mentioned is None:
            self.is_mentioned = False
        if self.timestamp is None and self.occurred_at is None:
            raise ValueError("timestamp or occurred_at is required")
        if self.timestamp is None:
            self.timestamp = self.occurred_at
        elif self.occurred_at is None:
            self.occurred_at = self.timestamp
        elif self.timestamp != self.occurred_at:
            raise ValueError("occurred_at must match timestamp when both are supplied")
        if self.direction is None:
            if self.sender_type == "system":
                self.direction = MessageDirection.SYSTEM
            elif self.is_self:
                self.direction = MessageDirection.OUTBOUND
            else:
                self.direction = MessageDirection.INBOUND
        return self


class MessageCreated(BaseModel):
    id: int


class AttachmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: int
    filename: str
    file_type: str
    mime_type: str
    file_size: int
    storage_path: str
    hash: str
    created_at: datetime


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: str
    source: str
    source_account_id: str
    source_message_id: str
    conversation_id: str
    conversation_type: str
    is_mentioned: bool | None
    is_self: bool
    sender_type: Literal["human", "system"]
    sender_id: str | None
    sender_name: str | None
    message_type: str
    raw_type: int | None
    content: str
    timestamp: datetime
    occurred_at: datetime
    received_at: datetime
    direction: MessageDirection
    source_local_id: str | None
    source_server_id: str | None
    source_message_id_is_fallback: bool
    reply_context: ReplyContext | None
    reply_to_message_id: str | None
    created_at: datetime
    attachments: list[AttachmentResponse]
