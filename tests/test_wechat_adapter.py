from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import pytest

from cf_agent_gateway.adapters.wechat import (
    AgentWechatClient,
    WechatAPIError,
    WechatConversationType,
    WechatMessageType,
    WechatNormalizationError,
    WechatResponseError,
    WechatSenderType,
    WechatTimeoutError,
    normalize_wechat_message,
)

BASE_URL = "http://agent-wechat.test:6174"
TOKEN = "test-token-that-must-not-leak"


def raw_message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "localId": 101,
        "serverId": 9001,
        "chatId": "wxid_alice",
        "sender": "wxid_alice",
        "senderName": "Alice",
        "type": 1,
        "content": "hello",
        "timestamp": "2026-08-01T10:15:00+08:00",
    }
    message.update(overrides)
    return message


def raw_system_message(**overrides: Any) -> dict[str, Any]:
    message = raw_message(
        localId=102,
        serverId=9002,
        chatId="team@chatroom",
        type=10000,
        content="You were invited to the group chat",
    )
    message.pop("sender")
    message.pop("senderName")
    message.update(overrides)
    return message


def wechat_client(handler: Callable[[httpx.Request], httpx.Response]) -> AgentWechatClient:
    return AgentWechatClient(BASE_URL, TOKEN, transport=httpx.MockTransport(handler))


def test_get_auth_status_parses_logged_in_user_and_sends_bearer_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/status/auth"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(
            200,
            json={"status": "logged_in", "loggedInUser": "wxid_bot"},
        )

    with wechat_client(handler) as client:
        status = client.get_auth_status()

    assert status.logged_in_user == "wxid_bot"
    assert status.source_account_id == "wxid_bot"
    assert status.status == "logged_in"


def test_get_auth_status_accepts_one_data_wrapper() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"status": "logged_in", "loggedInUser": "wxid_bot"}},
        )

    with wechat_client(handler) as client:
        assert client.get_auth_status().logged_in_user == "wxid_bot"


def test_get_auth_status_accepts_logged_out_without_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "logged_out"})

    with wechat_client(handler) as client:
        status = client.get_auth_status()

    assert status.status == "logged_out"
    assert status.logged_in_user is None
    assert status.source_account_id is None


def test_bearer_token_is_not_exposed_by_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(401, json={"error": TOKEN})

    with wechat_client(handler) as client, pytest.raises(WechatAPIError) as caught:
        client.get_auth_status()

    error = caught.value
    assert error.code == "wechat_api_error"
    assert error.category == "authentication"
    assert error.status_code == 401
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)
    assert error.__cause__ is None


@pytest.mark.parametrize(
    "invalid_token", ["secret-测试", "secret\r\nInjected: value", "secret token"]
)
def test_invalid_bearer_token_is_rejected_without_leaking_value(invalid_token: str) -> None:
    with pytest.raises(ValueError) as caught:
        AgentWechatClient(BASE_URL, invalid_token)

    assert invalid_token not in str(caught.value)
    assert invalid_token not in repr(caught.value)


def test_list_chats_and_messages_parse_supported_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chats":
            return httpx.Response(200, json={"data": {"chats": [{"chatId": "wxid_alice"}]}})
        assert request.url.path == "/api/messages/wxid_alice"
        return httpx.Response(200, json={"messages": [raw_message()]})

    with wechat_client(handler) as client:
        chats = client.list_chats()
        messages = client.list_messages("wxid_alice")

    assert chats == [{"chatId": "wxid_alice"}]
    assert messages[0].server_id == 9001


def test_get_media_decodes_verified_txt_response_and_preserves_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/api/messages/team%2Fone/media/local%231"
        return httpx.Response(
            200,
            json={
                "type": "file",
                "data": "YWdlbnQtd2VjaGF0IGZpbGUgdGVzdA==",
                "format": "txt",
                "filename": "test.txt",
            },
        )

    with wechat_client(handler) as client:
        media = client.get_media("team/one", "local#1")

    assert media.media_type == "file"
    assert media.data == b"agent-wechat file test"
    assert media.format == "txt"
    assert media.filename == "test.txt"
    assert media.supported is True


