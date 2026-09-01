from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from cf_agent_gateway.context.errors import (
    ContextAccessDeniedError,
    ContextValidationError,
)
from cf_agent_gateway.context.models import (
    ContextEntry,
    ContextEntryKind,
    ContextTimeline,
    ContextTimelineMessage,
)
from cf_agent_gateway.context.provider import (
    AuthorizedContextProvider,
    create_context_provider,
)
from cf_agent_gateway.hermes.models import HERMES_CONTEXT_TOOL_NAMES

DEFAULT_RECENT_CONTEXT_TURNS = 20
MAX_RECENT_CONTEXT_TURNS = 100
CONTEXT_READ_TOOL_NAME, CONTEXT_SEARCH_TOOL_NAME = HERMES_CONTEXT_TOOL_NAMES


class ContextTool:
    """Hermes adapter bound to one Gateway-authorized identity and AI thread."""

    def __init__(self, provider: AuthorizedContextProvider) -> None:
        if not isinstance(provider, AuthorizedContextProvider):
            raise TypeError("provider must be an AuthorizedContextProvider")
        self._provider = provider
        self._thread_id = provider.thread_id
        self._enterprise_identity_id = provider.enterprise_identity_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def enterprise_identity_id(self) -> str:
        return self._enterprise_identity_id

    @property
    def available_tools(self) -> tuple[str, str]:
        return HERMES_CONTEXT_TOOL_NAMES

    def invoke(
        self,
        tool_name: str,
        *,
        thread_id: str,
        identity: str,
        query: str | None = None,
        limit: int | None = None,
        occurred_at_gte: datetime | None = None,
        occurred_at_lt: datetime | None = None,
    ) -> ContextTimeline:
        self._authorize_invocation(thread_id=thread_id, identity=identity)
        normalized_name = _required_text(tool_name, "tool_name")
        if normalized_name == CONTEXT_READ_TOOL_NAME:
            if query is not None:
                raise ContextValidationError("context.read does not accept query")
            if limit is None and occurred_at_gte is None and occurred_at_lt is None:
                return self.read_current_thread()
            return self.read_recent_messages(
                limit=DEFAULT_RECENT_CONTEXT_TURNS if limit is None else limit,
                occurred_at_gte=occurred_at_gte,
                occurred_at_lt=occurred_at_lt,
            )
        if normalized_name == CONTEXT_SEARCH_TOOL_NAME:
            if limit is not None or occurred_at_gte is not None or occurred_at_lt is not None:
                raise ContextValidationError(
                    "context.search does not accept limit or occurred_at range"
                )
            if query is None:
                raise ContextValidationError("context.search requires query")
            return self.search_thread_context(query)
        raise ContextValidationError("unsupported context tool")

    def _authorize_invocation(self, *, thread_id: str, identity: str) -> None:
        normalized_thread_id = _required_text(thread_id, "thread_id")
        normalized_identity = _required_text(identity, "identity")
        if (
            normalized_thread_id != self._thread_id
            or normalized_identity != self._enterprise_identity_id
        ):
            raise ContextAccessDeniedError(normalized_thread_id)

    def read_current_thread(self) -> ContextTimeline:
        entries = self._provider_call(lambda: self._provider.read(self._thread_id))
        return self._timeline(entries)

    def read_recent_messages(
        self,
        limit: int = DEFAULT_RECENT_CONTEXT_TURNS,
        *,
        occurred_at_gte: datetime | None = None,
        occurred_at_lt: datetime | None = None,
    ) -> ContextTimeline:
        normalized_limit = _recent_turn_limit(limit)
        if (occurred_at_gte is None) != (occurred_at_lt is None):
            raise ContextValidationError(
                "occurred_at_gte and occurred_at_lt must be provided together"
            )
        if occurred_at_gte is None or occurred_at_lt is None:
            entries = self._provider_call(
                lambda: self._provider.read_recent(
                    self._thread_id,
                    limit=normalized_limit,
                )
            )
        else:
            entries = self._provider_call(
                lambda: self._provider.read_range(
                    self._thread_id,
                    occurred_at_gte=occurred_at_gte,
                    occurred_at_lt=occurred_at_lt,
                )
            )
        selected_message_ids: set[int] = set()
        for entry in reversed(entries):
            selected_message_ids.add(entry.message_id)
            if len(selected_message_ids) == normalized_limit:
                break
        return self._timeline(
            tuple(entry for entry in entries if entry.message_id in selected_message_ids)
        )

    def search_thread_context(self, query: str) -> ContextTimeline:
        normalized_query = _required_text(query, "query")
        entries = self._provider_call(
            lambda: self._provider.search(self._thread_id, normalized_query)
        )
        return self._timeline(entries)

    def _provider_call(
        self,
        operation: Callable[[], tuple[ContextEntry, ...]],
    ) -> tuple[ContextEntry, ...]:
        try:
            return operation()
        except (ContextAccessDeniedError, ContextValidationError):
            raise
        except Exception:
            raise ContextAccessDeniedError(self._thread_id) from None

    def _timeline(self, entries: tuple[ContextEntry, ...]) -> ContextTimeline:
        if any(
            not isinstance(entry, ContextEntry) or entry.thread_id != self._thread_id
            for entry in entries
        ):
            raise ContextAccessDeniedError(self._thread_id)
        return ContextTimeline(
            thread_id=self._thread_id,
            messages=tuple(_timeline_message(entry) for entry in entries),
        )


def create_context_tool(
    session: Session,
    *,
    enterprise_identity_id: str,
    thread_id: str,
    allowed: bool = True,
) -> ContextTool:
    """Create one invocation-scoped tool from a Gateway-issued context grant."""

    provider = create_context_provider(
        session,
        enterprise_identity_id=enterprise_identity_id,
        thread_id=thread_id,
        allowed=allowed,
    )
    return ContextTool(provider)


def _timeline_message(entry: ContextEntry) -> ContextTimelineMessage:
    role = "user" if entry.kind is ContextEntryKind.MESSAGE else "assistant"
    return ContextTimelineMessage(
        role=role,
        kind=entry.kind,
        content=entry.content,
        occurred_at=entry.occurred_at,
        received_at=entry.received_at,
        created_at=entry.created_at,
        message_id=entry.message_id,
        response_id=entry.response_id,
        part_ordinal=entry.part_ordinal,
        artifact_id=entry.artifact_id,
    )


def _recent_turn_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContextValidationError("limit must be an integer")
    if not 1 <= value <= MAX_RECENT_CONTEXT_TURNS:
        raise ContextValidationError(f"limit must be between 1 and {MAX_RECENT_CONTEXT_TURNS}")
    return value


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError(f"{field_name} must not be empty")
    return value.strip()
