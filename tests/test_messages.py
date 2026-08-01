from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.message.errors import ConversationTypeConflictError
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore


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
        "sender_type": "human",
        "sender_id": "user-001",
        "sender_name": "Test User",
        "message_type": "text",
        "raw_type": 1,
        "content": "Store this message",
        "timestamp": "2026-07-31T10:00:00+08:00",
        "source_local_id": "local-001",
        "source_server_id": "server-001",
        "source_message_id_is_fallback": False,
        "reply_context": {
            "source_local_id": "reply-local-001",
            "source_server_id": "reply-server-001",
            "sender_id": "user-002",
            "sender_name": "Reply Author",
            "raw_type": 1,
            "content": "Original message",
        },
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


def database_session_factory(client: TestClient) -> sessionmaker[Session]:
    return client.app.state.database_session_factory


def parsed_message_event(event_id: str = "event-001", **overrides: object) -> MessageEvent:
    return MessageEvent.model_validate(message_event(event_id, **overrides))


def conversation_snapshot(client: TestClient) -> tuple[str, str | None, int]:
    with database_session_factory(client)() as session:
        conversation = session.scalar(select(Conversation))
        assert conversation is not None
        message_count = session.scalar(select(func.count()).select_from(Message))
        return conversation.conversation_type, conversation.conversation_name, message_count


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
    assert body["sender_type"] == "human"
    assert body["raw_type"] == 1
    assert body["source_local_id"] == "local-001"
    assert body["source_server_id"] == "server-001"
    assert body["source_message_id_is_fallback"] is False
    assert body["reply_context"] == {
        "source_local_id": "reply-local-001",
        "source_server_id": "reply-server-001",
        "sender_id": "user-002",
        "sender_name": "Reply Author",
        "raw_type": 1,
        "content": "Original message",
    }
    assert body["reply_to_message_id"] is None


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
    assert conversation_response.json()[0]["sender_type"] == "human"
    assert conversation_response.json()[0]["raw_type"] == 1
    assert conversation_response.json()[0]["reply_context"]["content"] == "Original message"


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


def test_human_message_requires_sender_id(client: TestClient) -> None:
    response = client.post("/internal/messages", json=message_event(sender_id=None))

    assert response.status_code == 422
    assert "sender_id is required for human senders" in response.text


def test_system_message_allows_null_sender_id(client: TestClient) -> None:
    created = client.post(
        "/internal/messages",
        json=message_event(
            sender_type="system",
            sender_id=None,
            sender_name=None,
            message_type="system",
            raw_type=10000,
        ),
    )

    assert created.status_code == 201
    body = client.get(f"/messages/{created.json()['id']}").json()
    assert body["sender_type"] == "system"
    assert body["sender_id"] is None


def test_sender_type_rejects_unknown_value(client: TestClient) -> None:
    response = client.post(
        "/internal/messages",
        json=message_event(sender_type="bot"),
    )

    assert response.status_code == 422


def test_optional_source_fields_default_for_other_adapters(client: TestClient) -> None:
    event = message_event()
    del event["sender_type"]
    for field in (
        "raw_type",
        "source_local_id",
        "source_server_id",
        "source_message_id_is_fallback",
        "reply_context",
    ):
        del event[field]

    created = client.post("/internal/messages", json=event)

    assert created.status_code == 201
    body = client.get(f"/messages/{created.json()['id']}").json()
    assert body["sender_type"] == "human"
    assert body["raw_type"] is None
    assert body["source_local_id"] is None
    assert body["source_server_id"] is None
    assert body["source_message_id_is_fallback"] is False
    assert body["reply_context"] is None


