from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from cf_agent_gateway.workspace.models import ThreadPolicy, ThreadType


class WorkspaceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuthorizedThreadRequest(WorkspaceSchema):
    enterprise_identity_id: str = Field(min_length=1, max_length=36)
    platform: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=255)
    physical_conversation_id: str = Field(min_length=1, max_length=255)
    conversation_type: ThreadType
    sender_id: str = Field(min_length=1, max_length=255)


class ThreadResolverInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        from_attributes=True,
        frozen=True,
        populate_by_name=True,
        coerce_numbers_to_str=True,
    )


class ConversationRef(ThreadResolverInput):
    conversation_id: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("conversation_id", "physical_conversation_id", "id"),
    )
    conversation_type: ThreadType


class SourceAccountRef(ThreadResolverInput):
    platform: str = Field(
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("platform", "source"),
    )
    account_id: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("account_id", "source_account_id", "id"),
    )


class SenderIdentityRef(ThreadResolverInput):
    identity_id: str = Field(
        min_length=1,
        max_length=36,
        validation_alias=AliasChoices("identity_id", "enterprise_identity_id", "id"),
    )


class AgentProfileRef(ThreadResolverInput):
    profile_id: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("profile_id", "id", "profile_key"),
    )
    revision: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("revision", "profile_revision", "current_revision"),
    )


class ThreadResolutionRequest(ThreadResolverInput):
    conversation: ConversationRef
    source_account: SourceAccountRef
    sender_identity: SenderIdentityRef
    agent_profile: AgentProfileRef
    thread_policy: ThreadPolicy


class HermesThreadBinding(WorkspaceSchema):
    ai_thread_id: str = Field(min_length=1, max_length=36)
    hermes_thread_id: str | None = Field(default=None, min_length=1, max_length=255)
