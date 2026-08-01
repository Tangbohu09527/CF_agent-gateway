from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cf_agent_gateway.identity.models import IdentityStatus


class IdentitySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IdentityCreate(IdentitySchema):
    employee_id: str | None = Field(default=None, min_length=1, max_length=255)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    status: IdentityStatus = IdentityStatus.ACTIVE


class SourceIdentityKey(IdentitySchema):
    platform: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=255)
    sender_id: str = Field(min_length=1, max_length=255)


class SourceIdentityMappingCreate(SourceIdentityKey):
    enterprise_identity_id: str = Field(min_length=1, max_length=36)


class IdentityResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    DISABLED = "disabled"
    CONFLICT = "conflict"


class IdentityResolution(BaseModel):
    status: IdentityResolutionStatus
    enterprise_identity_id: str | None = None
    employee_id: str | None = None
    mapping_id: str | None = None

    @property
    def is_executable(self) -> bool:
        return self.status is IdentityResolutionStatus.RESOLVED