@pytest.mark.parametrize("sender_id", [None, "", "   "])
def test_database_rejects_human_message_without_sender_id(
    client: TestClient, sender_id: str | None
) -> None:
    with database_session_factory(client)() as session:
        session.add(
            Conversation(
                source="test-channel",
                source_account_id="bot-001",
                conversation_id="conversation-001",
                conversation_type="group",
            )
        )
        session.add(
            Message(
                event_id="event-001",
                source="test-channel",
                source_account_id="bot-001",
                source_message_id="source-message-001",
                conversation_id="conversation-001",
                conversation_type="group",
                is_mentioned=False,
                is_self=False,
                sender_type="human",
                sender_id=sender_id,
                sender_name=None,
                message_type="text",
                raw_type=1,
                content="invalid",
                timestamp=datetime.fromisoformat("2026-07-31T10:00:00+08:00"),
                source_local_id="local-001",
                source_server_id="server-001",
                source_message_id_is_fallback=False,
                reply_context=None,
                reply_to_message_id=None,
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("raw_type", [True, "1"])
def test_raw_type_rejects_coercion(client: TestClient, raw_type: object) -> None:
    response = client.post("/internal/messages", json=message_event(raw_type=raw_type))

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
    message_check_names = {
        constraint.name
        for constraint in Message.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

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
    assert "ck_message_sender_type" in message_check_names
    assert "ck_message_human_sender_id" in message_check_names


def test_group_conversation_rejects_private_message_without_modification(
    client: TestClient,
) -> None:
    created = client.post("/internal/messages", json=message_event())
    conflict = client.post(
        "/internal/messages",
        json=message_event(
            "event-002",
            source_message_id="source-message-002",
            conversation_type="private",
            is_mentioned=None,
            conversation_name="Must not replace the group name",
        ),
    )

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "conversation_type_conflict",
        "source": "test-channel",
        "source_account_id": "bot-001",
        "conversation_id": "conversation-001",
        "existing_type": "group",
        "requested_type": "private",
    }
    assert conversation_snapshot(client) == ("group", "Gateway development", 1)


def test_private_conversation_rejects_group_message(client: TestClient) -> None:
    private_event = message_event(conversation_type="private", is_mentioned=None)
    created = client.post("/internal/messages", json=private_event)
    conflict = client.post(
        "/internal/messages",
        json=message_event(
            "event-002",
            source_message_id="source-message-002",
            conversation_name="Must not replace the private name",
        ),
    )

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "conversation_type_conflict"
    assert conflict.json()["detail"]["existing_type"] == "private"
    assert conflict.json()["detail"]["requested_type"] == "group"
    assert conversation_snapshot(client) == ("private", "Gateway development", 1)


def test_missing_conversation_name_preserves_existing_name(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    second = client.post(
        "/internal/messages",
        json=message_event(
            "event-002",
            source_message_id="source-message-002",
            conversation_name=None,
        ),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert conversation_snapshot(client) == ("group", "Gateway development", 2)


def test_non_null_conversation_name_updates_display_name(client: TestClient) -> None:
    first = client.post("/internal/messages", json=message_event())
    second = client.post(
        "/internal/messages",
        json=message_event(
            "event-002",
            source_message_id="source-message-002",
            conversation_name="Renamed conversation",
        ),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert conversation_snapshot(client) == ("group", "Renamed conversation", 2)


def test_concurrent_conversation_creation_recovers_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = database_session_factory(client)
    with factory() as first_session:
        first, first_created = MessageStore(first_session).create(parsed_message_event())

    with factory() as second_session:
        store = MessageStore(second_session)
        get_conversation = store._get_conversation
        conversation_lookups = 0

        def stale_then_current_conversation(**scope: str) -> Conversation | None:
            nonlocal conversation_lookups
            conversation_lookups += 1
            if conversation_lookups == 1:
                return None
            return get_conversation(**scope)

        monkeypatch.setattr(store, "_get_conversation", stale_then_current_conversation)
        second, second_created = store.create(
            parsed_message_event(
                "event-002",
                source_message_id="source-message-002",
                conversation_name="Concurrent rename must not win",
                content="Second physical message",
            )
        )

    assert first_created is True
    assert second_created is True
    assert first.id != second.id
    assert conversation_lookups == 2
    assert conversation_snapshot(client) == ("group", "Gateway development", 2)
    with factory() as verification_session:
        conversation_count = verification_session.scalar(
            select(func.count()).select_from(Conversation)
        )
        assert conversation_count == 1

    messages = client.get(conversation_messages_path()).json()
    assert [message["event_id"] for message in messages] == ["event-001", "event-002"]
    assert messages[0]["content"] == "Store this message"
    assert messages[0]["attachments"][0]["filename"] == "design.txt"
    assert messages[1]["content"] == "Second physical message"


def test_retry_path_keeps_duplicate_event_id_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = database_session_factory(client)
    with factory() as first_session:
        existing, _ = MessageStore(first_session).create(parsed_message_event())

    with factory() as retry_session:
        store = MessageStore(retry_session)
        get_existing_message = store._get_existing_message
        get_conversation = store._get_conversation
        existing_lookups = 0
        conversation_lookups = 0

        def temporarily_hidden_message(event: MessageEvent) -> Message | None:
            nonlocal existing_lookups
            existing_lookups += 1
            if existing_lookups <= 2:
                return None
            return get_existing_message(event)

        def stale_then_current_conversation(**scope: str) -> Conversation | None:
            nonlocal conversation_lookups
            conversation_lookups += 1
            if conversation_lookups == 1:
                return None
            return get_conversation(**scope)

        monkeypatch.setattr(store, "_get_existing_message", temporarily_hidden_message)
        monkeypatch.setattr(store, "_get_conversation", stale_then_current_conversation)
        duplicate, created = store.create(
            parsed_message_event(source_message_id="different-source-message")
        )

    assert created is False
    assert duplicate.id == existing.id
    assert existing_lookups == 3
    assert conversation_lookups == 2
    assert conversation_snapshot(client) == ("group", "Gateway development", 1)


def test_retry_path_keeps_duplicate_source_message_id_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = database_session_factory(client)
    with factory() as first_session:
        existing, _ = MessageStore(first_session).create(parsed_message_event())

    with factory() as retry_session:
        store = MessageStore(retry_session)
        get_existing_message = store._get_existing_message
        get_conversation = store._get_conversation
        existing_lookups = 0
        conversation_lookups = 0

        def temporarily_hidden_message(event: MessageEvent) -> Message | None:
            nonlocal existing_lookups
            existing_lookups += 1
            if existing_lookups <= 2:
                return None
            return get_existing_message(event)

        def stale_then_current_conversation(**scope: str) -> Conversation | None:
            nonlocal conversation_lookups
            conversation_lookups += 1
            if conversation_lookups == 1:
                return None
            return get_conversation(**scope)

        monkeypatch.setattr(store, "_get_existing_message", temporarily_hidden_message)
        monkeypatch.setattr(store, "_get_conversation", stale_then_current_conversation)
        duplicate, created = store.create(parsed_message_event("event-002"))

    assert created is False
    assert duplicate.id == existing.id
    assert existing_lookups == 3
    assert conversation_lookups == 2
    assert conversation_snapshot(client) == ("group", "Gateway development", 1)


def test_conversation_race_rejects_type_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = database_session_factory(client)
    with factory() as first_session:
        MessageStore(first_session).create(parsed_message_event())

    with factory() as conflicting_session:
        store = MessageStore(conflicting_session)
        get_conversation = store._get_conversation
        conversation_lookups = 0

        def stale_then_current_conversation(**scope: str) -> Conversation | None:
            nonlocal conversation_lookups
            conversation_lookups += 1
            if conversation_lookups == 1:
                return None
            return get_conversation(**scope)

        monkeypatch.setattr(store, "_get_conversation", stale_then_current_conversation)
        with pytest.raises(ConversationTypeConflictError) as exc_info:
            store.create(
                parsed_message_event(
                    "event-002",
                    source_message_id="source-message-002",
                    conversation_type="private",
                    is_mentioned=None,
                )
            )

    error = exc_info.value
    assert error.code == "conversation_type_conflict"
    assert error.source == "test-channel"
    assert error.source_account_id == "bot-001"
    assert error.conversation_id == "conversation-001"
    assert error.existing_type == "group"
    assert error.requested_type == "private"
    assert conversation_lookups == 2
    assert conversation_snapshot(client) == ("group", "Gateway development", 1)


def test_unknown_integrity_error_during_retry_is_not_swallowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = database_session_factory(client)
    with factory() as first_session:
        MessageStore(first_session).create(parsed_message_event())

    unknown_error = IntegrityError("INSERT INTO messages", {}, RuntimeError("unknown"))
    with factory() as retry_session:
        store = MessageStore(retry_session)
        get_conversation = store._get_conversation
        commit = retry_session.commit
        conversation_lookups = 0
        commit_attempts = 0

        def stale_then_current_conversation(**scope: str) -> Conversation | None:
            nonlocal conversation_lookups
            conversation_lookups += 1
            if conversation_lookups == 1:
                return None
            return get_conversation(**scope)

        def fail_unknown_on_retry() -> None:
            nonlocal commit_attempts
            commit_attempts += 1
            if commit_attempts == 2:
                raise unknown_error
            commit()

        monkeypatch.setattr(store, "_get_conversation", stale_then_current_conversation)
        monkeypatch.setattr(retry_session, "commit", fail_unknown_on_retry)
        with pytest.raises(IntegrityError) as exc_info:
            store.create(
                parsed_message_event(
                    "event-002",
                    source_message_id="source-message-002",
                )
            )

    assert exc_info.value is unknown_error
    assert conversation_lookups == 2
    assert commit_attempts == 2
    assert conversation_snapshot(client) == ("group", "Gateway development", 1)
