from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from cf_agent_gateway.context import (
    AuthorizedContextProvider,
    ContextAccessDeniedError,
    ContextEntry,
    ContextEntryKind,
    ContextRuntimeError,
    ContextTool,
    ContextValidationError,
    ThreadContextAccessPolicy,
)

THREAD_ID = "thread-context-tool"
IDENTITY_ID = "identity-context-tool"
PERSISTED_AT = datetime(2026, 8, 10, 10, tzinfo=UTC)


def entry(
    *,
    message_id: int,
    kind: ContextEntryKind,
    content: str | None = None,
    artifact_id: str | None = None,
    thread_id: str = THREAD_ID,
) -> ContextEntry:
    return ContextEntry(
        thread_id=thread_id,
        kind=kind,
        message_id=message_id,
        content=content,
        artifact_id=artifact_id,
        occurred_at=PERSISTED_AT + timedelta(minutes=message_id),
        received_at=PERSISTED_AT + timedelta(minutes=message_id),
        created_at=PERSISTED_AT + timedelta(minutes=message_id),
        response_id=f"response-{message_id}" if kind is not ContextEntryKind.MESSAGE else None,
        part_ordinal=0 if kind is not ContextEntryKind.MESSAGE else None,
    )


def context_tool(storage: object) -> ContextTool:
    provider = AuthorizedContextProvider(
        storage,  # type: ignore[arg-type]
        access_policy=ThreadContextAccessPolicy(
            enterprise_identity_id=IDENTITY_ID,
            thread_id=THREAD_ID,
        ),
        enterprise_identity_id=IDENTITY_ID,
        thread_id=THREAD_ID,
    )
    return ContextTool(provider)


def test_context_tool_returns_structured_current_recent_and_search_timelines() -> None:
    timeline = (
        entry(message_id=1, kind=ContextEntryKind.MESSAGE, content="first question"),
        entry(
            message_id=1,
            kind=ContextEntryKind.ASSISTANT_RESPONSE,
            content="first answer",
        ),
        entry(
            message_id=1,
            kind=ContextEntryKind.ARTIFACT_REFERENCE,
            artifact_id="artifact-first",
        ),
        entry(message_id=2, kind=ContextEntryKind.MESSAGE, content="second question"),
        entry(
            message_id=2,
            kind=ContextEntryKind.ASSISTANT_RESPONSE,
            content="second answer",
        ),
    )
    provider = Mock()
    provider.read.return_value = timeline
    provider.read_recent.return_value = timeline
    provider.search.return_value = (timeline[1],)
    tool = context_tool(provider)

    current = tool.read_current_thread()
    recent = tool.read_recent_messages(limit=1)
    matches = tool.search_thread_context(" first answer ")

    assert current.thread_id == THREAD_ID
    assert tool.thread_id == THREAD_ID
    assert tool.enterprise_identity_id == IDENTITY_ID
    assert [message.role for message in current.messages] == [
        "user",
        "assistant",
        "assistant",
        "user",
        "assistant",
    ]
    assert current.messages[0].content == "first question"
    assert current.messages[2].artifact_id == "artifact-first"
    assert current.messages[0].occurred_at == PERSISTED_AT + timedelta(minutes=1)
    assert [message.message_id for message in recent.messages] == [2, 2]
    assert [message.content for message in matches.messages] == ["first answer"]
    assert current.model_dump(mode="json")["thread_id"] == THREAD_ID
    provider.read.assert_called_once_with(THREAD_ID)
    provider.read_recent.assert_called_once_with(THREAD_ID, limit=1)
    provider.search.assert_called_once_with(THREAD_ID, "first answer")


def test_context_tool_invokes_advertised_context_read_and_search_names() -> None:
    storage = Mock()
    timeline = (
        entry(message_id=1, kind=ContextEntryKind.MESSAGE, content="named question"),
        entry(
            message_id=1,
            kind=ContextEntryKind.ASSISTANT_RESPONSE,
            content="named answer",
        ),
    )
    storage.read_recent.return_value = timeline
    storage.search.return_value = (timeline[1],)
    tool = context_tool(storage)

    recent = tool.invoke(
        "context.read",
        thread_id=THREAD_ID,
        identity=IDENTITY_ID,
        limit=1,
    )
    matches = tool.invoke(
        "context.search",
        thread_id=THREAD_ID,
        identity=IDENTITY_ID,
        query="named answer",
    )

    assert tool.available_tools == ("context.read", "context.search")
    assert [message.content for message in recent.messages] == [
        "named question",
        "named answer",
    ]
    assert [message.content for message in matches.messages] == ["named answer"]
    storage.read_recent.assert_called_once_with(THREAD_ID, limit=1)
    storage.search.assert_called_once_with(THREAD_ID, "named answer")


