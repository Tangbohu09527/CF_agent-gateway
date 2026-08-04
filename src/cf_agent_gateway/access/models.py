from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cf_agent_gateway.access.enums import (
    ConversationType,
    Decision,
    IdentityStatus,
    ReasonCode,
    RiskLevel,
)


def _immutable_strings(values: frozenset[str]) -> frozenset[str]:
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class IdentityFacts:
    identity_resolved: bool
    enterprise_identity_id: str | None
    identity_status: IdentityStatus
    user_allowed: bool
    user_permission_scope: frozenset[str] = field(default_factory=frozenset)
    user_allowed_skills: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity_status", IdentityStatus(self.identity_status))
        object.__setattr__(
            self, "user_permission_scope", _immutable_strings(self.user_permission_scope)
        )
        object.__setattr__(
            self, "user_allowed_skills", _immutable_strings(self.user_allowed_skills)
        )


@dataclass(frozen=True, slots=True)
class ConversationFacts:
    """Carry conversation shape for context routing, never caller permission."""

    conversation_type: ConversationType | str
    is_mentioned: bool | None = None


@dataclass(frozen=True, slots=True)
class RequestFacts:
    requested_scope: frozenset[str]
    requested_skill_ids: frozenset[str]
    risk_level: RiskLevel

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_scope", _immutable_strings(self.requested_scope))
        object.__setattr__(
            self, "requested_skill_ids", _immutable_strings(self.requested_skill_ids)
        )
        object.__setattr__(self, "risk_level", RiskLevel(self.risk_level))


@dataclass(frozen=True, slots=True)
class GatewayPolicyFacts:
    system_permission_scope: frozenset[str]
    system_allowed_skills: frozenset[str]
    allowed_risk_levels: frozenset[RiskLevel]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "system_permission_scope", _immutable_strings(self.system_permission_scope)
        )
        object.__setattr__(
            self, "system_allowed_skills", _immutable_strings(self.system_allowed_skills)
        )
        object.__setattr__(
            self,
            "allowed_risk_levels",
            frozenset(RiskLevel(level) for level in self.allowed_risk_levels),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    decision: Decision
    reason_code: ReasonCode
    enterprise_identity_id: str | None
    user_allowed: bool
    is_mentioned: bool | None
    permission_scope: frozenset[str]
    allowed_skills: frozenset[str]
    risk_level: RiskLevel

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", Decision(self.decision))
        object.__setattr__(self, "reason_code", ReasonCode(self.reason_code))
        object.__setattr__(self, "permission_scope", _immutable_strings(self.permission_scope))
        object.__setattr__(self, "allowed_skills", _immutable_strings(self.allowed_skills))
        object.__setattr__(self, "risk_level", RiskLevel(self.risk_level))

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, serialization-ready representation."""
        return {
            "allowed": self.allowed,
            "decision": self.decision.value,
            "reason_code": self.reason_code.value,
            "enterprise_identity_id": self.enterprise_identity_id,
            "user_allowed": self.user_allowed,
            "is_mentioned": self.is_mentioned,
            "permission_scope": sorted(self.permission_scope),
            "allowed_skills": sorted(self.allowed_skills),
            "risk_level": self.risk_level.value,
        }
