from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from cf_agent_gateway.access.enums import RiskLevel
from cf_agent_gateway.access.policy_errors import InvalidPolicyWindowError
from cf_agent_gateway.access.policy_models import DEFAULT_GATEWAY_POLICY_KEY

PolicyValue = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class AccessPolicySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TimeBoundPolicySchema(AccessPolicySchema):
    enabled: bool = True
    permission_scope: frozenset[PolicyValue] = Field(default_factory=frozenset)
    allowed_skills: frozenset[PolicyValue] = Field(default_factory=frozenset)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator("valid_from", "valid_until")
    @classmethod
    def normalize_policy_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_policy_window(self) -> Self:
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until < self.valid_from
        ):
            raise InvalidPolicyWindowError
        return self


class UserAccessPolicyUpsert(TimeBoundPolicySchema):
    enterprise_identity_id: str = Field(min_length=1, max_length=36)


class GroupAccessPolicyUpsert(TimeBoundPolicySchema):
    source: str = Field(min_length=1, max_length=64)
    source_account_id: str = Field(min_length=1, max_length=255)
    conversation_id: str = Field(min_length=1, max_length=255)


class GatewayAccessPolicyUpsert(AccessPolicySchema):
    policy_key: Literal["default"] = DEFAULT_GATEWAY_POLICY_KEY
    permission_scope: frozenset[PolicyValue] = Field(default_factory=frozenset)
    allowed_skills: frozenset[PolicyValue] = Field(default_factory=frozenset)
    allowed_risk_levels: frozenset[RiskLevel] = Field(default_factory=frozenset)
    enabled: bool = True
