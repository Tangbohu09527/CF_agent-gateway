from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

from cf_agent_gateway.workspace.models import ThreadPolicy, ThreadType

THREAD_KEY_VERSION = 1
THREAD_KEY_V2_VERSION = 2
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


def build_v2_thread_key(
    *,
    platform: str,
    account_id: str,
    physical_conversation_id: str,
    conversation_type: ThreadType | str,
    sender_identity_id: str | None,
    agent_profile_id: str,
    agent_profile_revision: str | int,
    thread_policy: ThreadPolicy | str,
) -> str:
    thread_type = _thread_type(conversation_type)
    policy = _thread_policy(thread_policy)
    _validate_policy(policy, thread_type)
    if agent_profile_revision is None:
        raise ValueError("agent_profile_revision must not be empty")

    normalized_sender_identity_id = None
    if policy is not ThreadPolicy.GROUP_SHARED:
        normalized_sender_identity_id = _required(
            sender_identity_id,
            "sender_identity_id",
        )

    fields = {
        "account_id": _required(account_id, "account_id"),
        "agent_profile_id": _required(agent_profile_id, "agent_profile_id"),
        "agent_profile_revision": _required(
            str(agent_profile_revision),
            "agent_profile_revision",
        ),
        "conversation_id": _required(
            physical_conversation_id,
            "physical_conversation_id",
        ),
        "conversation_type": thread_type.value,
        "platform": _required(platform, "platform").casefold(),
        "sender_identity_id": normalized_sender_identity_id,
        "thread_policy": policy.value,
        "version": THREAD_KEY_V2_VERSION,
    }
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"v{THREAD_KEY_V2_VERSION}:sha256:{policy.value}:{digest}"


def _required(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _thread_type(value: ThreadType | str) -> ThreadType:
    if isinstance(value, ThreadType):
        return value
    return ThreadType(_required(value, "conversation_type").casefold())


def _thread_policy(value: ThreadPolicy | str) -> ThreadPolicy:
    if isinstance(value, ThreadPolicy):
        return value
    return ThreadPolicy(_required(value, "thread_policy").casefold())


def _validate_policy(policy: ThreadPolicy, thread_type: ThreadType) -> None:
    if policy is ThreadPolicy.PRIVATE_SENDER and thread_type is not ThreadType.PRIVATE:
        raise ValueError("private_sender requires a private conversation")
    if policy is not ThreadPolicy.PRIVATE_SENDER and thread_type is not ThreadType.GROUP:
        raise ValueError(f"{policy.value} requires a group conversation")


def _encode_segment(value: str) -> str:
    return quote(value, safe=_READABLE_SEGMENT_SAFE_CHARACTERS)
