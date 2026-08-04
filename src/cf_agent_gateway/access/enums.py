from enum import StrEnum


class IdentityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"
    UNRESOLVED = "unresolved"


class ConversationType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


class RiskLevel(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class Decision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


class ReasonCode(StrEnum):
    ALLOWED = "allowed"
    INVALID_CONVERSATION_TYPE = "invalid_conversation_type"
    INVALID_CONVERSATION_FACTS = "invalid_conversation_facts"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    IDENTITY_DISABLED = "identity_disabled"
    USER_NOT_ALLOWED = "user_not_allowed"
    BOT_NOT_MENTIONED = "bot_not_mentioned"
    RISK_NOT_ALLOWED = "risk_not_allowed"
    PERMISSION_SCOPE_EMPTY = "permission_scope_empty"
    SKILL_NOT_ALLOWED = "skill_not_allowed"
