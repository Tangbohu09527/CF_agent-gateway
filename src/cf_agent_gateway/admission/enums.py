from enum import StrEnum


class SenderType(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"


class AdmissionReason(StrEnum):
    ALLOWED = "allowed"
    SELF_MESSAGE = "self_message"
    SYSTEM_MESSAGE = "system_message"
    SENDER_UNRESOLVED = "sender_unresolved"
    ACCESS_DENIED = "access_denied"
    LEGACY_UNRESOLVED = "legacy_unresolved"


class AdmissionOutcomeState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


class AdmissionDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNRESOLVED = "unresolved"


class AdmissionEvidenceOrigin(StrEnum):
    RUNTIME = "runtime"
    LEGACY_DISPATCH = "legacy_dispatch"
    LEGACY_UNRESOLVED = "legacy_unresolved"
