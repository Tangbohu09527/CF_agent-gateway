from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from cf_agent_gateway.message.models import Attachment, Conversation, Message
from cf_agent_gateway.message.schemas import MessageEvent


class MessageStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: MessageEvent) -> tuple[Message, bool]:
        existing = self._get_by_event_id(event.event_id)
        if existing is not None:
            return existing, False

        existing = self._get_by_source_message(
            source=event.source,
            source_account_id=event.source_account_id,
            conversation_id=event.conversation_id,
            source_message_id=event.source_message_id,
        )
        if existing is not None:
            return existing, False

        conversation = self._get_conversation(
            source=event.source,
            source_account_id=event.source_account_id,
            conversation_id=event.conversation_id,
        )
        if conversation is None:
            conversation = Conversation(
                source=event.source,
                source_account_id=event.source_account_id,
                conversation_id=event.conversation_id,
                conversation_type=event.conversation_type,
                conversation_name=event.conversation_name,
            )
            self._session.add(conversation)
        else:
            conversation.conversation_type = event.conversation_type
            conversation.conversation_name = event.conversation_name

        message = Message(
            event_id=event.event_id,
            source=event.source,
            source_account_id=event.source_account_id,
            source_message_id=event.source_message_id,
            conversation_id=event.conversation_id,
            conversation_type=event.conversation_type,
            is_mentioned=event.is_mentioned,
            is_self=event.is_self,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            message_type=event.message_type,
            content=event.content,
            timestamp=event.timestamp,
            reply_to_message_id=event.reply_to_message_id,
            attachments=[
                Attachment(
                    filename=metadata.filename,
                    file_type=metadata.file_type,
                    mime_type=metadata.mime_type,
                    file_size=metadata.file_size,
                    storage_path=metadata.storage_path,
                    hash=metadata.hash,
                )
                for metadata in event.attachments
            ],
        )
        self._session.add(message)

        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._get_by_event_id(event.event_id)
            if existing is not None:
                return existing, False
            existing = self._get_by_source_message(
                source=event.source,
                source_account_id=event.source_account_id,
                conversation_id=event.conversation_id,
                source_message_id=event.source_message_id,
            )
            if existing is not None:
                return existing, False
            raise

        return message, True

    def get(self, message_id: int) -> Message | None:
        statement = (
            select(Message)
            .where(Message.id == message_id)
            .options(selectinload(Message.attachments))
        )
        return self._session.scalar(statement)

    def list_for_conversation(
        self, *, source: str, source_account_id: str, conversation_id: str
    ) -> list[Message]:
        statement = (
            select(Message)
            .where(
                Message.source == source,
                Message.source_account_id == source_account_id,
                Message.conversation_id == conversation_id,
            )
            .options(selectinload(Message.attachments))
            .order_by(Message.timestamp, Message.id)
        )
        return list(self._session.scalars(statement))

    def _get_by_event_id(self, event_id: str) -> Message | None:
        statement = (
            select(Message)
            .where(Message.event_id == event_id)
            .options(selectinload(Message.attachments))
        )
        return self._session.scalar(statement)

    def _get_by_source_message(
        self,
        *,
        source: str,
        source_account_id: str,
        conversation_id: str,
        source_message_id: str,
    ) -> Message | None:
        statement = (
            select(Message)
            .where(
                Message.source == source,
                Message.source_account_id == source_account_id,
                Message.conversation_id == conversation_id,
                Message.source_message_id == source_message_id,
            )
            .options(selectinload(Message.attachments))
        )
        return self._session.scalar(statement)

    def _get_conversation(
        self, *, source: str, source_account_id: str, conversation_id: str
    ) -> Conversation | None:
        statement = select(Conversation).where(
            Conversation.source == source,
            Conversation.source_account_id == source_account_id,
            Conversation.conversation_id == conversation_id,
        )
        return self._session.scalar(statement)
