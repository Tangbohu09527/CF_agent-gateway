from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cf_agent_gateway.message.schemas import (
    MAX_ATTACHMENT_FILE_SIZE,
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_MESSAGE_CONTENT_LENGTH,
    MAX_RAW_TYPE,
    MessageEvent,
)
from cf_agent_gateway.message.store import (
    DEFAULT_CONVERSATION_MESSAGE_LIMIT,
    MessageStore,
)

CONVERSATION_PATH = "/sources/test-channel/accounts/bot-001/conversations/conversation-001/messages"


def message_event(index: int = 1, **overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": f"event-{index:03d}",
        "source": "test-channel",
        "source_account_id": "bot-001",
        "source_message_id": f"source-message-{index:03d}",
        "conversation_id": "conversation-001",
        "conversation_type": "private",
        "is_mentioned": None,
        "is_self": False,
        "sender_type": "human",
        "sender_id": "user-001",
        "message_type": "text",
        "raw_type": 1,
        "content": f"message {index}",
        "timestamp": "2026-08-21T10:00:00+08:00",
        "attachments": [],
    }
    event.update(overrides)
    return event


def attachment(**overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "filename": "message.txt",
        "file_type": "document",
        "mime_type": "text/plain",
        "file_size": 128,
        "storage_path": "attachments/message.txt",
        "hash": "sha256:example",
    }
    metadata.update(overrides)
    return metadata


def test_conversation_query_applies_default_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def list_for_conversation(self: MessageStore, **kwargs: Any) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(MessageStore, "list_for_conversation", list_for_conversation)

    response = client.get(CONVERSATION_PATH)

    assert response.status_code == 200
    assert captured["limit"] == DEFAULT_CONVERSATION_MESSAGE_LIMIT
    assert captured["offset"] == 0


def test_conversation_query_supports_bounded_pagination(client: TestClient) -> None:
    ids = [
        client.post("/internal/messages", json=message_event(index)).json()["id"]
        for index in range(1, 4)
    ]

    response = client.get(f"{CONVERSATION_PATH}?limit=2&offset=1")

    assert response.status_code == 200
    assert [message["id"] for message in response.json()] == ids[1:]


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=101", "offset=-1", "offset=100001"],
)
def test_conversation_query_rejects_out_of_bounds_pagination(
    client: TestClient,
    query: str,
) -> None:
    response = client.get(f"{CONVERSATION_PATH}?{query}")

    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"content": "x" * (MAX_MESSAGE_CONTENT_LENGTH + 1)},
        {
            "reply_context": {
                "content": "x" * (MAX_MESSAGE_CONTENT_LENGTH + 1),
            }
        },
        {"attachments": [attachment()] * (MAX_ATTACHMENTS_PER_MESSAGE + 1)},
        {"attachments": [attachment(file_size=MAX_ATTACHMENT_FILE_SIZE + 1)]},
        {"raw_type": MAX_RAW_TYPE + 1},
    ],
)
def test_message_event_rejects_oversized_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MessageEvent.model_validate(message_event(**overrides))


def test_message_event_accepts_documented_field_boundaries() -> None:
    event = MessageEvent.model_validate(
        message_event(
            content="x" * MAX_MESSAGE_CONTENT_LENGTH,
            attachments=[attachment()] * MAX_ATTACHMENTS_PER_MESSAGE,
            raw_type=MAX_RAW_TYPE,
        )
    )

    assert len(event.content) == MAX_MESSAGE_CONTENT_LENGTH
    assert len(event.attachments) == MAX_ATTACHMENTS_PER_MESSAGE
