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
