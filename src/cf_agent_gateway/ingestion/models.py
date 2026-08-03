from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from cf_agent_gateway.admission import AdmissionOutcome


@dataclass(frozen=True, slots=True)
class PersistedAttachmentSnapshot:
    id: int
    message_id: int
    filename: str
    file_type: str
    mime_type: str
    file_size: int
    storage_path: str
    hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedMessageSnapshot:
    """Read-only request-resolution input copied from a committed Message row."""

    message_id: int
    event_id: str
    source: str
    source_account_id: str
    source_message_id: str
    conversation_id: str
    conversation_type: str
    is_mentioned: bool | None
    is_self: bool
    sender_type: str
    sender_id: str | None
    sender_name: str | None
    message_type: str
    raw_type: int | None
    content: str
    timestamp: datetime
    source_local_id: str | None
    source_server_id: str | None
    source_message_id_is_fallback: bool
    reply_context: Mapping[str, object] | None
    reply_to_message_id: str | None
    created_at: datetime
    attachments: tuple[PersistedAttachmentSnapshot, ...]

    def __post_init__(self) -> None:
        if self.reply_context is not None:
            copied_context = deepcopy(dict(self.reply_context))
            object.__setattr__(self, "reply_context", MappingProxyType(copied_context))
        object.__setattr__(self, "attachments", tuple(self.attachments))


@dataclass(frozen=True, slots=True)
class MessageIngestionOutcome:
    message_id: int
    message_created: bool
    admission: AdmissionOutcome
    should_create_task: bool
    workspace_id: str | None
    ai_thread_id: str | None
