from __future__ import annotations


class MessageError(Exception):
    """Base class for stable Message Store domain errors."""

    code = "message_error"


class ConversationTypeConflictError(MessageError):
    code = "conversation_type_conflict"

    def __init__(
        self,
        *,
        source: str,
        source_account_id: str,
        conversation_id: str,
        existing_type: str,
        requested_type: str,
    ) -> None:
        self.source = source
        self.source_account_id = source_account_id
        self.conversation_id = conversation_id
        self.existing_type = existing_type
        self.requested_type = requested_type
        super().__init__("conversation type does not match the stored conversation")
