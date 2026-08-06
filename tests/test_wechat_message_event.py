from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from cf_agent_gateway.adapters.wechat import (
    RawWechatMessage,
    normalize_wechat_message,
    wechat_message_to_event,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity, SourceIdentityMapping
from cf_agent_gateway.message.models import Message, MessageRawPayload
from cf_agent_gateway.message.store import MessageStore


def test_wechat_converter_maps_every_message_event_field() -> None:
    normalized = normalize_wechat_message(
        {
            "localId": " 44 ",
            "serverId": 55,
            "chatId": "team@chatroom",
            "sender": "wxid_alice",
            "senderName": "Alice",
            "type": 49,
            "content": "reply body",
            "timestamp": "2026-08-01T10:15:00+08:00",
            "isMentioned": True,
            "isSelf": True,
            "reply": {
                "localId": 12,
                "serverId": " 34 ",
                "sender": "wxid_bob",
                "senderName": "Bob",
                "type": 1,
                "content": "original body",
            },
        },
        source_account_id="wxid_bot",
        conversation_name="Gateway Team",
    )

    event = wechat_message_to_event(normalized)

    event_payload = event.model_dump()
    received_at = event_payload.pop("received_at")
    assert received_at >= event_payload["occurred_at"]
    assert event_payload == {
        "event_id": normalized.event_id,
        "source": "wechat",
        "source_account_id": "wxid_bot",
        "source_message_id": "55",
        "conversation_id": "team@chatroom",
        "conversation_type": "group",
        "is_mentioned": True,
        "is_self": True,
        "conversation_name": "Gateway Team",
        "sender_type": "human",
        "sender_id": "wxid_alice",
        "sender_name": "Alice",
        "message_type": "reply",
        "raw_type": 49,
        "content": "reply body",
        "timestamp": datetime.fromisoformat("2026-08-01T10:15:00+08:00"),
        "occurred_at": datetime.fromisoformat("2026-08-01T10:15:00+08:00"),
        "direction": "outbound",
        "source_local_id": "44",
        "source_server_id": "55",
        "source_message_id_is_fallback": False,
        "reply_context": {
            "source_local_id": "12",
            "source_server_id": "34",
            "sender_id": "wxid_bob",
            "sender_name": "Bob",
            "raw_type": 1,
            "content": "original body",
        },
        "reply_to_message_id": None,
        "attachments": [],
        "raw_payload": {
            "localId": " 44 ",
            "serverId": 55,
            "chatId": "team@chatroom",
            "sender": "wxid_alice",
            "senderName": "Alice",
            "type": 49,
            "content": "reply body",
            "timestamp": "2026-08-01T10:15:00+08:00",
            "isMentioned": True,
            "isSelf": True,
            "reply": {
                "localId": 12,
                "serverId": " 34 ",
                "sender": "wxid_bob",
                "senderName": "Bob",
                "type": 1,
                "content": "original body",
            },
        },
    }


def test_senderless_wechat_system_message_is_persisted_and_queryable(
    client: TestClient,
) -> None:
    content = '"某人"邀请你加入了群聊'
    raw = RawWechatMessage.model_validate(
        {
            "localId": 1,
            "serverId": 123,
            "chatId": "group@chatroom",
            "type": 10000,
            "content": content,
            "timestamp": "2026-08-01T10:15:00+08:00",
        }
    )
    normalized = normalize_wechat_message(raw, source_account_id="wxid_bot")
    event = wechat_message_to_event(normalized)

    with client.app.state.database_session_factory() as session:
        stored, created = MessageStore(session).create(event)
        message_id = stored.id
        identity_count = session.scalar(select(func.count()).select_from(EnterpriseIdentity))
        mapping_count = session.scalar(select(func.count()).select_from(SourceIdentityMapping))
        null_reply_count = session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.id == message_id, Message.reply_context.is_(None))
        )

    assert created is True
    assert identity_count == 0
    assert mapping_count == 0
    assert null_reply_count == 1

    response = client.get(f"/messages/{message_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["sender_type"] == "system"
    assert body["sender_id"] is None
    assert body["sender_name"] is None
    assert body["message_type"] == "system"
    assert body["raw_type"] == 10000
    assert body["content"] == content
    assert body["source_message_id"] == "123"
    assert body["source_local_id"] == "1"
    assert body["source_server_id"] == "123"
    assert body["source_message_id_is_fallback"] is False
    assert body["is_mentioned"] is False
    assert body["is_self"] is False
    assert body["reply_context"] is None
    assert body["reply_to_message_id"] is None
    assert body["attachments"] == []

    conversation_response = client.get(
        "/sources/wechat/accounts/wxid_bot/conversations/group@chatroom/messages"
    )
    assert conversation_response.status_code == 200
    assert [message["id"] for message in conversation_response.json()] == [message_id]
    assert conversation_response.json()[0]["sender_type"] == "system"


def test_wechat_raw_payload_survives_normalization_and_persistence(
    client: TestClient,
) -> None:
    raw_payload = {
        "localId": 88,
        "serverId": 99,
        "chatId": "wxid_alice",
        "sender": "wxid_alice",
        "type": 1,
        "content": "archive exactly",
        "timestamp": "2026-08-01T10:15:00+08:00",
        "unknown": {"items": [1, True, None, "value"]},
    }
    normalized = normalize_wechat_message(raw_payload, source_account_id="wxid_bot")
    event = wechat_message_to_event(normalized)

    with client.app.state.database_session_factory() as session:
        stored, created = MessageStore(session).create(event)
        archived_payload = session.scalar(
            select(MessageRawPayload).where(MessageRawPayload.message_id == stored.id)
        )

    assert created is True
    assert archived_payload is not None
    assert archived_payload.payload == raw_payload


def test_wechat_local_id_fallback_and_self_flag_survive_persistence(
    client: TestClient,
) -> None:
    normalized = normalize_wechat_message(
        {
            "localId": 101,
            "serverId": None,
            "chatId": "wxid_alice",
            "sender": "wxid_alice",
            "senderName": "Alice",
            "type": 1,
            "content": "  sent by this account  ",
            "timestamp": "2026-08-01T10:16:00+08:00",
            "isMentioned": True,
            "isSelf": True,
        },
        source_account_id="wxid_bot",
    )
    event = wechat_message_to_event(normalized)

    with client.app.state.database_session_factory() as session:
        stored, created = MessageStore(session).create(event)
        message_id = stored.id

    assert created is True
    response = client.get(f"/messages/{message_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["source_message_id"].startswith("local:v1:")
    assert body["source_local_id"] == "101"
    assert body["source_server_id"] is None
    assert body["source_message_id_is_fallback"] is True
    assert body["content"] == "  sent by this account  "
    assert body["is_mentioned"] is None
    assert body["is_self"] is True

    with client.app.state.database_session_factory() as session:
        persisted = session.scalar(select(Message).where(Message.id == message_id))
        assert persisted is not None
        assert persisted.source_message_id_is_fallback is True
