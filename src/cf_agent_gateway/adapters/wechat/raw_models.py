from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, StrictStr, model_validator
from pydantic_core import to_jsonable_python


class RawWechatModel(BaseModel):
    """Validated upstream data with undeclared top-level fields deliberately ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AgentWechatAuthStatus(RawWechatModel):
    logged_in_user: str | None = Field(default=None, alias="loggedInUser", min_length=1)
    status: Any

    @property
    def source_account_id(self) -> str | None:
        return self.logged_in_user


class AgentWechatMedia(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media_type: str
    data: bytes | None
    format: str | None
    filename: str | None
    supported: bool


class RawWechatMessage(RawWechatModel):
    raw_payload: dict[str, JsonValue] = Field(exclude=True, repr=False)
    local_id: StrictInt | StrictStr | None = Field(default=None, alias="localId")
    server_id: StrictInt | StrictStr | None = Field(default=None, alias="serverId")
    chat_id: str = Field(alias="chatId", min_length=1)
    sender: str | None = None
    sender_name: str | None = Field(default=None, alias="senderName")
    type: StrictInt
    content: str
    timestamp: datetime

    # These values intentionally remain uncoerced. Normalization follows JSON identity
    # semantics: only the literal boolean true is treated as true.
    is_mentioned: Any = Field(default=None, alias="isMentioned")
    is_self: Any = Field(default=None, alias="isSelf")

    # The upstream reply schema is not yet stable. Preserve the supplied JSON value at
    # this declared boundary and only summarize verified message-like keys downstream.
    reply: Any = None

    @model_validator(mode="before")
    @classmethod
    def preserve_raw_payload(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        candidate = dict(value)
        candidate["raw_payload"] = to_jsonable_python(deepcopy(dict(value)))
        return candidate