@pytest.mark.parametrize(
    ("thread_id", "identity"),
    [
        ("thread-other", IDENTITY_ID),
        (THREAD_ID, "identity-other"),
    ],
)
def test_context_tool_named_invocation_rejects_wrong_binding_before_storage(
    thread_id: str,
    identity: str,
) -> None:
    storage = Mock()
    tool = context_tool(storage)

    with pytest.raises(ContextAccessDeniedError):
        tool.invoke(
            "context.read",
            thread_id=thread_id,
            identity=identity,
        )

    storage.read.assert_not_called()
    storage.read_recent.assert_not_called()
    storage.read_range.assert_not_called()
    storage.search.assert_not_called()


def test_context_tool_recent_messages_delegates_authorized_time_range() -> None:
    provider = Mock()
    provider.read_range.return_value = (
        entry(message_id=2, kind=ContextEntryKind.MESSAGE, content="bounded question"),
    )
    tool = context_tool(provider)
    range_start = PERSISTED_AT
    range_end = PERSISTED_AT + timedelta(hours=1)

    timeline = tool.read_recent_messages(
        limit=1,
        occurred_at_gte=range_start,
        occurred_at_lt=range_end,
    )

    assert [message.content for message in timeline.messages] == ["bounded question"]
    provider.read_range.assert_called_once_with(
        THREAD_ID,
        occurred_at_gte=range_start,
        occurred_at_lt=range_end,
    )
    provider.read.assert_not_called()
    provider.read_recent.assert_not_called()


@pytest.mark.parametrize("limit", [True, 0, 101, 1.5])
def test_context_tool_rejects_invalid_recent_limits(limit: object) -> None:
    provider = Mock()
    tool = context_tool(provider)

    with pytest.raises(ContextValidationError):
        tool.read_recent_messages(limit=limit)  # type: ignore[arg-type]

    provider.read.assert_not_called()
    provider.read_recent.assert_not_called()
    provider.read_range.assert_not_called()


def test_context_tool_rejects_incomplete_time_range_before_provider() -> None:
    provider = Mock()
    tool = context_tool(provider)

    with pytest.raises(ContextValidationError):
        tool.read_recent_messages(occurred_at_gte=PERSISTED_AT)

    provider.read.assert_not_called()
    provider.read_recent.assert_not_called()
    provider.read_range.assert_not_called()


def test_context_tool_denies_different_identity_before_storage() -> None:
    storage = Mock()
    provider = AuthorizedContextProvider(
        storage,
        access_policy=ThreadContextAccessPolicy(
            enterprise_identity_id="identity-owner",
            thread_id=THREAD_ID,
        ),
        enterprise_identity_id="identity-other",
        thread_id=THREAD_ID,
    )
    tool = ContextTool(provider)

    with pytest.raises(ContextAccessDeniedError) as caught:
        tool.read_current_thread()

    assert caught.value.code == "context_access_denied"
    storage.read.assert_not_called()
    storage.read_recent.assert_not_called()
    storage.read_range.assert_not_called()
    storage.search.assert_not_called()


def test_context_provider_converts_policy_failure_to_fail_closed_denial() -> None:
    storage = Mock()
    policy = Mock()
    policy.allows.side_effect = RuntimeError("policy backend unavailable")
    provider = AuthorizedContextProvider(
        storage,
        access_policy=policy,
        enterprise_identity_id=IDENTITY_ID,
        thread_id=THREAD_ID,
    )
    tool = ContextTool(provider)

    with pytest.raises(ContextAccessDeniedError) as caught:
        tool.read_current_thread()

    assert caught.value.code == "context_access_denied"
    assert caught.value.__cause__ is None
    storage.read.assert_not_called()
    storage.read_recent.assert_not_called()
    storage.read_range.assert_not_called()
    storage.search.assert_not_called()


def test_context_tool_converts_provider_failure_to_fail_closed_denial() -> None:
    provider = Mock()
    provider.read.side_effect = ContextRuntimeError("database details must not escape")
    tool = context_tool(provider)

    with pytest.raises(ContextAccessDeniedError) as caught:
        tool.read_current_thread()

    assert caught.value.code == "context_access_denied"
    assert caught.value.__cause__ is None
    assert "database details" not in str(caught.value)


def test_context_tool_rejects_cross_thread_provider_output() -> None:
    provider = Mock()
    provider.read.return_value = (
        entry(
            message_id=1,
            kind=ContextEntryKind.MESSAGE,
            content="other thread secret",
            thread_id="thread-other",
        ),
    )
    tool = context_tool(provider)

    with pytest.raises(ContextAccessDeniedError):
        tool.read_current_thread()
