from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.agent_profile.errors import (
    AgentProfileIdConflictError,
    AgentProfileNotFoundError,
    AgentProfileRevisionConflictError,
    ConversationNotFoundError,
    ConversationNotGroupError,
    GroupTypeConflictError,
    GroupTypeIdConflictError,
    GroupTypeNotFoundError,
    InvalidGroupThreadPolicyError,
    UnknownGroupTypeNotConfiguredError,
)
from cf_agent_gateway.agent_profile.models import (
    UNKNOWN_GROUP_TYPE_KEY,
    AgentProfile,
    AgentProfileStatus,
    ConversationGroupTypeBinding,
    GroupType,
    GroupTypeStatus,
)
from cf_agent_gateway.message.models import Conversation
from cf_agent_gateway.workspace.models import ThreadPolicy


class AgentProfileStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_agent_profile(
        self,
        *,
        profile_key: str,
        revision: int,
        provider: str,
        external_profile_ref: str,
        model: str,
        status: AgentProfileStatus = AgentProfileStatus.ACTIVE,
        agent_profile_id: str | None = None,
    ) -> tuple[AgentProfile, bool]:
        existing = self.get_agent_profile_revision(profile_key, revision)
        if existing is not None:
            return self._same_agent_profile_revision_or_raise(
                existing,
                provider=provider,
                external_profile_ref=external_profile_ref,
                model=model,
                status=status,
            ), False

        profile = AgentProfile(
            id=agent_profile_id or str(uuid4()),
            profile_key=profile_key,
            revision=revision,
            provider=provider,
            external_profile_ref=external_profile_ref,
            model=model,
            status=status,
        )
        self._session.add(profile)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_agent_profile_revision(profile_key, revision)
            if existing is not None:
                return self._same_agent_profile_revision_or_raise(
                    existing,
                    provider=provider,
                    external_profile_ref=external_profile_ref,
                    model=model,
                    status=status,
                ), False
            if self.get_agent_profile(profile.id) is not None:
                raise AgentProfileIdConflictError(profile.id) from None
            raise
        return profile, True

    def get_agent_profile(self, agent_profile_id: str) -> AgentProfile | None:
        return self._session.get(AgentProfile, agent_profile_id, populate_existing=True)

    def get_agent_profile_revision(self, profile_key: str, revision: int) -> AgentProfile | None:
        statement = select(AgentProfile).where(
            AgentProfile.profile_key == profile_key,
            AgentProfile.revision == revision,
        )
        return self._session.scalar(statement.execution_options(populate_existing=True))

    def list_agent_profile_revisions(self, profile_key: str) -> list[AgentProfile]:
        statement = (
            select(AgentProfile)
            .where(AgentProfile.profile_key == profile_key)
            .order_by(AgentProfile.revision, AgentProfile.id)
        )
        return list(self._session.scalars(statement.execution_options(populate_existing=True)))

    def set_agent_profile_status(
        self,
        agent_profile_id: str,
        status: AgentProfileStatus,
    ) -> AgentProfile:
        profile = self.get_agent_profile(agent_profile_id)
        if profile is None:
            raise AgentProfileNotFoundError(agent_profile_id)
        if profile.status != status:
            profile.status = status
            self._session.commit()
        return profile

    def create_group_type(
        self,
        *,
        type_key: str,
        display_name: str,
        agent_profile_id: str,
        thread_policy: ThreadPolicy | str,
        status: GroupTypeStatus = GroupTypeStatus.ACTIVE,
        group_type_id: str | None = None,
    ) -> tuple[GroupType, bool]:
        normalized_thread_policy = self._group_thread_policy(thread_policy)
        existing = self.get_group_type_by_key(type_key)
        if existing is not None:
            return self._same_group_type_or_raise(
                existing,
                display_name=display_name,
                agent_profile_id=agent_profile_id,
                thread_policy=normalized_thread_policy,
                status=status,
            ), False

        self._require_agent_profile(agent_profile_id)
        group_type = GroupType(
            id=group_type_id or str(uuid4()),
            type_key=type_key,
            display_name=display_name,
            agent_profile_id=agent_profile_id,
            thread_policy=normalized_thread_policy,
            status=status,
        )
        self._session.add(group_type)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_group_type_by_key(type_key)
            if existing is not None:
                return self._same_group_type_or_raise(
                    existing,
                    display_name=display_name,
                    agent_profile_id=agent_profile_id,
                    thread_policy=normalized_thread_policy,
                    status=status,
                ), False
            if self.get_group_type(group_type.id) is not None:
                raise GroupTypeIdConflictError(group_type.id) from None
            raise
        return group_type, True

    def upsert_group_type(
        self,
        *,
        type_key: str,
        display_name: str,
        agent_profile_id: str,
        thread_policy: ThreadPolicy | str,
        status: GroupTypeStatus = GroupTypeStatus.ACTIVE,
    ) -> tuple[GroupType, bool]:
        self._require_agent_profile(agent_profile_id)
        values: dict[str, object] = {
            "display_name": display_name,
            "agent_profile_id": agent_profile_id,
            "thread_policy": self._group_thread_policy(thread_policy),
            "status": status,
        }
        existing = self.get_group_type_by_key(type_key)
        if existing is not None:
            self._update_group_type(existing, values)
            return existing, False

        group_type = GroupType(id=str(uuid4()), type_key=type_key, **values)
        self._session.add(group_type)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_group_type_by_key(type_key)
            if existing is None:
                raise
            self._update_group_type(existing, values)
            return existing, False
        return group_type, True

    def get_group_type(self, group_type_id: str) -> GroupType | None:
        return self._session.get(GroupType, group_type_id, populate_existing=True)

    def get_group_type_by_key(self, type_key: str) -> GroupType | None:
        statement = select(GroupType).where(GroupType.type_key == type_key)
        return self._session.scalar(statement.execution_options(populate_existing=True))

    def bind_conversation_group_type(
        self,
        *,
        conversation_record_id: int,
        group_type_id: str,
    ) -> tuple[ConversationGroupTypeBinding, bool]:
        self._require_group_conversation(conversation_record_id)
        self._require_group_type(group_type_id)
        existing = self.get_conversation_group_type_binding(conversation_record_id)
        if existing is not None:
            if existing.group_type_id != group_type_id:
                existing.group_type_id = group_type_id
                self._session.commit()
            return existing, False

        binding = ConversationGroupTypeBinding(
            id=str(uuid4()),
            conversation_id=conversation_record_id,
            group_type_id=group_type_id,
        )
        self._session.add(binding)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_conversation_group_type_binding(conversation_record_id)
            if existing is None:
                raise
            if existing.group_type_id != group_type_id:
                existing.group_type_id = group_type_id
                self._session.commit()
            return existing, False
        return binding, True

    def get_conversation_group_type_binding(
        self, conversation_record_id: int
    ) -> ConversationGroupTypeBinding | None:
        statement = select(ConversationGroupTypeBinding).where(
            ConversationGroupTypeBinding.conversation_id == conversation_record_id
        )
        return self._session.scalar(statement.execution_options(populate_existing=True))

    def get_bound_group_type(self, conversation_record_id: int) -> GroupType | None:
        statement = (
            select(GroupType)
            .join(
                ConversationGroupTypeBinding,
                ConversationGroupTypeBinding.group_type_id == GroupType.id,
            )
            .where(ConversationGroupTypeBinding.conversation_id == conversation_record_id)
        )
        return self._session.scalar(statement.execution_options(populate_existing=True))

    def resolve_group_type(self, conversation_record_id: int) -> GroupType | None:
        conversation = self._require_conversation(conversation_record_id)
        if conversation.conversation_type != "group":
            return None

        bound = self.get_bound_group_type(conversation_record_id)
        if bound is not None:
            return bound

        unknown_group = self.get_group_type_by_key(UNKNOWN_GROUP_TYPE_KEY)
        if unknown_group is None:
            raise UnknownGroupTypeNotConfiguredError
        return unknown_group

    @staticmethod
    def _same_agent_profile_revision_or_raise(
        profile: AgentProfile,
        *,
        provider: str,
        external_profile_ref: str,
        model: str,
        status: AgentProfileStatus,
    ) -> AgentProfile:
        if (
            profile.provider != provider
            or profile.external_profile_ref != external_profile_ref
            or profile.model != model
            or profile.status != status
        ):
            raise AgentProfileRevisionConflictError(profile.profile_key, profile.revision)
        return profile

    @staticmethod
    def _same_group_type_or_raise(
        group_type: GroupType,
        *,
        display_name: str,
        agent_profile_id: str,
        thread_policy: ThreadPolicy,
        status: GroupTypeStatus,
    ) -> GroupType:
        if (
            group_type.display_name != display_name
            or group_type.agent_profile_id != agent_profile_id
            or group_type.thread_policy != thread_policy
            or group_type.status != status
        ):
            raise GroupTypeConflictError(group_type.type_key)
        return group_type

    @staticmethod
    def _group_thread_policy(thread_policy: ThreadPolicy | str) -> ThreadPolicy:
        normalized = ThreadPolicy(thread_policy)
        if normalized is ThreadPolicy.PRIVATE_SENDER:
            raise InvalidGroupThreadPolicyError(normalized.value)
        return normalized

    def _update_group_type(self, group_type: GroupType, values: dict[str, object]) -> None:
        changed = False
        for field_name, value in values.items():
            if getattr(group_type, field_name) != value:
                setattr(group_type, field_name, value)
                changed = True
        if changed:
            self._session.commit()

    def _require_agent_profile(self, agent_profile_id: str) -> AgentProfile:
        profile = self.get_agent_profile(agent_profile_id)
        if profile is None:
            raise AgentProfileNotFoundError(agent_profile_id)
        return profile

    def _require_group_type(self, group_type_id: str) -> GroupType:
        group_type = self.get_group_type(group_type_id)
        if group_type is None:
            raise GroupTypeNotFoundError(group_type_id)
        return group_type

    def _require_conversation(self, conversation_record_id: int) -> Conversation:
        conversation = self._session.get(
            Conversation,
            conversation_record_id,
            populate_existing=True,
        )
        if conversation is None:
            raise ConversationNotFoundError(conversation_record_id)
        return conversation

    def _require_group_conversation(self, conversation_record_id: int) -> Conversation:
        conversation = self._require_conversation(conversation_record_id)
        if conversation.conversation_type != "group":
            raise ConversationNotGroupError(conversation_record_id)
        return conversation
