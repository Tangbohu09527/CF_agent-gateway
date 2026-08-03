from __future__ import annotations


class MessageIngestionError(RuntimeError):
    """Base class for message-ingestion failures."""


class PersistedMessageNotFoundError(MessageIngestionError):
    """Raised when a committed message cannot be read back by its database id."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        super().__init__(f"persisted message {message_id} could not be read back")