def test_get_media_decodes_verified_zip_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "data": "UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==",
                "format": "zip",
                "filename": "archive.zip",
            },
        )

    with wechat_client(handler) as client:
        media = client.get_media("wxid_alice", 101)

    assert media.data == b"PK\x05\x06" + (b"\x00" * 18)
    assert media.format == "zip"
    assert media.filename == "archive.zip"
    assert media.supported is True


def test_get_media_returns_controlled_unsupported_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"type": "unsupported", "format": "", "filename": ""},
        )

    with wechat_client(handler) as client:
        media = client.get_media("wxid_alice", 101)

    assert media.media_type == "unsupported"
    assert media.data is None
    assert media.format == ""
    assert media.filename == ""
    assert media.supported is False


def test_get_media_rejects_supported_response_without_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"type": "file", "format": "txt", "filename": "test.txt"},
        )

    with wechat_client(handler) as client, pytest.raises(WechatResponseError) as caught:
        client.get_media("wxid_alice", 101)

    assert caught.value.code == "wechat_response_error"
    assert caught.value.operation == "get_media"


def test_get_media_rejects_invalid_base64_without_leaking_data() -> None:
    invalid_data = "sensitive-invalid-base64!!!"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "data": invalid_data,
                "format": "txt",
                "filename": "test.txt",
            },
        )

    with wechat_client(handler) as client, pytest.raises(WechatResponseError) as caught:
        client.get_media("wxid_alice", 101)

    assert invalid_data not in str(caught.value)
    assert invalid_data not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_send_text_uses_verified_endpoint_and_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/messages/send"
        payload = json.loads(request.content)
        assert payload == {"chatId": "wxid_alice", "text": "hello"}
        assert "content" not in payload
        return httpx.Response(200, json={"success": True, "localId": 102})

    with wechat_client(handler) as client:
        result = client.send_text("wxid_alice", "hello")

    assert result == {"success": True, "localId": 102}


def test_transport_timeout_has_stable_sanitized_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(TOKEN, request=request)

    with wechat_client(handler) as client, pytest.raises(WechatTimeoutError) as caught:
        client.list_chats()

    assert caught.value.code == "wechat_timeout_error"
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


def test_success_with_invalid_json_has_stable_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with wechat_client(handler) as client, pytest.raises(WechatResponseError) as caught:
        client.list_chats()

    assert caught.value.code == "wechat_response_error"


def test_private_message_normalization() -> None:
    normalized = normalize_wechat_message(raw_message(), source_account_id="wxid_bot")

    assert normalized.source == "wechat"
    assert normalized.source_account_id == "wxid_bot"
    assert normalized.source_local_id == "101"
    assert normalized.source_server_id == "9001"
    assert normalized.conversation_id == "wxid_alice"
    assert normalized.conversation_type is WechatConversationType.PRIVATE
    assert normalized.conversation_name is None
    assert normalized.sender_id == "wxid_alice"
    assert normalized.sender_name == "Alice"
    assert normalized.sender_type is WechatSenderType.HUMAN
    assert normalized.message_type is WechatMessageType.TEXT
    assert normalized.raw_type == 1
    assert normalized.content == "hello"
    assert normalized.timestamp == datetime.fromisoformat("2026-08-01T10:15:00+08:00")
    assert normalized.is_mentioned is None
    assert normalized.is_self is False


def test_group_message_normalization_and_chat_id_recognition() -> None:
    normalized = normalize_wechat_message(
        raw_message(chatId="team@chatroom"),
        source_account_id="wxid_bot",
        conversation_name="Gateway Team",
    )

    assert normalized.conversation_type is WechatConversationType.GROUP
    assert normalized.conversation_name == "Gateway Team"
    assert normalized.is_mentioned is False


@pytest.mark.parametrize(
    ("chat_id", "expected"),
    [
        ("team@chatroom", WechatConversationType.GROUP),
        ("team@chatroom-extra", WechatConversationType.PRIVATE),
        ("wxid_alice", WechatConversationType.PRIVATE),
    ],
)
def test_only_chatroom_suffix_identifies_a_group(
    chat_id: str, expected: WechatConversationType
) -> None:
    normalized = normalize_wechat_message(raw_message(chatId=chat_id), source_account_id="wxid_bot")

    assert normalized.conversation_type is expected


