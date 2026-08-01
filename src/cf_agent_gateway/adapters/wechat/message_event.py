from __future__ import annotations

from cf_agent_gateway.adapters.wechat.normalized_models import NormalizedWechatMessage
from cf_agent_gateway.message.schemas import MessageEvent, ReplyContext


def wechat_message_to_event(message: NormalizedWechatMessage) -> MessageEvent:
    reply_context = None
    if message.reply is not None:
        reply_context = ReplyContext(
            source_local_id=message.reply.local_id,
            source_server_id=message.reply.server_id,
            sender_id=message.reply.sender_id,
            sender_name=message.reply.sender_name,
            raw_type=message.reply.raw_type,
            content=message.reply.content,
        )

    return MessageEvent(
        source=message.source,
        source_account_id=message.source_account_id,
        source_message_id=message.source_message_id,
        event_id=message.event_id,
        conversation_id=message.conversation_id,
        conversation_type=message.conversation_type.value,
        conversation_name=message.conversation_name,
        sender_type=message.sender_type.value,
        sender_id=message.sender_id,
        sender_name=message.sender_name,
        message_type=message.message_type.value,
        raw_type=message.raw_type,
        content=message.content,
        timestamp=message.timestamp,
        is_mentioned=message.is_mentioned,
        is_self=message.is_self,
        source_local_id=message.source_local_id,
        source_server_id=message.source_server_id,
        source_message_id_is_fallback=message.source_message_id_is_fallback,
        reply_context=reply_context,
        reply_to_message_id=None,
        attachments=[],
    )
