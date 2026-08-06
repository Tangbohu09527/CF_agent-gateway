from enum import StrEnum


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class DeliveryAttemptStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
