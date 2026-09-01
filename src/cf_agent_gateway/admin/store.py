from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from cf_agent_gateway.admin.schemas import (
    AdminConversationItem,
    AdminDeliveryItem,
    AdminMessageItem,
    AdminPage,
    AdminResponsePartItem,
    AdminThreadDeliverySummary,
    AdminThreadDetail,
    AdminThreadSourceBindingItem,
    AdminThreadTimelineItem,
    AdminTimelineDeliveryItem,
)
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryStatus
from cf_agent_gateway.identity.models import SourceIdentityMapping
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.message.schemas import MessageResponse
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.task.model.models import HermesDispatchRecord
from cf_agent_gateway.workspace.models import AIThread, ThreadSourceBinding


@dataclass(frozen=True, slots=True)
class AdminQuery:
    start_time: datetime | None = None
    end_time: datetime | None = None
    identity_id: str | None = None
    source: str | None = None
    source_account_id: str | None = None
    conversation_id: str | None = None
    limit: int = 50
    offset: int = 0


class AdminArchiveStore:
    """Read archive and operational history without mutating runtime state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_conversations(self, query: AdminQuery) -> AdminPage[AdminConversationItem]:
        stats = (
            select(
                Message.source.label("source"),
                Message.source_account_id.label("source_account_id"),
                Message.conversation_id.label("conversation_id"),
                func.count(Message.id).label("message_count"),
                func.min(Message.occurred_at).label("first_message_at"),
                func.max(Message.occurred_at).label("last_message_at"),
            )
            .group_by(Message.source, Message.source_account_id, Message.conversation_id)
            .subquery()
        )
        predicates = self._conversation_predicates(query)
        statement = (
            select(
                Conversation,
                stats.c.message_count,
                stats.c.first_message_at,
                stats.c.last_message_at,
            )
            .outerjoin(
                stats,
                and_(
                    stats.c.source == Conversation.source,
                    stats.c.source_account_id == Conversation.source_account_id,
                    stats.c.conversation_id == Conversation.conversation_id,
                ),
            )
            .where(*predicates)
            .order_by(stats.c.last_message_at.desc(), Conversation.id.desc())
            .offset(query.offset)
            .limit(query.limit)
        )
        rows = self._session.execute(statement).all()
        total = self._session.scalar(
            select(func.count()).select_from(Conversation).where(*predicates)
        )
        items = [
            AdminConversationItem(
                id=conversation.id,
                source=conversation.source,
                source_account_id=conversation.source_account_id,
                conversation_id=conversation.conversation_id,
                conversation_type=conversation.conversation_type,
                conversation_name=conversation.conversation_name,
                message_count=message_count or 0,
                first_message_at=first_message_at,
                last_message_at=last_message_at,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
            )
            for conversation, message_count, first_message_at, last_message_at in rows
        ]
        return self._page(items, total, query)

    def list_messages(self, query: AdminQuery) -> AdminPage[AdminMessageItem]:
        identity_mapping = aliased(SourceIdentityMapping)
        predicates = self._message_predicates(query)
        statement = (
            select(
                Message,
                identity_mapping.enterprise_identity_id,
                HermesDispatchRecord,
                ResponseRecord,
            )
            .outerjoin(identity_mapping, self._message_identity_join(identity_mapping))
            .outerjoin(HermesDispatchRecord, HermesDispatchRecord.message_id == Message.id)
            .outerjoin(ResponseRecord, ResponseRecord.message_id == Message.id)
            .where(*predicates)
            .options(selectinload(Message.attachments))
            .order_by(Message.occurred_at.desc(), Message.id.desc())
            .offset(query.offset)
            .limit(query.limit)
        )
        rows = self._session.execute(statement).all()
        total = self._session.scalar(select(func.count(Message.id)).where(*predicates))
        items = [
            self._message_item(message, mapped_identity_id, dispatch, response)
            for message, mapped_identity_id, dispatch, response in rows
        ]
        return self._page(items, total, query)

    def get_thread(self, thread_id: str, query: AdminQuery) -> AdminThreadDetail | None:
        thread = self._session.scalar(select(AIThread).where(AIThread.id == thread_id))
        if thread is None:
            return None
        if query.identity_id is not None and not self._identity_has_thread_access(
            thread_id, query.identity_id
        ):
            return None

        return AdminThreadDetail(
            id=thread.id,
            workspace_id=thread.workspace_id,
            agent_profile_id=thread.agent_profile_id,
            thread_type=thread.thread_type.value,
            thread_policy=thread.thread_policy.value if thread.thread_policy is not None else None,
            thread_key=thread.thread_key,
            status=thread.status.value,
            hermes_thread_id=thread.hermes_thread_id,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            last_active_at=thread.last_active_at,
            source_bindings=self._thread_source_bindings(thread_id, query),
            timeline=self._thread_timeline(thread_id, query),
            delivery_summary=self._thread_delivery_summary(thread_id, query),
        )

    def list_deliveries(self, query: AdminQuery) -> AdminPage[AdminDeliveryItem]:
        predicates = self._delivery_predicates(query)
        statement = (
            select(DeliveryOutboxRecord, ResponseRecord, HermesDispatchRecord)
            .join(ResponseRecord, ResponseRecord.response_id == DeliveryOutboxRecord.response_id)
            .outerjoin(
                HermesDispatchRecord,
                HermesDispatchRecord.message_id == ResponseRecord.message_id,
            )
            .where(*predicates)
            .order_by(DeliveryOutboxRecord.created_at.desc(), DeliveryOutboxRecord.id.desc())
            .offset(query.offset)
            .limit(query.limit)
        )
        rows = self._session.execute(statement).all()
        total = self._session.scalar(
            select(func.count(DeliveryOutboxRecord.id))
            .join(ResponseRecord, ResponseRecord.response_id == DeliveryOutboxRecord.response_id)
            .outerjoin(
                HermesDispatchRecord,
                HermesDispatchRecord.message_id == ResponseRecord.message_id,
            )
            .where(*predicates)
        )
        items = [
            AdminDeliveryItem(
                id=delivery.id,
                response_id=delivery.response_id,
                message_id=response.message_id,
                identity_id=dispatch.enterprise_identity_id if dispatch is not None else None,
                workspace_id=response.workspace_id,
                ai_thread_id=response.ai_thread_id,
                channel=delivery.channel,
                account_id=delivery.account_id,
                conversation_id=delivery.conversation_id,
                status=delivery.status.value,
                next_part_ordinal=delivery.next_part_ordinal,
                attempt_count=delivery.attempt_count,
                available_at=delivery.available_at,
                claimed_at=delivery.claimed_at,
                completed_at=delivery.completed_at,
                last_error_code=delivery.last_error_code,
                created_at=delivery.created_at,
                updated_at=delivery.updated_at,
            )
            for delivery, response, dispatch in rows
        ]
        return self._page(items, total, query)

    def _conversation_predicates(self, query: AdminQuery) -> list[object]:
        predicates: list[object] = []
        if query.source is not None:
            predicates.append(Conversation.source == query.source)
        if query.source_account_id is not None:
            predicates.append(Conversation.source_account_id == query.source_account_id)
        if query.conversation_id is not None:
            predicates.append(Conversation.conversation_id == query.conversation_id)

        if query.start_time is not None or query.end_time is not None or query.identity_id:
            matching_message = select(Message.id).where(
                Message.source == Conversation.source,
                Message.source_account_id == Conversation.source_account_id,
                Message.conversation_id == Conversation.conversation_id,
            )
            if query.start_time is not None:
                matching_message = matching_message.where(Message.occurred_at >= query.start_time)
            if query.end_time is not None:
                matching_message = matching_message.where(Message.occurred_at < query.end_time)
            if query.identity_id is not None:
                matching_message = matching_message.where(
                    self._message_identity_predicate(query.identity_id)
                )
            predicates.append(exists(matching_message.correlate(Conversation)))
        return predicates

    def _message_predicates(self, query: AdminQuery) -> list[object]:
        predicates: list[object] = []
        if query.start_time is not None:
            predicates.append(Message.occurred_at >= query.start_time)
        if query.end_time is not None:
            predicates.append(Message.occurred_at < query.end_time)
        if query.source is not None:
            predicates.append(Message.source == query.source)
        if query.source_account_id is not None:
            predicates.append(Message.source_account_id == query.source_account_id)
        if query.conversation_id is not None:
            predicates.append(Message.conversation_id == query.conversation_id)
        if query.identity_id is not None:
            predicates.append(self._message_identity_predicate(query.identity_id))
        return predicates

    def _message_identity_predicate(self, identity_id: str) -> object:
        dispatch_exists = exists(
            select(HermesDispatchRecord.id)
            .where(HermesDispatchRecord.message_id == Message.id)
            .correlate(Message)
        )
        dispatch_match = exists(
            select(HermesDispatchRecord.id)
            .where(
                HermesDispatchRecord.message_id == Message.id,
                HermesDispatchRecord.enterprise_identity_id == identity_id,
            )
            .correlate(Message)
        )
        mapping_match = exists(
            select(SourceIdentityMapping.id)
            .where(
                self._message_identity_join(SourceIdentityMapping),
                SourceIdentityMapping.enterprise_identity_id == identity_id,
            )
            .correlate(Message)
        )
        return or_(dispatch_match, and_(~dispatch_exists, mapping_match))

    @staticmethod
    def _message_identity_join(mapping: object) -> object:
        return and_(
            mapping.platform == Message.source,  # type: ignore[attr-defined]
            mapping.account_id == Message.source_account_id,  # type: ignore[attr-defined]
            mapping.sender_id == Message.sender_id,  # type: ignore[attr-defined]
        )

    def _identity_has_thread_access(self, thread_id: str, identity_id: str) -> bool:
        statement = select(
            exists().where(
                HermesDispatchRecord.ai_thread_id == thread_id,
                HermesDispatchRecord.enterprise_identity_id == identity_id,
            )
        )
        return bool(self._session.scalar(statement))

    def _thread_source_bindings(
        self, thread_id: str, query: AdminQuery
    ) -> list[AdminThreadSourceBindingItem]:
        predicates: list[object] = [ThreadSourceBinding.ai_thread_id == thread_id]
        if query.source is not None:
            predicates.append(ThreadSourceBinding.platform == query.source)
        if query.source_account_id is not None:
            predicates.append(ThreadSourceBinding.account_id == query.source_account_id)
        if query.conversation_id is not None:
            predicates.append(ThreadSourceBinding.physical_conversation_id == query.conversation_id)
        statement = (
            select(ThreadSourceBinding)
            .where(*predicates)
            .order_by(ThreadSourceBinding.created_at, ThreadSourceBinding.id)
        )
        return [
            AdminThreadSourceBindingItem.model_validate(binding)
            for binding in self._session.scalars(statement)
        ]

    def _thread_timeline(
        self, thread_id: str, query: AdminQuery
    ) -> AdminPage[AdminThreadTimelineItem]:
        predicates = self._thread_message_predicates(thread_id, query)
        statement = (
            select(Message, HermesDispatchRecord, ResponseRecord)
            .join(HermesDispatchRecord, HermesDispatchRecord.message_id == Message.id)
            .outerjoin(ResponseRecord, ResponseRecord.message_id == Message.id)
            .where(*predicates)
            .options(
                selectinload(Message.attachments),
                selectinload(ResponseRecord.parts),
            )
            .order_by(Message.occurred_at, Message.id)
            .offset(query.offset)
            .limit(query.limit)
        )
        rows = self._session.execute(statement).all()
        total = self._session.scalar(
            select(func.count(Message.id))
            .join(HermesDispatchRecord, HermesDispatchRecord.message_id == Message.id)
            .where(*predicates)
        )
        response_ids = [response.response_id for _, _, response in rows if response is not None]
        deliveries_by_response = self._timeline_deliveries(response_ids)
        items: list[AdminThreadTimelineItem] = []
        for message, dispatch, response in rows:
            base = self._message_item(
                message,
                dispatch.enterprise_identity_id,
                dispatch,
                response,
            ).model_dump()
            response_parts = []
            deliveries = []
            if response is not None:
                response_parts = [
                    AdminResponsePartItem(
                        ordinal=part.ordinal,
                        part_type=part.part_type.value,
                        text=part.text,
                        artifact_id=part.artifact_id,
                    )
                    for part in response.parts
                ]
                deliveries = deliveries_by_response.get(response.response_id, [])
            items.append(
                AdminThreadTimelineItem(
                    **base,
                    response_parts=response_parts,
                    deliveries=deliveries,
                )
            )
        return self._page(items, total, query)

    def _timeline_deliveries(
        self, response_ids: list[str]
    ) -> dict[str, list[AdminTimelineDeliveryItem]]:
        if not response_ids:
            return {}
        statement = (
            select(DeliveryOutboxRecord)
            .where(DeliveryOutboxRecord.response_id.in_(response_ids))
            .order_by(DeliveryOutboxRecord.response_id, DeliveryOutboxRecord.id)
        )
        deliveries: dict[str, list[AdminTimelineDeliveryItem]] = {}
        for delivery in self._session.scalars(statement):
            deliveries.setdefault(delivery.response_id, []).append(
                AdminTimelineDeliveryItem(
                    id=delivery.id,
                    status=delivery.status.value,
                    attempt_count=delivery.attempt_count,
                    completed_at=delivery.completed_at,
                    last_error_code=delivery.last_error_code,
                )
            )
        return deliveries

    def _thread_delivery_summary(
        self, thread_id: str, query: AdminQuery
    ) -> AdminThreadDeliverySummary:
        predicates = self._thread_message_predicates(thread_id, query)
        statement = (
            select(DeliveryOutboxRecord.status, func.count(DeliveryOutboxRecord.id))
            .join(ResponseRecord, ResponseRecord.response_id == DeliveryOutboxRecord.response_id)
            .join(
                HermesDispatchRecord,
                HermesDispatchRecord.message_id == ResponseRecord.message_id,
            )
            .join(Message, Message.id == HermesDispatchRecord.message_id)
            .where(*predicates)
            .group_by(DeliveryOutboxRecord.status)
        )
        counts = {status.value: count for status, count in self._session.execute(statement)}
        return AdminThreadDeliverySummary(
            total=sum(counts.values()),
            queued=counts.get(DeliveryStatus.QUEUED.value, 0),
            delivering=counts.get(DeliveryStatus.DELIVERING.value, 0),
            delivered=counts.get(DeliveryStatus.DELIVERED.value, 0),
            failed=counts.get(DeliveryStatus.FAILED.value, 0),
            uncertain=counts.get(DeliveryStatus.UNCERTAIN.value, 0),
        )

    @staticmethod
    def _thread_message_predicates(thread_id: str, query: AdminQuery) -> list[object]:
        predicates: list[object] = [HermesDispatchRecord.ai_thread_id == thread_id]
        if query.identity_id is not None:
            predicates.append(HermesDispatchRecord.enterprise_identity_id == query.identity_id)
        if query.start_time is not None:
            predicates.append(Message.occurred_at >= query.start_time)
        if query.end_time is not None:
            predicates.append(Message.occurred_at < query.end_time)
        if query.source is not None:
            predicates.append(Message.source == query.source)
        if query.source_account_id is not None:
            predicates.append(Message.source_account_id == query.source_account_id)
        if query.conversation_id is not None:
            predicates.append(Message.conversation_id == query.conversation_id)
        return predicates

    @staticmethod
    def _delivery_predicates(query: AdminQuery) -> list[object]:
        predicates: list[object] = []
        if query.start_time is not None:
            predicates.append(DeliveryOutboxRecord.created_at >= query.start_time)
        if query.end_time is not None:
            predicates.append(DeliveryOutboxRecord.created_at < query.end_time)
        if query.identity_id is not None:
            predicates.append(HermesDispatchRecord.enterprise_identity_id == query.identity_id)
        if query.source is not None:
            predicates.append(DeliveryOutboxRecord.channel == query.source)
        if query.source_account_id is not None:
            predicates.append(DeliveryOutboxRecord.account_id == query.source_account_id)
        if query.conversation_id is not None:
            predicates.append(DeliveryOutboxRecord.conversation_id == query.conversation_id)
        return predicates

    @staticmethod
    def _message_item(
        message: Message,
        mapped_identity_id: str | None,
        dispatch: HermesDispatchRecord | None,
        response: ResponseRecord | None,
    ) -> AdminMessageItem:
        data = MessageResponse.model_validate(message).model_dump()
        identity_id = (
            dispatch.enterprise_identity_id if dispatch is not None else mapped_identity_id
        )
        return AdminMessageItem(
            **data,
            identity_id=identity_id,
            ai_thread_id=dispatch.ai_thread_id if dispatch is not None else None,
            dispatch_status=dispatch.status.value if dispatch is not None else None,
            response_id=response.response_id if response is not None else None,
            response_status=response.status.value if response is not None else None,
        )

    @staticmethod
    def _page(items: list[object], total: int | None, query: AdminQuery) -> AdminPage:
        return AdminPage(
            items=items,
            total=total or 0,
            limit=query.limit,
            offset=query.offset,
        )
