from __future__ import annotations

import re
from datetime import datetime
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cf_agent_gateway.message.schemas import MessageResponse
from cf_agent_gateway.task.model import (
    HermesDispatchRecoveryAction,
    HermesDispatchStatus,
)

_SECRET_PATTERN = re.compile(
    r"(?i)(?:\bauthorization\b|\bbearer\b|\btoken\b|\bsecret\b|"
    r"\bpassword\b|\bpasswd\b|\bapi[ _-]?key\b)"
)


class AdminPage[ItemT](BaseModel):
    items: list[ItemT]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class AdminConversationItem(BaseModel):
    id: int
    source: str
    source_account_id: str
    conversation_id: str
    conversation_type: str
    conversation_name: str | None
    message_count: int = Field(ge=0)
    first_message_at: datetime | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminMessageItem(MessageResponse):
    identity_id: str | None = None
    ai_thread_id: str | None = None
    dispatch_status: str | None = None
    response_id: str | None = None
    response_status: str | None = None


class AdminResponsePartItem(BaseModel):
    ordinal: int = Field(ge=0)
    part_type: str
    text: str | None
    artifact_id: str | None


class AdminTimelineDeliveryItem(BaseModel):
    id: int
    status: str
    attempt_count: int = Field(ge=0)
    completed_at: datetime | None
    last_error_code: str | None


class AdminThreadTimelineItem(AdminMessageItem):
    response_parts: list[AdminResponsePartItem] = Field(default_factory=list)
    deliveries: list[AdminTimelineDeliveryItem] = Field(default_factory=list)


class AdminThreadSourceBindingItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    account_id: str
    physical_conversation_id: str
    sender_id: str | None
    created_at: datetime
    updated_at: datetime


class AdminThreadDeliverySummary(BaseModel):
    total: int = Field(ge=0)
    queued: int = Field(ge=0)
    delivering: int = Field(ge=0)
    delivered: int = Field(ge=0)
    failed: int = Field(ge=0)
    uncertain: int = Field(ge=0)


class AdminThreadDetail(BaseModel):
    id: str
    workspace_id: str
    agent_profile_id: str | None
    thread_type: str
    thread_policy: str | None
    thread_key: str
    status: str
    hermes_thread_id: str | None
    created_at: datetime
    updated_at: datetime
    last_active_at: datetime
    source_bindings: list[AdminThreadSourceBindingItem]
    timeline: AdminPage[AdminThreadTimelineItem]
    delivery_summary: AdminThreadDeliverySummary


class AdminDeliveryItem(BaseModel):
    id: int
    response_id: str
    message_id: int
    identity_id: str | None
    workspace_id: str
    ai_thread_id: str
    channel: str
    account_id: str
    conversation_id: str
    status: str
    next_part_ordinal: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    available_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class AdminDispatchRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operator: str = Field(min_length=1, max_length=128)
    reference: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=1024)

    @field_validator("operator", "reference", "reason", mode="before")
    @classmethod
    def validate_recovery_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if any(category(character) == "Cc" for character in value):
            raise ValueError("recovery metadata contains invalid characters")
        if _SECRET_PATTERN.search(value):
            raise ValueError("recovery metadata may not contain secret material")
        return value


class AdminDispatchInspection(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dispatch_record_id: int
    status: HermesDispatchStatus
    message_id: int
    ai_thread_id: str
    attempt_count: int = Field(ge=0)
    last_error_code: str | None
    has_dispatch_response: bool
    has_hermes_response: bool
    has_delivery: bool
    blocks_following_dispatch: bool
    created_at: datetime
    updated_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    lease_expires_at: datetime | None


class AdminDispatchRecoveryResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dispatch_record_id: int
    action: HermesDispatchRecoveryAction
    status: HermesDispatchStatus
    audit_id: int
    idempotent: bool