def test_structured_mention_of_current_bot_is_true() -> None:
    normalized = normalize_wechat_message(
        raw_message(chatId="team@chatroom", isMentioned=True, content="@Bot_测试版 hello"),
        source_account_id="wxid_bot",
    )

    assert normalized.is_mentioned is True


def test_mention_of_another_member_without_field_is_false() -> None:
    normalized = normalize_wechat_message(
        raw_message(chatId="team@chatroom", content="@OtherMember hello"),
        source_account_id="wxid_bot",
    )

    assert normalized.is_mentioned is False


def test_manually_typed_bot_name_without_field_is_false() -> None:
    normalized = normalize_wechat_message(
        raw_message(chatId="team@chatroom", content="@Bot_测试版 hello"),
        source_account_id="wxid_bot",
    )

    assert normalized.is_mentioned is False


def test_mention_and_self_flags_require_literal_true() -> None:
    normalized = normalize_wechat_message(
        raw_message(chatId="team@chatroom", isMentioned=1, isSelf="true"),
        source_account_id="wxid_bot",
    )

    assert normalized.is_mentioned is False
    assert normalized.is_self is False


def test_is_self_true_is_preserved() -> None:
    normalized = normalize_wechat_message(raw_message(isSelf=True), source_account_id="wxid_bot")

    assert normalized.is_self is True


def test_mixed_message_list_reads_and_normalizes_senderless_system_message() -> None:
    system_message = raw_system_message()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/messages/team@chatroom"
        return httpx.Response(
            200,
            json={
                "messages": [
                    raw_message(chatId="team@chatroom"),
                    system_message,
                ]
            },
        )

    with wechat_client(handler) as client:
        messages = client.list_messages("team@chatroom")

    assert len(messages) == 2
    assert messages[1].sender is None
    normalized = normalize_wechat_message(messages[1], source_account_id="wxid_bot")
    assert normalized.message_type is WechatMessageType.SYSTEM
    assert normalized.sender_type is WechatSenderType.SYSTEM
    assert normalized.sender_id is None
    assert normalized.sender_name is None
    assert normalized.content == "You were invited to the group chat"
    assert normalized.is_mentioned is False
    assert normalized.is_self is False


def test_system_message_preserves_explicit_is_self_true() -> None:
    normalized = normalize_wechat_message(
        raw_system_message(isSelf=True), source_account_id="wxid_bot"
    )

    assert normalized.sender_type is WechatSenderType.SYSTEM
    assert normalized.is_self is True


def test_normal_message_without_sender_is_rejected_during_normalization() -> None:
    message = raw_message()
    message.pop("sender")

    with pytest.raises(WechatNormalizationError, match="sender"):
        normalize_wechat_message(message, source_account_id="wxid_bot")


def test_server_id_is_the_stable_source_message_id() -> None:
    first = normalize_wechat_message(raw_message(), source_account_id="wxid_bot")
    second = normalize_wechat_message(raw_message(localId=999), source_account_id="wxid_bot")

    assert first.source_message_id == "9001"
    assert first.source_message_id_is_fallback is False
    assert second.source_message_id == first.source_message_id
    assert second.event_id == first.event_id


def test_missing_server_id_uses_scoped_local_id_fallback() -> None:
    first = normalize_wechat_message(raw_message(serverId=None), source_account_id="wxid_bot")
    same = normalize_wechat_message(raw_message(serverId=None), source_account_id="wxid_bot")
    other_chat = normalize_wechat_message(
        raw_message(serverId=None, chatId="wxid_bob"), source_account_id="wxid_bot"
    )
    other_account = normalize_wechat_message(
        raw_message(serverId=None), source_account_id="wxid_other_bot"
    )

    assert first.source_message_id.startswith("local:v1:")
    assert first.source_local_id == "101"
    assert first.source_server_id is None
    assert first.source_message_id_is_fallback is True
    assert same.source_message_id == first.source_message_id
    assert other_chat.source_message_id != first.source_message_id
    assert other_account.source_message_id != first.source_message_id


@pytest.mark.parametrize("server_id", [None, "", "   ", 0, "0"])
def test_unusable_server_id_falls_back_to_local_id(server_id: object) -> None:
    normalized = normalize_wechat_message(
        raw_message(serverId=server_id), source_account_id="wxid_bot"
    )

    assert normalized.source_message_id_is_fallback is True
    assert normalized.source_local_id == "101"
    assert normalized.source_server_id is None


