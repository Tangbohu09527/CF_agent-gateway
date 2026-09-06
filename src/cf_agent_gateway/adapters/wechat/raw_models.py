from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic_core import to_jsonable_python


class RawWechatModel(BaseModel):
    """Validated upstream data with undeclared top-level fields deliberately ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AgentWechatAuthStatus(RawWechatModel):
    logged_in_user: StrictStr | None = Field(
        default=None, alias="loggedInUser", min_length=1, max_length=255, repr=False
    )
    status: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("logged_in_user", "status")
    @classmethod
    def validate_auth_identifier(cls, value: str | None) -> str | None:
        if value is not None and any(
            not character.isprintable() or character.isspace() for character in value
        ):
            raise ValueError(
                "auth state and account must be printable identifiers without whitespace"
            )
        return value

    @property
    def source_account_id(self) -> str | None:
        # The sole account source is the authenticated API field, never a nickname,
        # configured bot ID, Linux identity, token, or remembered previous login.
        return self.logged_in_user if self.status == "logged_in" else None


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
