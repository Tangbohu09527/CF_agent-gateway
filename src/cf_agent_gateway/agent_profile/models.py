from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.agent_profile.errors import AgentProfileRevisionImmutableError
from cf_agent_gateway.database import Base
from cf_agent_gateway.workspace.models import ThreadPolicy

UNKNOWN_GROUP_TYPE_KEY = "unknown_group"


class AgentProfileStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class GroupTypeStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class AgentProfile(Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint(
            "profile_key",
            "revision",
            name="uq_agent_profile_key_revision",
        ),
        CheckConstraint("revision > 0", name="ck_agent_profile_positive_revision"),
        CheckConstraint(
            "length(trim(profile_key)) > 0",
            name="ck_agent_profile_nonempty_key",
        ),
        CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_agent_profile_nonempty_provider",
        ),
        CheckConstraint(
            "length(trim(external_profile_ref)) > 0",
            name="ck_agent_profile_nonempty_external_ref",
        ),
        CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_agent_profile_nonempty_model",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_key: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(64))
    external_profile_ref: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(255))
    status: Mapped[AgentProfileStatus] = mapped_column(
        Enum(
            AgentProfileStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="agent_profile_status",
        ),
        default=AgentProfileStatus.ACTIVE,
        server_default=AgentProfileStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GroupType(Base):
    __tablename__ = "group_types"
    __table_args__ = (
        UniqueConstraint("type_key", name="uq_group_type_key"),
        CheckConstraint(
            "length(trim(type_key)) > 0",
            name="ck_group_type_nonempty_key",
        ),
        CheckConstraint(
            "length(trim(display_name)) > 0",
            name="ck_group_type_nonempty_display_name",
        ),
        CheckConstraint(
            "length(trim(thread_policy)) > 0",
            name="ck_group_type_nonempty_thread_policy",
        ),
        CheckConstraint(
            "thread_policy IN ('group_shared', 'group_sender')",
            name="ck_group_type_group_thread_policy",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type_key: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(255))
    agent_profile_id: Mapped[str] = mapped_column(
        ForeignKey("agent_profiles.id", ondelete="RESTRICT"), index=True
    )
    thread_policy: Mapped[ThreadPolicy] = mapped_column(
        Enum(
            ThreadPolicy,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="thread_policy",
        )
    )
    status: Mapped[GroupTypeStatus] = mapped_column(
        Enum(
            GroupTypeStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="group_type_status",
        ),
        default=GroupTypeStatus.ACTIVE,
        server_default=GroupTypeStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConversationGroupTypeBinding(Base):
    __tablename__ = "conversation_group_type_bindings"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            name="uq_conversation_group_type_binding_conversation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    group_type_id: Mapped[str] = mapped_column(
        ForeignKey("group_types.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


_IMMUTABLE_REVISION_FIELDS = (
    "id",
    "profile_key",
    "revision",
    "provider",
    "external_profile_ref",
    "model",
    "created_at",
)


@event.listens_for(AgentProfile, "before_update")
def _reject_agent_profile_revision_update(
    _mapper: object,
    _connection: object,
    profile: AgentProfile,
) -> None:
    state = inspect(profile)
    changed_fields = tuple(
        field_name
        for field_name in _IMMUTABLE_REVISION_FIELDS
        if state.attrs[field_name].history.has_changes()
    )
    if changed_fields:
        raise AgentProfileRevisionImmutableError(
            profile_key=profile.profile_key,
            revision=profile.revision,
            changed_fields=changed_fields,
        )


@event.listens_for(AgentProfile, "before_delete")
def _reject_agent_profile_revision_delete(
    _mapper: object,
    _connection: object,
    profile: AgentProfile,
) -> None:
    raise AgentProfileRevisionImmutableError(
        profile_key=profile.profile_key,
        revision=profile.revision,
        changed_fields=("delete",),
    )


event.listen(
    AgentProfile.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_agent_profiles_immutable_revision
        BEFORE UPDATE OF id, profile_key, revision, provider,
            external_profile_ref, model, created_at
        ON agent_profiles
        FOR EACH ROW
        WHEN NEW.id IS NOT OLD.id
          OR NEW.profile_key IS NOT OLD.profile_key
          OR NEW.revision IS NOT OLD.revision
          OR NEW.provider IS NOT OLD.provider
          OR NEW.external_profile_ref IS NOT OLD.external_profile_ref
          OR NEW.model IS NOT OLD.model
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'agent profile revision is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AgentProfile.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_agent_profiles_prevent_delete
        BEFORE DELETE ON agent_profiles
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'agent profile revision is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AgentProfile.__table__,
    "after_create",
    DDL(
        """
        CREATE FUNCTION guard_agent_profile_revision_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'agent profile revision is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.profile_key IS DISTINCT FROM OLD.profile_key
               OR NEW.revision IS DISTINCT FROM OLD.revision
               OR NEW.provider IS DISTINCT FROM OLD.provider
               OR NEW.external_profile_ref IS DISTINCT FROM OLD.external_profile_ref
               OR NEW.model IS DISTINCT FROM OLD.model
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'agent profile revision is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AgentProfile.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_agent_profiles_immutable_revision
        BEFORE UPDATE OR DELETE ON agent_profiles
        FOR EACH ROW
        EXECUTE FUNCTION guard_agent_profile_revision_immutable()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AgentProfile.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS guard_agent_profile_revision_immutable()").execute_if(
        dialect="postgresql"
    ),
)