def test_source_ids_use_canonical_upstream_strings() -> None:
    normalized = normalize_wechat_message(
        raw_message(localId="  local-101  ", serverId="  server-9001  "),
        source_account_id="wxid_bot",
    )

    assert normalized.source_local_id == "local-101"
    assert normalized.source_server_id == "server-9001"
    assert normalized.source_message_id == "server-9001"


def test_missing_server_and_local_ids_is_a_normalization_error() -> None:
    with pytest.raises(WechatNormalizationError) as caught:
        normalize_wechat_message(
            raw_message(serverId=None, localId=None), source_account_id="wxid_bot"
        )

    assert caught.value.code == "wechat_normalization_error"


def test_event_id_is_deterministic() -> None:
    first = normalize_wechat_message(raw_message(), source_account_id="wxid_bot")
    renamed = normalize_wechat_message(
        raw_message(senderName="Renamed", content="changed"),
        source_account_id="wxid_bot",
        conversation_name="Renamed conversation",
    )

    assert first.event_id.startswith("v1:wechat:")
    assert renamed.event_id == first.event_id


def test_different_bot_accounts_have_different_event_ids() -> None:
    first = normalize_wechat_message(raw_message(), source_account_id="wxid_bot_one")
    second = normalize_wechat_message(raw_message(), source_account_id="wxid_bot_two")

    assert first.event_id != second.event_id


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        (1, WechatMessageType.TEXT),
        (3, WechatMessageType.IMAGE),
        (999, WechatMessageType.UNKNOWN),
    ],
)
def test_verified_and_unknown_message_type_mapping(
    raw_type: int, expected: WechatMessageType
) -> None:
    normalized = normalize_wechat_message(raw_message(type=raw_type), source_account_id="wxid_bot")

    assert normalized.message_type is expected
    assert normalized.raw_type == raw_type


def test_type_49_is_not_assumed_to_be_a_file() -> None:
    normalized = normalize_wechat_message(
        raw_message(type=49, content="unstructured app content"),
        source_account_id="wxid_bot",
    )

    assert normalized.message_type is WechatMessageType.APP


def test_type_49_uses_structured_reply_fact_and_preserves_summary() -> None:
    normalized = normalize_wechat_message(
        raw_message(
            type=49,
            reply={
                "serverId": 8123,
                "sender": "wxid_bob",
                "senderName": "Bob",
                "type": 1,
                "content": "original",
            },
        ),
        source_account_id="wxid_bot",
    )

    assert normalized.message_type is WechatMessageType.REPLY
    assert normalized.reply is not None
    assert normalized.reply.server_id == "8123"
    assert normalized.reply.sender_id == "wxid_bob"
    assert normalized.reply.content == "original"


@pytest.mark.parametrize("reply", [False, 0, [], {}, "", "   "])
def test_type_49_does_not_treat_empty_reply_values_as_reply(reply: object) -> None:
    normalized = normalize_wechat_message(
        raw_message(type=49, reply=reply), source_account_id="wxid_bot"
    )

    assert normalized.message_type is WechatMessageType.APP
    assert normalized.reply is None


@pytest.mark.parametrize("raw_type", [True, 1.0, "1"])
def test_raw_message_type_is_not_coerced(raw_type: object) -> None:
    with pytest.raises(WechatNormalizationError):
        normalize_wechat_message(raw_message(type=raw_type), source_account_id="wxid_bot")


def test_unknown_raw_fields_are_deliberately_ignored() -> None:
    normalized = normalize_wechat_message(
        raw_message(unverifiedField="unverified"), source_account_id="wxid_bot"
    )

    assert normalized.message_type is WechatMessageType.TEXT


@pytest.mark.parametrize(
    ("subtype", "expected"),
    [(6, WechatMessageType.FILE), (19, WechatMessageType.FORWARD)],
)
def test_type_49_uses_structured_app_subtype(subtype: int, expected: WechatMessageType) -> None:
    content = f"<msg><appmsg><type>{subtype}</type></appmsg></msg>"
    normalized = normalize_wechat_message(
        raw_message(type=49, content=content), source_account_id="wxid_bot"
    )

    assert normalized.message_type is expected
