from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.access import (
    AccessPolicyService,
    AccessPolicyStore,
    ConversationType,
    GatewayAccessPolicy,
    GroupAccessPolicy,
    IdentityStatus,
    InvalidGatewayPolicyKeyError,
    RequestFacts,
    RiskLevel,
    UserAccessPolicy,
    evaluate_access,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.identity.errors import IdentityNotFoundError
from cf_agent_gateway.identity.models import EnterpriseIdentity
from cf_agent_gateway.identity.models import IdentityStatus as StoredIdentityStatus
from cf_agent_gateway.identity.service import IdentityService

AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def create_identity(session: Session, suffix: str = "1") -> str:
    identity = IdentityService(session).create_identity(
        employee_id=f"employee-{suffix}",
        display_name=f"Employee {suffix}",
    )
    return identity.id


def test_create_user_allowlist_policy(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        policy = AccessPolicyService(session).upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:write", "messages:read"},
            allowed_skills={"summarize", "search"},
            valid_from=AT - timedelta(days=1),
            valid_until=AT + timedelta(days=1),
        )

        assert policy.enterprise_identity_id == identity_id
        assert policy.enabled is True
        assert policy.permission_scope == frozenset({"messages:read", "messages:write"})
        assert policy.allowed_skills == frozenset({"search", "summarize"})
        assert policy.created_at is not None
        assert policy.updated_at is not None


def test_repeated_user_policy_write_updates_one_stable_row(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        service = AccessPolicyService(session)
        first = service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:read"},
            allowed_skills={"search"},
        )
        second = service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:write"},
            allowed_skills={"summarize"},
        )

        assert second.id == first.id
        assert second.permission_scope == frozenset({"messages:write"})
        assert second.allowed_skills == frozenset({"summarize"})
        assert session.scalar(select(func.count()).select_from(UserAccessPolicy)) == 1


