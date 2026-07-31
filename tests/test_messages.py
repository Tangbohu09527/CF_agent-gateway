from fastapi.testclient import TestClient


def message_event(event_id: str = "event-001") -> dict[str, object]:
    return {
        "event_id": event_id,
        "source": "test-channel",
        "source_message_id": "source-message-001",
        "conversation_id": "conversation-001",
        "conversation_type": "group",
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


def test_create_message(client: TestClient) -> None:
    response = client.post("/internal/messages", json=message_event())

    assert response.status_code == 201
    assert response.json() == {"id": 1}


def test_get_message_and_conversation_messages(client: TestClient) -> None:
    message_id = client.post("/internal/messages", json=message_event()).json()["id"]

    response = client.get(f"/messages/{message_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == "event-001"
    assert body["content"] == "Store this message"
    assert body["attachments"][0]["filename"] == "design.txt"

    conversation_response = client.get("/conversations/conversation-001/messages")
    assert conversation_response.status_code == 200
    assert [message["id"] for message in conversation_response.json()] == [message_id]


def test_event_id_is_idempotent(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    duplicate_event = message_event()
    duplicate_event["content"] = "This must not overwrite the stored message"

    second = client.post("/internal/messages", json=duplicate_event)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()

    messages = client.get("/conversations/conversation-001/messages").json()
    assert len(messages) == 1
    assert messages[0]["content"] == "Store this message"
