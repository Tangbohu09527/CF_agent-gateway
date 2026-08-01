from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class WechatConversationType(StrEnum):
    GROUP = "group"
    PRIVATE = "private"


class WechatMessageType(StrEnum):
    APP = "app"
    FILE = "file"
    FORWARD = "forward"
    IMAGE = "image"
    REPLY = "reply"
    TEXT = "text"
    UNKNOWN = "unknown"


class WechatReplySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    local_id: str | None = None
    server_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    raw_type: int | None = None
    content: str | None = None


class NormalizedWechatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["wechat"] = "wechat"
    source_account_id: str
    source_message_id: str
    source_message_id_is_fallback: bool
    event_id: str
    conversation_id: str
    conversation_type: WechatConversationType
    conversation_name: str | None = None
    sender_id: str
    sender_name: str | None = None
    message_type: WechatMessageType
    raw_type: int
    content: str
    timestamp: datetime
    is_mentioned: bool | None
    is_self: bool
    reply: WechatReplySummary | None = None
