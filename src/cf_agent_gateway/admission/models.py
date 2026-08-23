from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.access import AuthorizationDecision, ConversationType, RiskLevel
from cf_agent_gateway.access.policy_models import SortedStringSet
from cf_agent_gateway.admission.enums import (
    AdmissionDecision,
    AdmissionEvidenceOrigin,
    AdmissionOutcomeState,
    AdmissionReason,
    SenderType,
)
from cf_agent_gateway.database import Base


def _enum_values(enum_type: type[AdmissionOutcomeState]) -> list[str]:
    return [member.value for member in enum_type]


class MessageAdmissionOutcome(Base):
    """The single durable admission authority for one persisted message."""

    __tablename__ = "message_admission_outcomes"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_message_admission_outcome_message"),
        CheckConstraint(
            "state IN ('pending', 'completed')",
            name="ck_message_admission_outcome_state",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('allowed', 'denied', 'unresolved')",
            name="ck_message_admission_outcome_decision",
        ),
        CheckConstraint(
            "evidence_origin IN ('runtime', 'legacy_dispatch', 'legacy_unresolved')",
            name="ck_message_admission_evidence_origin",
        ),
        CheckConstraint(
            "(evidence_origin = 'runtime' AND "
            "(state = 'pending' OR decision IN ('allowed', 'denied'))) OR "
            "(evidence_origin = 'legacy_dispatch' AND "
            "state = 'completed' AND decision = 'allowed') OR "
            "(evidence_origin = 'legacy_unresolved' AND "
            "state = 'completed' AND decision = 'unresolved')",
            name="ck_message_admission_origin_decision",
        ),
        CheckConstraint(
            "routing_mode IN ('none', 'v1', 'v2', 'legacy')",
            name="ck_message_admission_routing_mode",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_message_admission_nonnegative_attempt_count",
        ),
        CheckConstraint(
            "(claim_token IS NULL AND lease_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_message_admission_claim_pair",
        ),
        CheckConstraint(
            "(state = 'pending' AND decision IS NULL "
            "AND admission_reason IS NULL AND completed_at IS NULL "
            "AND should_create_task = false AND evidence_origin = 'runtime') OR "
            "(state = 'completed' AND decision IS NOT NULL "
            "AND admission_reason IS NOT NULL AND completed_at IS NOT NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_message_admission_state_fields",
        ),
        CheckConstraint(
            "evidence_origin != 'runtime' OR "
            "(requested_scope IS NOT NULL AND requested_skill_ids IS NOT NULL "
            "AND risk_level IS NOT NULL)",
            name="ck_message_admission_runtime_request",
        ),
        CheckConstraint(
            "(decision = 'allowed' AND should_create_task = true "
            "AND enterprise_identity_id IS NOT NULL "
            "AND workspace_id IS NOT NULL AND ai_thread_id IS NOT NULL) OR "
            "(decision IN ('denied', 'unresolved') AND should_create_task = false "
            "AND workspace_id IS NULL AND ai_thread_id IS NULL) OR "
            "decision IS NULL",
            name="ck_message_admission_decision_targets",
        ),
        CheckConstraint(
            "(decision = 'allowed' AND admission_reason = 'allowed') OR "
            "(decision = 'unresolved' AND admission_reason = 'legacy_unresolved') OR "
            "(decision = 'denied' AND admission_reason NOT IN "
            "('allowed', 'legacy_unresolved')) OR decision IS NULL",
            name="ck_message_admission_decision_reason",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_message_admission_nonempty_error",
        ),
        Index(
            "ix_message_admission_state_lease",
            "state",
            "lease_expires_at",
            "message_id",
        ),
        Index(
            "ix_message_admission_decision_completed",
            "decision",
            "completed_at",
            "message_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), index=True
    )
    state: Mapped[AdmissionOutcomeState] = mapped_column(
        Enum(
            AdmissionOutcomeState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="message_admission_outcome_state",
        )
    )
    decision: Mapped[AdmissionDecision | None] = mapped_column(
        Enum(
            AdmissionDecision,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="message_admission_decision",
        ),
        nullable=True,
    )
    evidence_origin: Mapped[AdmissionEvidenceOrigin] = mapped_column(
        Enum(
            AdmissionEvidenceOrigin,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="message_admission_evidence_origin",
        )
    )
    admission_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    should_create_task: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    requested_scope: Mapped[frozenset[str] | None] = mapped_column(SortedStringSet(), nullable=True)
    requested_skill_ids: Mapped[frozenset[str] | None] = mapped_column(
        SortedStringSet(), nullable=True
    )
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    authorization_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_snapshot: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    policy_snapshot: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    gateway_policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("gateway_access_policies.id", ondelete="RESTRICT"), nullable=True
    )
    gateway_policy_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_access_policies.id", ondelete="RESTRICT"), nullable=True
    )
    user_policy_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enterprise_identity_id: Mapped[str | None] = mapped_column(
        ForeignKey("enterprise_identities.id", ondelete="RESTRICT"), nullable=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("employee_workspaces.id", ondelete="RESTRICT"), nullable=True
    )
    ai_thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_threads.id", ondelete="RESTRICT"), nullable=True
    )
    routing_mode: Mapped[str] = mapped_column(String(16), default="none", server_default="none")
    route_conversation_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="RESTRICT"), nullable=True
    )
    agent_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=True
    )
    agent_profile_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_profile_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_profile_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_type_id: Mapped[str | None] = mapped_column(
        ForeignKey("group_types.id", ondelete="RESTRICT"), nullable=True
    )
    group_type_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thread_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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
    policy_snapshot: dict[str, object] | None = None
    gateway_policy_id: str | None = None
    gateway_policy_updated_at: datetime | None = None
    user_policy_id: str | None = None
    user_policy_updated_at: datetime | None = None
    routing_mode: str = "none"
    route_conversation_record_id: int | None = None
    agent_profile_id: str | None = None
    agent_profile_key: str | None = None
    agent_profile_reference: str | None = None
    agent_profile_revision: int | None = None
    group_type_id: str | None = None
    group_type_key: str | None = None
    thread_policy: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", AdmissionReason(self.reason))
