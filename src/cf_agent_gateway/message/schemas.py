from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

PreservedText = Annotated[str, StringConstraints(strip_whitespace=False)]


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
    timestamp: datetime
    source_local_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_server_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_message_id_is_fallback: bool = False
    reply_context: ReplyContext | None = None
    reply_to_message_id: str | None = Field(default=None, max_length=255)
    attachments: list[AttachmentMetadata] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.sender_type == "human" and self.sender_id is None:
            raise ValueError("sender_id is required for human senders")
        if self.conversation_type == "private":
            if self.is_mentioned is not None:
                raise ValueError("is_mentioned must be null for private conversations")
        elif self.is_mentioned is None:
            self.is_mentioned = False
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
    source_local_id: str | None
    source_server_id: str | None
    source_message_id_is_fallback: bool
    reply_context: ReplyContext | None
    reply_to_message_id: str | None
    created_at: datetime
    attachments: list[AttachmentResponse]
