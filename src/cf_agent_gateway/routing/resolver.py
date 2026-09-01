from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cf_agent_gateway.agent_profile import (
    AgentProfileStatus,
    AgentProfileStore,
    GroupTypeStatus,
)
from cf_agent_gateway.message.models import Conversation
from cf_agent_gateway.routing.errors import (
    RouteAgentProfileUnavailableError,
    RouteConversationNotFoundError,
    RouteConversationTypeConflictError,
    RouteGroupTypeUnavailableError,
)
from cf_agent_gateway.routing.models import ResolvedRoute
from cf_agent_gateway.workspace.models import ThreadPolicy, ThreadType


class RouteResolver:
    """Resolve V2 routing only from scoped, persisted configuration facts."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._profile_store = AgentProfileStore(session)

    def resolve(
        self,
        *,
        source: str,
        source_account_id: str,
        conversation_id: str,
        conversation_type: ThreadType | str,
        enterprise_identity_id: str,
    ) -> ResolvedRoute:
        requested_type = ThreadType(conversation_type)
        conversation = self._session.scalar(
            select(Conversation).where(
                Conversation.source == source,
                Conversation.source_account_id == source_account_id,
                Conversation.conversation_id == conversation_id,
            )
        )
        if conversation is None:
            raise RouteConversationNotFoundError()
        if conversation.conversation_type != requested_type.value:
            raise RouteConversationTypeConflictError(
                persisted_type=conversation.conversation_type,
                requested_type=requested_type.value,
            )

        group_type = None
        if requested_type is ThreadType.PRIVATE:
            profile = self._profile_store.resolve_private_agent_profile(conversation.id)
            thread_policy = ThreadPolicy.PRIVATE_SENDER
        else:
            group_type = self._profile_store.resolve_group_type(conversation.id)
            if group_type is None:
                raise RouteConversationTypeConflictError(
                    persisted_type=conversation.conversation_type,
                    requested_type=requested_type.value,
                )
            if group_type.status is not GroupTypeStatus.ACTIVE:
                raise RouteGroupTypeUnavailableError(group_type.id, group_type.status.value)
            profile = self._profile_store.get_agent_profile(group_type.agent_profile_id)
            if profile is None:
                raise RouteAgentProfileUnavailableError(group_type.agent_profile_id, "missing")
            thread_policy = group_type.thread_policy

        if profile.status is not AgentProfileStatus.ACTIVE:
            raise RouteAgentProfileUnavailableError(profile.id, profile.status.value)

        return ResolvedRoute(
            conversation_record_id=conversation.id,
            source=conversation.source,
            source_account_id=conversation.source_account_id,
            conversation_id=conversation.conversation_id,
            conversation_type=requested_type,
            enterprise_identity_id=enterprise_identity_id,
            group_type_id=group_type.id if group_type is not None else None,
            group_type_key=group_type.type_key if group_type is not None else None,
            agent_profile_id=profile.id,
            agent_profile_key=profile.profile_key,
            agent_profile_reference=profile.external_profile_ref,
            agent_profile_revision=profile.revision,
            thread_policy=thread_policy,
        )
