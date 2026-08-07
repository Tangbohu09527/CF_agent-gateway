from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.agent_profile import (
    UNKNOWN_GROUP_TYPE_KEY,
    AgentProfileStore,
    ConversationGroupTypeBinding,
    PrivateConversationProfileNotConfiguredError,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.message.models import Conversation
from cf_agent_gateway.routing import RouteResolver
from cf_agent_gateway.workspace.models import ThreadPolicy, ThreadType


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as database_session:
            yield database_session
    finally:
        engine.dispose()


def create_conversation(
    session: Session,
    *,
    account_id: str,
    conversation_id: str,
    conversation_type: str,
) -> Conversation:
    conversation = Conversation(
        source="wechat",
        source_account_id=account_id,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
    )
    session.add(conversation)
    session.commit()
    return conversation


def create_profile(store: AgentProfileStore, profile_key: str) -> str:
    profile, _ = store.create_agent_profile(
        profile_key=profile_key,
        revision=1,
        provider="hermes",
        external_profile_ref=f"profiles/{profile_key}",
        model="hermes-agent",
    )
    return profile.id


def test_private_resolution_is_scoped_by_source_account_and_persisted_binding(
    session: Session,
) -> None:
    store = AgentProfileStore(session)
    first_profile_id = create_profile(store, "first")
    second_profile_id = create_profile(store, "second")
    first = create_conversation(
        session,
        account_id="bot-a",
        conversation_id="same-private-id",
        conversation_type="private",
    )
    second = create_conversation(
        session,
        account_id="bot-b",
        conversation_id="same-private-id",
        conversation_type="private",
    )
    store.bind_conversation_agent_profile(
        conversation_record_id=first.id,
        agent_profile_id=first_profile_id,
    )
    store.bind_conversation_agent_profile(
        conversation_record_id=second.id,
        agent_profile_id=second_profile_id,
    )

    first_route = RouteResolver(session).resolve(
        source="wechat",
        source_account_id="bot-a",
        conversation_id="same-private-id",
        conversation_type=ThreadType.PRIVATE,
        enterprise_identity_id="identity-a",
    )
    second_route = RouteResolver(session).resolve(
        source="wechat",
        source_account_id="bot-b",
        conversation_id="same-private-id",
        conversation_type=ThreadType.PRIVATE,
        enterprise_identity_id="identity-b",
    )

    assert first_route.agent_profile_id == first_profile_id
    assert second_route.agent_profile_id == second_profile_id
    assert first_route.thread_policy is ThreadPolicy.PRIVATE_SENDER
    assert second_route.thread_policy is ThreadPolicy.PRIVATE_SENDER


def test_private_resolution_requires_persisted_profile_binding(session: Session) -> None:
    conversation = create_conversation(
        session,
        account_id="bot-a",
        conversation_id="unbound-private",
        conversation_type="private",
    )

    with pytest.raises(
        PrivateConversationProfileNotConfiguredError,
        match=str(conversation.id),
    ):
        RouteResolver(session).resolve(
            source="wechat",
            source_account_id="bot-a",
            conversation_id=conversation.conversation_id,
            conversation_type=ThreadType.PRIVATE,
            enterprise_identity_id="identity-a",
        )


def test_group_resolution_uses_only_persisted_unknown_group_fallback(
    session: Session,
) -> None:
    store = AgentProfileStore(session)
    profile_id = create_profile(store, "unknown-group-profile")
    unknown_group, _ = store.create_group_type(
        type_key=UNKNOWN_GROUP_TYPE_KEY,
        display_name="Persisted fallback",
        agent_profile_id=profile_id,
        thread_policy=ThreadPolicy.GROUP_SENDER,
    )
    conversation = create_conversation(
        session,
        account_id="bot-a",
        conversation_id="name-and-body-ignored",
        conversation_type="group",
    )

    route = RouteResolver(session).resolve(
        source="wechat",
        source_account_id="bot-a",
        conversation_id=conversation.conversation_id,
        conversation_type=ThreadType.GROUP,
        enterprise_identity_id="identity-a",
    )

    assert route.group_type_id == unknown_group.id
    assert route.agent_profile_id == profile_id
    assert route.thread_policy is ThreadPolicy.GROUP_SENDER
    assert session.scalar(select(func.count()).select_from(ConversationGroupTypeBinding)) == 0
