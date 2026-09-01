from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.agent_profile import (
    UNKNOWN_GROUP_TYPE_KEY,
    AgentProfile,
    AgentProfileRevisionConflictError,
    AgentProfileRevisionImmutableError,
    AgentProfileStatus,
    AgentProfileStore,
    ConversationGroupTypeBinding,
    ConversationNotGroupError,
    GroupType,
    GroupTypeConflictError,
    GroupTypeStatus,
    InvalidGroupThreadPolicyError,
    ThreadPolicy,
    UnknownGroupTypeNotConfiguredError,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.message.models import Conversation


@pytest.fixture
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def create_profile(
    store: AgentProfileStore,
    *,
    revision: int = 1,
    model: str = "gpt-5.2",
) -> AgentProfile:
    profile, created = store.create_agent_profile(
        profile_key="group-assistant",
        revision=revision,
        provider="openai",
        external_profile_ref=f"hermes-group-v{revision}",
        model=model,
    )
    assert created
    return profile


def create_conversation(
    session: Session,
    *,
    conversation_id: str,
    conversation_type: str = "group",
    source_account_id: str = "bot-001",
) -> Conversation:
    conversation = Conversation(
        source="wechat",
        source_account_id=source_account_id,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
    )
    session.add(conversation)
    session.commit()
    return conversation


def test_models_define_required_fields_and_storage_boundaries() -> None:
    assert set(AgentProfile.__table__.columns.keys()) == {
        "id",
        "profile_key",
        "revision",
        "provider",
        "external_profile_ref",
        "model",
        "status",
        "created_at",
        "updated_at",
    }
    assert {
        "id",
        "type_key",
        "display_name",
        "agent_profile_id",
        "thread_policy",
        "status",
    } <= set(GroupType.__table__.columns.keys())
    assert {"id", "conversation_id", "group_type_id"} <= set(
        ConversationGroupTypeBinding.__table__.columns.keys()
    )

    all_columns = {
        column.name
        for table in (
            AgentProfile.__table__,
            GroupType.__table__,
            ConversationGroupTypeBinding.__table__,
        )
        for column in table.columns
    }
    assert all("prompt" not in column_name for column_name in all_columns)
    assert all("skill" not in column_name for column_name in all_columns)


def test_models_define_revision_binding_and_foreign_key_constraints() -> None:
    profile_uniques = {
        tuple(constraint.columns.keys())
        for constraint in AgentProfile.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    profile_checks = {
        constraint.name
        for constraint in AgentProfile.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    binding_uniques = {
        tuple(constraint.columns.keys())
        for constraint in ConversationGroupTypeBinding.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    foreign_keys = {
        (foreign_key.parent.name, foreign_key.target_fullname, foreign_key.ondelete)
        for table in (GroupType.__table__, ConversationGroupTypeBinding.__table__)
        for foreign_key in table.foreign_keys
        if isinstance(foreign_key, ForeignKey)
    }

    assert ("profile_key", "revision") in profile_uniques
    assert "ck_agent_profile_positive_revision" in profile_checks
    assert ("conversation_id",) in binding_uniques
    assert (
        "agent_profile_id",
        "agent_profiles.id",
        "RESTRICT",
    ) in foreign_keys
    assert ("conversation_id", "conversations.id", "CASCADE") in foreign_keys
    assert ("group_type_id", "group_types.id", "RESTRICT") in foreign_keys


def test_agent_profile_revisions_are_idempotent_distinct_and_ordered(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        first = create_profile(store, revision=1)
        duplicate, created = store.create_agent_profile(
            profile_key="group-assistant",
            revision=1,
            provider="openai",
            external_profile_ref="hermes-group-v1",
            model="gpt-5.2",
        )
        second = create_profile(store, revision=2, model="gpt-5.3")

        assert not created
        assert duplicate.id == first.id
        assert second.id != first.id
        assert [
            profile.revision for profile in store.list_agent_profile_revisions("group-assistant")
        ] == [1, 2]
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 2


def test_existing_revision_rejects_a_different_snapshot(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        create_profile(store)

        with pytest.raises(AgentProfileRevisionConflictError) as exc_info:
            store.create_agent_profile(
                profile_key="group-assistant",
                revision=1,
                provider="openai",
                external_profile_ref="hermes-group-v1",
                model="different-model",
            )

        assert exc_info.value.code == "agent_profile_revision_conflict"


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("profile_key", "renamed-profile"),
        ("id", "replacement-id"),
        ("revision", 2),
        ("provider", "different-provider"),
        ("external_profile_ref", "different-external-profile"),
        ("model", "different-model"),
        ("created_at", datetime(2020, 1, 1, tzinfo=UTC)),
    ],
)
def test_agent_profile_revision_snapshot_cannot_be_updated(
    session_factory_fixture: sessionmaker[Session],
    field_name: str,
    replacement: object,
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)
        original = getattr(profile, field_name)

        setattr(profile, field_name, replacement)
        with pytest.raises(AgentProfileRevisionImmutableError) as exc_info:
            session.commit()
        session.rollback()
        session.refresh(profile)

        assert field_name in exc_info.value.changed_fields
        assert getattr(profile, field_name) == original


def test_agent_profile_status_can_change_without_mutating_revision(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)

        updated = store.set_agent_profile_status(profile.id, AgentProfileStatus.ARCHIVED)

        assert updated.status is AgentProfileStatus.ARCHIVED
        assert updated.profile_key == "group-assistant"
        assert updated.revision == 1
        assert updated.model == "gpt-5.2"

        with pytest.raises(AgentProfileRevisionConflictError):
            store.create_agent_profile(
                profile_key="group-assistant",
                revision=1,
                provider="openai",
                external_profile_ref="hermes-group-v1",
                model="gpt-5.2",
            )


def test_database_rejects_bulk_revision_updates_but_allows_status_updates(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)

        with pytest.raises(IntegrityError, match="agent profile revision is immutable"):
            session.execute(
                update(AgentProfile)
                .where(AgentProfile.id == profile.id)
                .values(model="bulk-overwrite")
            )
        session.rollback()

        session.execute(
            update(AgentProfile)
            .where(AgentProfile.id == profile.id)
            .values(status=AgentProfileStatus.DISABLED)
        )
        session.commit()
        session.refresh(profile)

        assert profile.model == "gpt-5.2"
        assert profile.status is AgentProfileStatus.DISABLED


def test_agent_profile_revision_cannot_be_deleted_and_replaced(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)

        session.delete(profile)
        with pytest.raises(AgentProfileRevisionImmutableError):
            session.commit()
        session.rollback()

        with pytest.raises(IntegrityError, match="agent profile revision is immutable"):
            session.execute(delete(AgentProfile).where(AgentProfile.id == profile.id))
        session.rollback()

        assert store.get_agent_profile(profile.id) is not None


def test_sqlite_enforces_v2_foreign_keys(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
        session.add(
            GroupType(
                id="missing-profile-group-type",
                type_key="missing_profile",
                display_name="Missing profile",
                agent_profile_id="missing-profile",
                thread_policy=ThreadPolicy.GROUP_SHARED,
            )
        )

        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            session.commit()
        session.rollback()


def test_group_type_create_is_idempotent_and_conflicting_create_is_rejected(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)
        first, created = store.create_group_type(
            type_key="project_group",
            display_name="Project group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        duplicate, duplicate_created = store.create_group_type(
            type_key="project_group",
            display_name="Project group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )

        assert created
        assert not duplicate_created
        assert duplicate.id == first.id
        with pytest.raises(GroupTypeConflictError):
            store.create_group_type(
                type_key="project_group",
                display_name="Different name",
                agent_profile_id=profile.id,
                thread_policy=ThreadPolicy.GROUP_SENDER,
            )


def test_group_type_rejects_private_thread_policy(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)

        with pytest.raises(InvalidGroupThreadPolicyError):
            store.create_group_type(
                type_key="invalid_group",
                display_name="Invalid group",
                agent_profile_id=profile.id,
                thread_policy=ThreadPolicy.PRIVATE_SENDER,
            )

        assert session.scalar(select(func.count()).select_from(GroupType)) == 0
        session.add(
            GroupType(
                id="invalid-private-policy",
                type_key="invalid_private_policy",
                display_name="Invalid private policy",
                agent_profile_id=profile.id,
                thread_policy=ThreadPolicy.PRIVATE_SENDER,
            )
        )
        with pytest.raises(IntegrityError, match="ck_group_type_group_thread_policy"):
            session.commit()
        session.rollback()


def test_group_type_can_move_to_a_new_immutable_profile_revision(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        first_profile = create_profile(store, revision=1)
        second_profile = create_profile(store, revision=2, model="gpt-5.3")
        group_type, created = store.upsert_group_type(
            type_key="project_group",
            display_name="Project group",
            agent_profile_id=first_profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        updated, updated_created = store.upsert_group_type(
            type_key="project_group",
            display_name="Project room",
            agent_profile_id=second_profile.id,
            thread_policy=ThreadPolicy.GROUP_SHARED,
            status=GroupTypeStatus.DISABLED,
        )

        assert created
        assert not updated_created
        assert updated.id == group_type.id
        assert updated.agent_profile_id == second_profile.id
        assert updated.thread_policy is ThreadPolicy.GROUP_SHARED
        assert updated.status is GroupTypeStatus.DISABLED
        assert first_profile.model == "gpt-5.2"


def test_unbound_group_falls_back_to_unknown_group_and_binding_is_reusable(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)
        conversation = create_conversation(session, conversation_id="group-001")

        with pytest.raises(UnknownGroupTypeNotConfiguredError):
            store.resolve_group_type(conversation.id)

        unknown_group, _ = store.create_group_type(
            type_key=UNKNOWN_GROUP_TYPE_KEY,
            display_name="Unknown group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        project_group, _ = store.create_group_type(
            type_key="project_group",
            display_name="Project group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SHARED,
        )
        assert store.resolve_group_type(conversation.id).id == unknown_group.id

        first_binding, created = store.bind_conversation_group_type(
            conversation_record_id=conversation.id,
            group_type_id=project_group.id,
        )
        duplicate, duplicate_created = store.bind_conversation_group_type(
            conversation_record_id=conversation.id,
            group_type_id=project_group.id,
        )
        rebound, rebound_created = store.bind_conversation_group_type(
            conversation_record_id=conversation.id,
            group_type_id=unknown_group.id,
        )

        assert created
        assert not duplicate_created
        assert not rebound_created
        assert duplicate.id == first_binding.id == rebound.id
        assert store.resolve_group_type(conversation.id).id == unknown_group.id
        assert session.scalar(select(func.count()).select_from(ConversationGroupTypeBinding)) == 1


def test_unknown_group_resolution_preserves_disabled_profile_reference(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)
        store.set_agent_profile_status(profile.id, AgentProfileStatus.DISABLED)
        unknown_group, _ = store.create_group_type(
            type_key=UNKNOWN_GROUP_TYPE_KEY,
            display_name="Unknown group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        conversation = create_conversation(session, conversation_id="group-disabled-profile")

        resolved_group = store.resolve_group_type(conversation.id)

        assert resolved_group is not None
        assert resolved_group.id == unknown_group.id
        assert resolved_group.agent_profile_id == profile.id
        resolved_profile = store.get_agent_profile(resolved_group.agent_profile_id)
        assert resolved_profile is not None
        assert resolved_profile.status is AgentProfileStatus.DISABLED


def test_private_conversation_does_not_use_unknown_group(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)
        unknown_group, _ = store.create_group_type(
            type_key=UNKNOWN_GROUP_TYPE_KEY,
            display_name="Unknown group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        conversation = create_conversation(
            session,
            conversation_id="private-001",
            conversation_type="private",
        )

        assert store.resolve_group_type(conversation.id) is None
        with pytest.raises(ConversationNotGroupError):
            store.bind_conversation_group_type(
                conversation_record_id=conversation.id,
                group_type_id=unknown_group.id,
            )


def test_same_external_conversation_id_is_bound_per_conversation_record(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        store = AgentProfileStore(session)
        profile = create_profile(store)
        first_type, _ = store.create_group_type(
            type_key="first_group",
            display_name="First group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SHARED,
        )
        second_type, _ = store.create_group_type(
            type_key="second_group",
            display_name="Second group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        first_conversation = create_conversation(
            session,
            conversation_id="shared-external-id",
            source_account_id="bot-001",
        )
        second_conversation = create_conversation(
            session,
            conversation_id="shared-external-id",
            source_account_id="bot-002",
        )

        store.bind_conversation_group_type(
            conversation_record_id=first_conversation.id,
            group_type_id=first_type.id,
        )
        store.bind_conversation_group_type(
            conversation_record_id=second_conversation.id,
            group_type_id=second_type.id,
        )

        assert store.resolve_group_type(first_conversation.id).id == first_type.id
        assert store.resolve_group_type(second_conversation.id).id == second_type.id


def test_writes_refresh_stale_rows_before_deciding_an_update_is_unnecessary(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as first_session, session_factory_fixture() as second_session:
        first_store = AgentProfileStore(first_session)
        second_store = AgentProfileStore(second_session)
        profile = create_profile(first_store)
        first_group_type, _ = first_store.create_group_type(
            type_key="first_group",
            display_name="First group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        second_group_type, _ = first_store.create_group_type(
            type_key="second_group",
            display_name="Second group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SHARED,
        )
        conversation = create_conversation(first_session, conversation_id="group-001")
        first_store.bind_conversation_group_type(
            conversation_record_id=conversation.id,
            group_type_id=first_group_type.id,
        )

        second_store.set_agent_profile_status(profile.id, AgentProfileStatus.DISABLED)
        second_store.upsert_group_type(
            type_key="first_group",
            display_name="Changed by second session",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SHARED,
        )
        second_store.bind_conversation_group_type(
            conversation_record_id=conversation.id,
            group_type_id=second_group_type.id,
        )

        first_store.set_agent_profile_status(profile.id, AgentProfileStatus.ACTIVE)
        first_store.upsert_group_type(
            type_key="first_group",
            display_name="First group",
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )
        first_store.bind_conversation_group_type(
            conversation_record_id=conversation.id,
            group_type_id=first_group_type.id,
        )
        second_session.expire_all()

        assert second_store.get_agent_profile(profile.id).status is AgentProfileStatus.ACTIVE
        refreshed_group_type = second_store.get_group_type(first_group_type.id)
        assert refreshed_group_type is not None
        assert refreshed_group_type.display_name == "First group"
        assert refreshed_group_type.thread_policy is ThreadPolicy.GROUP_SENDER
        refreshed_binding = second_store.get_conversation_group_type_binding(conversation.id)
        assert refreshed_binding is not None
        assert refreshed_binding.group_type_id == first_group_type.id
