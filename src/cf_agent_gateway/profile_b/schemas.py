from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")]
SourceId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_@.:-]+$")]
Kind = Literal["employee", "group"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmployeeInput(Strict):
    employee_id: SourceId
    name: Name
    account_id: SourceId
    sender_id: SourceId
    enabled: bool = True
    bot_allowed: bool = False
    group_allowed: bool = False
    group_id: str | None = None


class EmployeeUpdate(Strict):
    name: Name
    enabled: bool
    bot_allowed: bool
    group_allowed: bool
    group_id: str | None = None


class TemplateInput(Strict):
    name: Name
    kind: Kind
    soul: Annotated[str, Field(min_length=1, max_length=16000)]
    language: Literal["en", "zh-CN"] = "zh-CN"
    expected_version: int | None = None


class NodeInput(Strict):
    name: Name
    connection_ref: SourceId


class GroupInput(Strict):
    name: Name
    kind: Kind
    template_id: str
    node_id: str


class RoomInput(Strict):
    name: Name
    account_id: SourceId
    conversation_id: SourceId
    enabled: bool = True
    group_id: str | None = None


class RoomUpdate(Strict):
    name: Name
    enabled: bool
    group_id: str | None = None


class DeriveInput(Strict):
    kind: Kind
    subject_id: str

    @model_validator(mode="after")
    def valid_subject(self):
        if self.kind == "employee":
            UUID(self.subject_id)
        elif (
            not self.subject_id.isascii()
            or not self.subject_id.isdigit()
            or not 0 < int(self.subject_id) < 2**63
        ):
            raise ValueError("group subject must be a positive database identity")
        return self


class TestMessage(Strict):
    account_id: SourceId
    sender_id: SourceId
    conversation_id: SourceId
    kind: Literal["private", "group"]
    mentioned: bool = True
    event_id: SourceId
    text: Annotated[str, Field(min_length=1, max_length=8000)]
