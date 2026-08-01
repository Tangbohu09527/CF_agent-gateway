from fastapi.testclient import TestClient
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from cf_agent_gateway.message.models import Conversation, Message


def message_event(event_id: str = "event-001", **overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": event_id,
        "source": "test-channel",
        "source_account_id": "bot-001",
        "source_message_id": "source-message-001",
        "conversation_id": "conversation-001",
        "conversation_type": "group",
        "is_mentioned": True,
        "is_self": False,
        "conversation_name": "Gateway development",
        "sender_id": "user-001",
        "sender_name": "Test User",
        "message_type": "text",
        "content": "Store this message",
        "timestamp": "2026-07-31T10:00:00+08:00",
        "reply_to_message_id": None,
        "attachments": [
            {
                "filename": "design.txt",
                "file_type": "document",
                "mime_type": "text/plain",
                "file_size": 128,
                "storage_path": "attachments/design.txt",
                "hash": "sha256:example",
            }
        ],
    }
    event.update(overrides)
    return event


def conversation_messages_path(
    *,
    source: str = "test-channel",
    source_account_id: str = "bot-001",
    conversation_id: str = "conversation-001",
) -> str:
    return (
        f"/sources/{source}/accounts/{source_account_id}/conversations/{conversation_id}/messages"
    )


def test_create_message_saves_and_returns_source_envelope(client: TestClient) -> None:
    create_response = client.post("/internal/messages", json=message_event())

    assert create_response.status_code == 201
    message_id = create_response.json()["id"]

    response = client.get(f"/messages/{message_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "test-channel"
    assert body["source_account_id"] == "bot-001"
    assert body["conversation_type"] == "group"
    assert body["is_mentioned"] is True
    assert body["is_self"] is False


def test_get_message_and_scoped_conversation_messages(client: TestClient) -> None:
    message_id = client.post("/internal/messages", json=message_event()).json()["id"]

    response = client.get(f"/messages/{message_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == "event-001"
    assert body["content"] == "Store this message"
    assert body["attachments"][0]["filename"] == "design.txt"

    conversation_response = client.get(conversation_messages_path())
    assert conversation_response.status_code == 200
    assert [message["id"] for message in conversation_response.json()] == [message_id]


def test_same_conversation_id_does_not_conflict_across_accounts(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    second = client.post(
        "/internal/messages",
        json=message_event(
            "event-002",
            source_account_id="bot-002",
            source_message_id="source-message-002",
        ),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_event_id_is_idempotent(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    duplicate_event = message_event(
        source_message_id="different-source-message",
        content="This must not overwrite the stored message",
    )

    second = client.post("/internal/messages", json=duplicate_event)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()

    messages = client.get(conversation_messages_path()).json()
    assert len(messages) == 1
    assert messages[0]["source_message_id"] == "source-message-001"
    assert messages[0]["content"] == "Store this message"


def test_source_message_identity_is_idempotent_across_event_ids(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    duplicate_message = message_event(
        "event-002",
        content="This must not overwrite the stored message",
    )

    second = client.post("/internal/messages", json=duplicate_message)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()

    messages = client.get(conversation_messages_path()).json()
    assert len(messages) == 1
    assert messages[0]["event_id"] == "event-001"
    assert messages[0]["content"] == "Store this message"


def test_same_source_message_id_does_not_conflict_across_accounts(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    second = client.post(
        "/internal/messages",
        json=message_event("event-002", source_account_id="bot-002"),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_group_message_stores_explicit_mention_values(client: TestClient) -> None:
    mentioned = client.post("/internal/messages", json=message_event())
    not_mentioned = client.post(
        "/internal/messages",
        json=message_event(
            "event-002",
            source_message_id="source-message-002",
            is_mentioned=False,
        ),
    )

    mentioned_body = client.get(f"/messages/{mentioned.json()['id']}").json()
    not_mentioned_body = client.get(f"/messages/{not_mentioned.json()['id']}").json()

    assert mentioned_body["is_mentioned"] is True
    assert not_mentioned_body["is_mentioned"] is False


def test_missing_group_mention_is_normalized_to_false(client: TestClient) -> None:
    event = message_event()
    del event["is_mentioned"]

    created = client.post("/internal/messages", json=event)

    assert created.status_code == 201
    body = client.get(f"/messages/{created.json()['id']}").json()
    assert body["is_mentioned"] is False


def test_private_message_stores_null_mention(client: TestClient) -> None:
    event = message_event(conversation_type="private", is_mentioned=None)

    created = client.post("/internal/messages", json=event)

    assert created.status_code == 201
    body = client.get(f"/messages/{created.json()['id']}").json()
    assert body["conversation_type"] == "private"
    assert body["is_mentioned"] is None


def test_private_message_rejects_non_null_mention(client: TestClient) -> None:
    response = client.post(
        "/internal/messages",
        json=message_event(conversation_type="private", is_mentioned=False),
    )

    assert response.status_code == 422


def test_self_message_is_still_saved(client: TestClient) -> None:
    created = client.post("/internal/messages", json=message_event(is_self=True))

    assert created.status_code == 201
    message_id = created.json()["id"]
    body = client.get(f"/messages/{message_id}").json()
    assert body["is_self"] is True

    messages = client.get(conversation_messages_path()).json()
    assert [message["id"] for message in messages] == [message_id]


def test_conversation_query_is_isolated_by_source_and_account(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    second = client.post(
        "/internal/messages",
        json=message_event("event-002", source_account_id="bot-002"),
    )
    third = client.post(
        "/internal/messages",
        json=message_event("event-003", source="other-channel"),
    )

    first_messages = client.get(conversation_messages_path()).json()
    second_messages = client.get(conversation_messages_path(source_account_id="bot-002")).json()
    third_messages = client.get(conversation_messages_path(source="other-channel")).json()

    assert [message["id"] for message in first_messages] == [first.json()["id"]]
    assert [message["id"] for message in second_messages] == [second.json()["id"]]
    assert [message["id"] for message in third_messages] == [third.json()["id"]]


def test_ambiguous_conversation_route_is_removed(client: TestClient) -> None:
    response = client.get("/conversations/conversation-001/messages")

    assert response.status_code == 404


def test_message_model_uses_account_scoped_constraints() -> None:
    conversation_unique_keys = {
        tuple(column.name for column in constraint.columns)
        for constraint in Conversation.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    message_unique_keys = {
        tuple(column.name for column in constraint.columns)
        for constraint in Message.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    conversation_foreign_keys = [
        constraint
        for constraint in Message.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]

    assert ("source", "source_account_id", "conversation_id") in conversation_unique_keys
    assert ("event_id",) in message_unique_keys
    assert (
        "source",
        "source_account_id",
        "conversation_id",
        "source_message_id",
    ) in message_unique_keys
    assert len(conversation_foreign_keys) == 1
    assert tuple(element.parent.name for element in conversation_foreign_keys[0].elements) == (
        "source",
        "source_account_id",
        "conversation_id",
    )
    assert tuple(element.target_fullname for element in conversation_foreign_keys[0].elements) == (
        "conversations.source",
        "conversations.source_account_id",
        "conversations.conversation_id",
    )