def test_user_policy_requires_an_existing_enterprise_identity(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        with pytest.raises(IdentityNotFoundError):
            AccessPolicyService(session).upsert_user_policy(
                enterprise_identity_id="missing-identity",
                permission_scope={"messages:read"},
            )

        assert session.scalar(select(func.count()).select_from(UserAccessPolicy)) == 0


def test_missing_user_policy_resolves_user_allowed_false(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)

        facts = AccessPolicyService(session).resolve_identity_facts(identity_id, at=AT)

        assert facts.identity_resolved is True
        assert facts.user_allowed is False


@pytest.mark.parametrize(
    "stored_status",
    [StoredIdentityStatus.DISABLED, StoredIdentityStatus.ARCHIVED],
)
def test_inactive_enterprise_identity_cannot_be_reenabled_by_user_policy(
    session_factory_fixture: sessionmaker[Session],
    stored_status: StoredIdentityStatus,
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        service = AccessPolicyService(session)
        service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:read"},
        )
        identity = session.get(EnterpriseIdentity, identity_id)
        assert identity is not None
        identity.status = stored_status
        session.commit()

        facts = service.resolve_identity_facts(identity_id, at=AT)

        assert facts.identity_status.value == stored_status.value
        assert facts.user_allowed is False
        assert facts.user_permission_scope == frozenset()


def test_disabled_user_policy_resolves_user_allowed_false(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        service = AccessPolicyService(session)
        service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            enabled=False,
            permission_scope={"messages:read"},
            allowed_skills={"search"},
        )

        facts = service.resolve_identity_facts(identity_id, at=AT)

        assert facts.user_allowed is False
        assert facts.user_permission_scope == frozenset()
        assert facts.user_allowed_skills == frozenset()


@pytest.mark.parametrize(
    ("valid_from", "valid_until"),
    [
        (None, AT - timedelta(seconds=1)),
        (AT + timedelta(seconds=1), None),
    ],
)
def test_out_of_window_user_policy_resolves_user_allowed_false(
    session_factory_fixture: sessionmaker[Session],
    valid_from: datetime | None,
    valid_until: datetime | None,
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        service = AccessPolicyService(session)
        service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:read"},
            allowed_skills={"search"},
            valid_from=valid_from,
            valid_until=valid_until,
        )

        assert service.resolve_identity_facts(identity_id, at=AT).user_allowed is False


def test_policy_window_boundaries_are_inclusive(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        service = AccessPolicyService(session)
        service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:read"},
            valid_from=AT,
            valid_until=AT,
        )

        assert service.resolve_identity_facts(identity_id, at=AT).user_allowed is True


def test_store_normalizes_offset_policy_times_before_sqlite_round_trip(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        AccessPolicyStore(session).upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:read"},
            valid_until=datetime(
                2026,
                1,
                15,
                12,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )
        session.expunge_all()

        facts = AccessPolicyService(session).resolve_identity_facts(
            identity_id,
            at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
        )

        assert facts.user_allowed is False


def test_create_group_policy_and_resolve_facts(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        policy = service.upsert_group_policy(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            permission_scope={"messages:read"},
            allowed_skills={"search"},
        )

        facts = service.resolve_conversation_facts(
            conversation_type=ConversationType.GROUP,
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            is_mentioned=True,
            at=AT,
        )

        assert policy.id
        assert facts.group_allowed is True
        assert facts.is_mentioned is True
        assert facts.group_permission_scope == frozenset({"messages:read"})
        assert facts.group_allowed_skills == frozenset({"search"})


@pytest.mark.parametrize("create_disabled_policy", [False, True])
def test_missing_or_disabled_group_resolves_group_allowed_false(
    session_factory_fixture: sessionmaker[Session],
    create_disabled_policy: bool,
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        if create_disabled_policy:
            service.upsert_group_policy(
                source="wechat",
                source_account_id="bot-001",
                conversation_id="group-001",
                enabled=False,
                permission_scope={"messages:read"},
            )

        facts = service.resolve_conversation_facts(
            conversation_type=ConversationType.GROUP,
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            is_mentioned=True,
            at=AT,
        )

        assert facts.group_allowed is False
        assert facts.group_permission_scope == frozenset()


def test_expired_group_policy_resolves_group_allowed_false(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        service.upsert_group_policy(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            permission_scope={"messages:read"},
            valid_until=AT - timedelta(seconds=1),
        )

        facts = service.resolve_conversation_facts(
            conversation_type=ConversationType.GROUP,
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            is_mentioned=True,
            at=AT,
        )

        assert facts.group_allowed is False
        assert facts.group_permission_scope == frozenset()


def test_same_group_id_is_isolated_by_source_account(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        service.upsert_group_policy(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="shared-group",
            permission_scope={"messages:read"},
        )
        service.upsert_group_policy(
            source="wechat",
            source_account_id="bot-002",
            conversation_id="shared-group",
            permission_scope={"messages:write"},
        )

        first = service.resolve_conversation_facts(
            conversation_type=ConversationType.GROUP,
            source="wechat",
            source_account_id="bot-001",
            conversation_id="shared-group",
            is_mentioned=True,
            at=AT,
        )
        second = service.resolve_conversation_facts(
            conversation_type=ConversationType.GROUP,
            source="wechat",
            source_account_id="bot-002",
            conversation_id="shared-group",
            is_mentioned=True,
            at=AT,
        )

        assert first.group_permission_scope == frozenset({"messages:read"})
        assert second.group_permission_scope == frozenset({"messages:write"})


def test_default_gateway_policy_generates_stable_facts(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        policy = service.upsert_gateway_policy(
            permission_scope={"messages:write", "messages:read"},
            allowed_skills={"summarize", "search"},
            allowed_risk_levels={RiskLevel.NORMAL, RiskLevel.LOW},
        )

        first = service.resolve_gateway_policy_facts()
        second = service.resolve_gateway_policy_facts()

        assert policy.policy_key == "default"
        assert first == second
        assert first.system_permission_scope == frozenset({"messages:read", "messages:write"})
        assert first.system_allowed_skills == frozenset({"search", "summarize"})
        assert first.allowed_risk_levels == frozenset({RiskLevel.LOW, RiskLevel.NORMAL})


def test_gateway_policy_key_is_fixed_to_default_in_v1(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        with pytest.raises(ValidationError):
            service.upsert_gateway_policy(
                policy_key="alternate",
                allowed_risk_levels={RiskLevel.NORMAL},
            )
        with pytest.raises(InvalidGatewayPolicyKeyError):
            AccessPolicyStore(session).upsert_gateway_policy(policy_key="alternate")

        assert session.scalar(select(func.count()).select_from(GatewayAccessPolicy)) == 0


def test_json_collections_round_trip_as_frozensets_and_sorted_arrays(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        service = AccessPolicyService(session)
        service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"zeta", "alpha"},
            allowed_skills={"skill-z", "skill-a"},
        )
        service.upsert_group_policy(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            permission_scope={"zeta", "alpha"},
            allowed_skills={"skill-z", "skill-a"},
        )
        service.upsert_gateway_policy(
            permission_scope={"zeta", "alpha"},
            allowed_skills={"skill-z", "skill-a"},
            allowed_risk_levels={RiskLevel.NORMAL, RiskLevel.LOW},
        )
        raw_user = session.execute(
            text(
                "SELECT permission_scope, allowed_skills FROM user_access_policies "
                "WHERE enterprise_identity_id = :identity_id"
            ),
            {"identity_id": identity_id},
        ).one()
        raw_gateway_risks = session.scalar(
            text(
                "SELECT allowed_risk_levels FROM gateway_access_policies "
                "WHERE policy_key = 'default'"
            )
        )
        session.expunge_all()
        store = AccessPolicyStore(session)

        user = store.get_user_policy(identity_id)
        group = store.get_group_policy(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
        )
        gateway = store.get_gateway_policy()

        assert json.loads(raw_user.permission_scope) == ["alpha", "zeta"]
        assert json.loads(raw_user.allowed_skills) == ["skill-a", "skill-z"]
        assert json.loads(raw_gateway_risks) == ["low", "normal"]
        assert user is not None and isinstance(user.permission_scope, frozenset)
        assert group is not None and isinstance(group.allowed_skills, frozenset)
        assert gateway is not None and isinstance(gateway.allowed_risk_levels, frozenset)


def test_persisted_group_and_gateway_policies_cannot_expand_user_permissions(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        service = AccessPolicyService(session)
        service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:read"},
            allowed_skills={"search"},
        )
        service.upsert_group_policy(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            permission_scope={"messages:read", "admin"},
            allowed_skills={"search", "admin"},
        )
        service.upsert_gateway_policy(
            permission_scope={"messages:read", "admin"},
            allowed_skills={"search", "admin"},
            allowed_risk_levels={RiskLevel.NORMAL},
        )

        decision = evaluate_access(
            service.resolve_identity_facts(identity_id, at=AT),
            service.resolve_conversation_facts(
                conversation_type=ConversationType.GROUP,
                source="wechat",
                source_account_id="bot-001",
                conversation_id="group-001",
                is_mentioned=True,
                at=AT,
            ),
            RequestFacts(
                requested_scope=frozenset(),
                requested_skill_ids=frozenset(),
                risk_level=RiskLevel.NORMAL,
            ),
            service.resolve_gateway_policy_facts(),
        )

        assert decision.allowed is True
        assert decision.permission_scope == frozenset({"messages:read"})
        assert decision.allowed_skills == frozenset({"search"})


def test_mention_is_only_the_structured_adapter_fact(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        service.upsert_group_policy(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="text-containing-@bot",
        )

        facts = service.resolve_conversation_facts(
            conversation_type=ConversationType.GROUP,
            source="wechat",
            source_account_id="bot-001",
            conversation_id="text-containing-@bot",
            is_mentioned=False,
            at=AT,
        )
        parameters = inspect.signature(service.resolve_conversation_facts).parameters

        assert facts.group_allowed is True
        assert facts.is_mentioned is False
        assert {"display_name", "message_content", "raw_text"}.isdisjoint(parameters)
        assert "is_mentioned" not in GroupAccessPolicy.__table__.columns


def test_private_conversation_does_not_read_group_policy(
    session_factory_fixture: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)

        def fail_group_lookup(**_: str) -> GroupAccessPolicy | None:
            raise AssertionError("private conversations must not query group policies")

        monkeypatch.setattr(service._store, "get_group_policy", fail_group_lookup)

        facts = service.resolve_conversation_facts(
            conversation_type=ConversationType.PRIVATE,
            source="wechat",
            source_account_id="bot-001",
            conversation_id="private-001",
            is_mentioned=True,
            at=AT,
        )

        assert facts.conversation_type is ConversationType.PRIVATE
        assert facts.group_allowed is None
        assert facts.is_mentioned is None
        assert facts.group_permission_scope == frozenset()


def test_source_identity_resolution_combines_mapping_and_user_policy(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        identity_id = create_identity(session)
        IdentityService(session).create_mapping(
            platform="wechat",
            account_id="bot-001",
            sender_id="sender-001",
            enterprise_identity_id=identity_id,
        )
        service = AccessPolicyService(session)
        service.upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"messages:read"},
        )

        facts = service.resolve_source_identity_facts(
            source="wechat",
            source_account_id="bot-001",
            sender_id="sender-001",
            at=AT,
        )

        assert facts.identity_resolved is True
        assert facts.enterprise_identity_id == identity_id
        assert facts.identity_status is IdentityStatus.ACTIVE
        assert facts.user_allowed is True


def test_missing_or_disabled_gateway_policy_fails_closed(
    session_factory_fixture: sessionmaker[Session],
) -> None:
    with session_factory_fixture() as session:
        service = AccessPolicyService(session)
        assert service.resolve_gateway_policy_facts().allowed_risk_levels == frozenset()

        service.upsert_gateway_policy(
            enabled=False,
            permission_scope={"messages:read"},
            allowed_skills={"search"},
            allowed_risk_levels={RiskLevel.NORMAL},
        )
        facts = service.resolve_gateway_policy_facts()

        assert facts.system_permission_scope == frozenset()
        assert facts.system_allowed_skills == frozenset()
        assert facts.allowed_risk_levels == frozenset()
