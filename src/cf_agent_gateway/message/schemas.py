from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MessageSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AttachmentMetadata(MessageSchema):
    filename: str = Field(min_length=1, max_length=255)
    file_type: str = Field(min_length=1, max_length=64)
    mime_type: str = Field(min_length=1, max_length=255)
    file_size: int = Field(ge=0)
    storage_path: str = Field(min_length=1, max_length=1024)
    hash: str = Field(min_length=1, max_length=255)


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
    sender_id: str = Field(min_length=1, max_length=255)
    sender_name: str | None = Field(default=None, max_length=255)
    message_type: str = Field(min_length=1, max_length=64)
    content: str
    timestamp: datetime
    reply_to_message_id: str | None = Field(default=None, max_length=255)
    attachments: list[AttachmentMetadata] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_mention(self) -> Self:
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
    sender_id: str
    sender_name: str | None
    message_type: str
    content: str
    timestamp: datetime
    reply_to_message_id: str | None
    created_at: datetime
    attachments: list[AttachmentResponse]
