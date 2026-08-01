from __future__ import annotations

from dataclasses import dataclass

from cf_agent_gateway.access import AuthorizationDecision, ConversationType, RiskLevel
from cf_agent_gateway.admission.enums import AdmissionReason, SenderType


@dataclass(frozen=True, slots=True)
class AdmissionCandidate:
    message_id: int
    source: str
    source_account_id: str
    conversation_id: str
    conversation_type: ConversationType | str
    sender_type: SenderType | str
    sender_id: str | None
    is_self: bool
    is_mentioned: bool | None
    message_type: str
    requested_scope: frozenset[str]
    requested_skill_ids: frozenset[str]
    risk_level: RiskLevel | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "conversation_type", ConversationType(self.conversation_type))
        object.__setattr__(self, "sender_type", SenderType(self.sender_type))
        object.__setattr__(self, "requested_scope", frozenset(self.requested_scope))
        object.__setattr__(self, "requested_skill_ids", frozenset(self.requested_skill_ids))
        object.__setattr__(self, "risk_level", RiskLevel(self.risk_level))


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    message_id: int
    admitted: bool
    should_create_task: bool
    reason: AdmissionReason | str
    enterprise_identity_id: str | None = None
    workspace_id: str | None = None
    ai_thread_id: str | None = None
    authorization: AuthorizationDecision | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", AdmissionReason(self.reason))
