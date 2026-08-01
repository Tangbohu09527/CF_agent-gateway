from __future__ import annotations

import hashlib
import json

from cf_agent_gateway.workspace.models import ThreadType

THREAD_KEY_VERSION = 1


def build_thread_key(
    *,
    platform: str,
    account_id: str,
    physical_conversation_id: str,
    conversation_type: ThreadType | str,
    sender_id: str | None = None,
) -> str:
    thread_type = ThreadType(conversation_type)
    fields = {
        "account_id": _required(account_id, "account_id"),
        "conversation_id": _required(physical_conversation_id, "physical_conversation_id"),
        "platform": _required(platform, "platform"),
        "thread_type": thread_type.value,
        "version": THREAD_KEY_VERSION,
    }
    if thread_type is ThreadType.GROUP:
        fields["sender_id"] = _required(sender_id, "sender_id")

    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"v{THREAD_KEY_VERSION}:{thread_type.value}:{digest}"


def build_private_thread_key(*, platform: str, account_id: str, private_chat_id: str) -> str:
    return build_thread_key(
        platform=platform,
        account_id=account_id,
        physical_conversation_id=private_chat_id,
        conversation_type=ThreadType.PRIVATE,
    )


def build_group_thread_key(
    *, platform: str, account_id: str, group_chat_id: str, sender_id: str
) -> str:
    return build_thread_key(
        platform=platform,
        account_id=account_id,
        physical_conversation_id=group_chat_id,
        conversation_type=ThreadType.GROUP,
        sender_id=sender_id,
    )


def _required(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()
