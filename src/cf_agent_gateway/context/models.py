from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ContextEntryKind(StrEnum):
    MESSAGE = "message"
    ASSISTANT_RESPONSE = "assistant_response"
    ARTIFACT_REFERENCE = "artifact_reference"


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """One immutable, explicitly-created summary of a thread timeline prefix."""

    thread_id: str
    snapshot_version: int
    summary: str
    covered_until: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """One persisted fact in a thread timeline."""

    thread_id: str
    kind: ContextEntryKind
    occurred_at: datetime
    received_at: datetime
    created_at: datetime
    message_id: int
    response_id: str | None = None
    part_ordinal: int | None = None
    content: str | None = None
    artifact_id: str | None = None
    dispatch_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ContextEntryKind(self.kind))


class ContextTimelineMessage(BaseModel):
    """JSON-serializable timeline item returned to a Hermes context tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    kind: ContextEntryKind
    content: str | None = None
    occurred_at: datetime
    received_at: datetime
    created_at: datetime
    message_id: int
    response_id: str | None = None
    part_ordinal: int | None = None
    artifact_id: str | None = None


class ContextTimeline(BaseModel):
    """Structured, serializable context response for one exact AI thread."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str
    messages: tuple[ContextTimelineMessage, ...]
