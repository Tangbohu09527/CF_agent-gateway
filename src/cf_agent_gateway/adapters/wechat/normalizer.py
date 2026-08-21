from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree

from pydantic import ValidationError

from cf_agent_gateway.adapters.wechat.errors import WechatNormalizationError
from cf_agent_gateway.adapters.wechat.normalized_models import (
    NormalizedWechatMessage,
    WechatConversationType,
    WechatMessageType,
    WechatReplySummary,
    WechatSenderType,
)
from cf_agent_gateway.adapters.wechat.raw_models import RawWechatMessage

EVENT_ID_VERSION = 1
LOCAL_MESSAGE_ID_VERSION = 1
REGRESSED_LOCAL_MESSAGE_ID_VERSION = 2
WECHAT_SOURCE = "wechat"


def normalize_wechat_message(
    raw: RawWechatMessage | Mapping[str, Any],
    *,
    source_account_id: str,
    conversation_name: str | None = None,
    sync_generation: int = 0,
) -> NormalizedWechatMessage:
    message = _raw_message(raw)
    account_id = _required(source_account_id, "source_account_id")
    chat_id = _required(message.chat_id, "chatId")
    generation = _validated_sync_generation(sync_generation)
    is_system = message.type == 10000
    sender_type = WechatSenderType.SYSTEM if is_system else WechatSenderType.HUMAN
    sender_id = (
        _optional_string(message.sender) if is_system else _required(message.sender, "sender")
    )
    source_local_id = _usable_id(message.local_id)
    source_server_id = _usable_id(message.server_id)
    source_message_id, is_fallback = _source_message_id(
        server_id=source_server_id,
        local_id=source_local_id,
        account_id=account_id,
        chat_id=chat_id,
        sync_generation=generation,
    )
    conversation_type = (
        WechatConversationType.GROUP
        if chat_id.endswith("@chatroom")
        else WechatConversationType.PRIVATE
    )
    is_mentioned = (
        message.is_mentioned is True if conversation_type is WechatConversationType.GROUP else None
    )
    reply = _reply_summary(message.reply)

    return NormalizedWechatMessage(
        source_account_id=account_id,
        source_message_id=source_message_id,
        source_local_id=source_local_id,
        source_server_id=source_server_id,
        source_message_id_is_fallback=is_fallback,
        event_id=build_wechat_event_id(
            source_account_id=account_id,
            chat_id=chat_id,
            source_message_id=source_message_id,
        ),
        conversation_id=chat_id,
        conversation_type=conversation_type,
        conversation_name=conversation_name,
        sender_type=sender_type,
        sender_id=sender_id,
        sender_name=message.sender_name,
        message_type=_message_type(message, reply=reply),
        raw_type=message.type,
        content=message.content,
        timestamp=message.timestamp,
        is_mentioned=is_mentioned,
        is_self=message.is_self is True,
        reply=reply,
    )


def build_wechat_event_id(*, source_account_id: str, chat_id: str, source_message_id: str) -> str:
    fields = {
        "account_id": _required(source_account_id, "source_account_id"),
        "chat_id": _required(chat_id, "chat_id"),
        "platform": WECHAT_SOURCE,
        "source_message_id": _required(source_message_id, "source_message_id"),
        "version": EVENT_ID_VERSION,
    }
    return f"v{EVENT_ID_VERSION}:{WECHAT_SOURCE}:{_digest(fields)}"


def _raw_message(raw: RawWechatMessage | Mapping[str, Any]) -> RawWechatMessage:
    if isinstance(raw, RawWechatMessage):
        return raw
    try:
        return RawWechatMessage.model_validate(raw)
    except (TypeError, ValidationError):
        raise WechatNormalizationError("raw WeChat message is invalid") from None


def _source_message_id(
    *,
    server_id: str | int | None,
    local_id: str | int | None,
    account_id: str,
    chat_id: str,
    sync_generation: int,
) -> tuple[str, bool]:
    stable_id = _usable_id(server_id)
    if stable_id is not None:
        return stable_id, False

    fallback_local_id = _usable_id(local_id)
    if fallback_local_id is None:
        raise WechatNormalizationError("raw WeChat message requires a usable serverId or localId")
    if sync_generation == 0:
        fields = {
            "account_id": account_id,
            "chat_id": chat_id,
            "local_id": fallback_local_id,
            "platform": WECHAT_SOURCE,
            "version": LOCAL_MESSAGE_ID_VERSION,
        }
        return f"local:v{LOCAL_MESSAGE_ID_VERSION}:{_digest(fields)}", True

    fields = {
        "account_id": account_id,
        "chat_id": chat_id,
        "generation": sync_generation,
        "local_id": fallback_local_id,
        "platform": WECHAT_SOURCE,
        "version": REGRESSED_LOCAL_MESSAGE_ID_VERSION,
    }
    return f"local:v{REGRESSED_LOCAL_MESSAGE_ID_VERSION}:{_digest(fields)}", True


def _message_type(
    message: RawWechatMessage, *, reply: WechatReplySummary | None
) -> WechatMessageType:
    if message.type == 10000:
        return WechatMessageType.SYSTEM
    if message.type == 1:
        return WechatMessageType.TEXT
    if message.type == 3:
        return WechatMessageType.IMAGE
    if message.type != 49:
        return WechatMessageType.UNKNOWN
    if reply is not None:
        return WechatMessageType.REPLY

    app_subtype = _app_message_subtype(message.content)
    if app_subtype == 6:
        return WechatMessageType.FILE
    if app_subtype == 19:
        return WechatMessageType.FORWARD
    return WechatMessageType.APP


def _app_message_subtype(content: str) -> int | None:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    app_message = root if root.tag == "appmsg" else root.find(".//appmsg")
    if app_message is None:
        return None
    subtype = app_message.findtext("type")
    try:
        return int(subtype) if subtype is not None else None
    except ValueError:
        return None


def _reply_summary(reply: Any) -> WechatReplySummary | None:
    if reply is None:
        return None
    if isinstance(reply, str):
        content = _optional_string(reply)
        return WechatReplySummary(content=content) if content is not None else None
    if not isinstance(reply, Mapping):
        return None
    raw_type = reply.get("type")
    summary = WechatReplySummary(
        local_id=_usable_id(reply.get("localId")),
        server_id=_usable_id(reply.get("serverId")),
        sender_id=_optional_string(reply.get("sender")),
        sender_name=_optional_string(reply.get("senderName")),
        raw_type=raw_type if type(raw_type) is int else None,
        content=_optional_string(reply.get("content")),
    )
    return summary if any(value is not None for value in summary.model_dump().values()) else None


def _usable_id(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and normalized != "0" else None


def _validated_sync_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WechatNormalizationError("sync_generation must be a nonnegative integer")
    return value


def _required(value: str | None, field_name: str) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise WechatNormalizationError(f"{field_name} must not be empty")
    return normalized


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _digest(fields: dict[str, object]) -> str:
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
