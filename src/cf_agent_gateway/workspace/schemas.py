from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cf_agent_gateway.workspace.models import ThreadType


class WorkspaceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuthorizedThreadRequest(WorkspaceSchema):
    enterprise_identity_id: str = Field(min_length=1, max_length=36)
    platform: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=255)
    physical_conversation_id: str = Field(min_length=1, max_length=255)
    conversation_type: ThreadType
    sender_id: str = Field(min_length=1, max_length=255)


class HermesThreadBinding(WorkspaceSchema):
    ai_thread_id: str = Field(min_length=1, max_length=36)
    hermes_thread_id: str | None = Field(default=None, min_length=1, max_length=255)
