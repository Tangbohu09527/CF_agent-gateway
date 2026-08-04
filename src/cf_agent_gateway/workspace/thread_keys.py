from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

from cf_agent_gateway.workspace.models import ThreadType

THREAD_KEY_VERSION = 1
THREAD_KEY_MAX_LENGTH = 96
_READABLE_SEGMENT_SAFE_CHARACTERS = "-._~@"


def build_thread_key(
    *,
    platform: str,
    account_id: str,
    physical_conversation_id: str,
    conversation_type: ThreadType | str,
    sender_id: str | None = None,
) -> str:
    thread_type = _thread_type(conversation_type)
    normalized_platform = _required(platform, "platform").casefold()
    normalized_account_id = _required(account_id, "account_id")
    normalized_conversation_id = _required(physical_conversation_id, "physical_conversation_id")
    fields = {
        "account_id": normalized_account_id,
        "conversation_id": normalized_conversation_id,
        "platform": normalized_platform,
        "thread_type": thread_type.value,
        "version": THREAD_KEY_VERSION,
    }
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    readable_key = ":".join(
        (
            f"v{THREAD_KEY_VERSION}",
            _encode_segment(normalized_platform),
            thread_type.value,
            _encode_segment(normalized_account_id),
            _encode_segment(normalized_conversation_id),
        )
    )
    if len(readable_key) <= THREAD_KEY_MAX_LENGTH:
        return readable_key

    digest = hashlib.sha256(canonical).hexdigest()
    return f"v{THREAD_KEY_VERSION}:sha256:{thread_type.value}:{digest}"


def build_private_thread_key(*, platform: str, account_id: str, private_chat_id: str) -> str:
    return build_thread_key(
        platform=platform,
        account_id=account_id,
        physical_conversation_id=private_chat_id,
        conversation_type=ThreadType.PRIVATE,
    )


def build_group_thread_key(
    *,
    platform: str,
    account_id: str,
    group_chat_id: str,
    sender_id: str | None = None,
) -> str:
    return build_thread_key(
        platform=platform,
        account_id=account_id,
        physical_conversation_id=group_chat_id,
        conversation_type=ThreadType.GROUP,
    )


def _required(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _thread_type(value: ThreadType | str) -> ThreadType:
    if isinstance(value, ThreadType):
        return value
    return ThreadType(_required(value, "conversation_type").casefold())


def _encode_segment(value: str) -> str:
    return quote(value, safe=_READABLE_SEGMENT_SAFE_CHARACTERS)
