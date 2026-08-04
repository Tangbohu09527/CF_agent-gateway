from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, fields

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.access import (
    AccessPolicyService,
    ConversationType,
    ReasonCode,
    RiskLevel,
)
from cf_agent_gateway.admission import (
    AdmissionCandidate,
    AdmissionOrchestrator,
    AdmissionOutcome,
    AdmissionReason,
    SenderType,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
)
from cf_agent_gateway.identity.models import (
    IdentityStatus as StoredIdentityStatus,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService

SOURCE = "wechat"
SOURCE_ACCOUNT_ID = "bot-001"
PRIVATE_CONVERSATION_ID = "private-001"
GROUP_CONVERSATION_ID = "group-001"
SENDER_ID = "wxid-001"
DEFAULT_SCOPE = frozenset({"messages:read", "messages:write"})
DEFAULT_SKILLS = frozenset({"search", "summarize"})


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


def candidate(**overrides: object) -> AdmissionCandidate:
    values: dict[str, object] = {
        "message_id": 1,
        "source": SOURCE,
        "source_account_id": SOURCE_ACCOUNT_ID,
        "conversation_id": PRIVATE_CONVERSATION_ID,
        "conversation_type": ConversationType.PRIVATE,
        "sender_type": SenderType.HUMAN,
        "sender_id": SENDER_ID,
        "is_self": False,
        "is_mentioned": None,
        "message_type": "text",
        "requested_scope": frozenset(),
        "requested_skill_ids": frozenset(),
        "risk_level": RiskLevel.NORMAL,
    }
    values.update(overrides)
    return AdmissionCandidate(**values)  # type: ignore[arg-type]


def provision_sender(
    session: Session,
    *,
    sender_id: str = SENDER_ID,
    employee_id: str | None = None,
    identity_status: StoredIdentityStatus = StoredIdentityStatus.ACTIVE,
    create_user_policy: bool = True,
    user_enabled: bool = True,
    permission_scope: frozenset[str] = DEFAULT_SCOPE,
    allowed_skills: frozenset[str] = DEFAULT_SKILLS,
) -> EnterpriseIdentity:
    identity_service = IdentityService(session)
    identity = identity_service.create_identity(
        employee_id=employee_id or f"employee-{sender_id}",
        status=identity_status,
    )
    identity_service.create_mapping(
        platform=SOURCE,
        account_id=SOURCE_ACCOUNT_ID,
        sender_id=sender_id,
        enterprise_identity_id=identity.id,
    )
    if create_user_policy:
        AccessPolicyService(session).upsert_user_policy(
            enterprise_identity_id=identity.id,
            enabled=user_enabled,
            permission_scope=permission_scope,
            allowed_skills=allowed_skills,
        )
    return identity


def allow_gateway(
    session: Session,
    *,
    permission_scope: frozenset[str] = DEFAULT_SCOPE,
    allowed_skills: frozenset[str] = DEFAULT_SKILLS,
    allowed_risk_levels: frozenset[RiskLevel] = frozenset({RiskLevel.NORMAL}),
) -> None:
    AccessPolicyService(session).upsert_gateway_policy(
        permission_scope=permission_scope,
        allowed_skills=allowed_skills,
        allowed_risk_levels=allowed_risk_levels,
    )


def allow_group(
    session: Session,
    *,
    permission_scope: frozenset[str] = DEFAULT_SCOPE,
    allowed_skills: frozenset[str] = DEFAULT_SKILLS,
    enabled: bool = True,
) -> None:
    AccessPolicyService(session).upsert_group_policy(
        source=SOURCE,
        source_account_id=SOURCE_ACCOUNT_ID,
        conversation_id=GROUP_CONVERSATION_ID,
        enabled=enabled,
        permission_scope=permission_scope,
        allowed_skills=allowed_skills,
    )


def private_candidate(**overrides: object) -> AdmissionCandidate:
    return candidate(**overrides)


def group_candidate(**overrides: object) -> AdmissionCandidate:
    values: dict[str, object] = {
        "conversation_id": GROUP_CONVERSATION_ID,
        "conversation_type": ConversationType.GROUP,
        "is_mentioned": True,
    }
    values.update(overrides)
    return candidate(**values)


def assert_no_workspace_resources(session: Session) -> None:
    assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 0
    assert session.scalar(select(func.count()).select_from(AIThread)) == 0


def assert_access_denied(
    session: Session,
    outcome: AdmissionOutcome,
    reason_code: ReasonCode,
) -> None:
    assert outcome.admitted is False
    assert outcome.should_create_task is False
    assert outcome.reason is AdmissionReason.ACCESS_DENIED
    assert outcome.workspace_id is None
    assert outcome.ai_thread_id is None
    authorization = outcome.authorization
    assert authorization is not None
    assert authorization.allowed is False
    assert authorization.reason_code is reason_code
    assert_no_workspace_resources(session)


def test_allowlisted_private_message_creates_workspace_and_thread(session: Session) -> None:
    identity = provision_sender(session)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(private_candidate())

    assert outcome.admitted is True
    assert outcome.should_create_task is True
    assert outcome.reason is AdmissionReason.ALLOWED
    assert outcome.enterprise_identity_id == identity.id
    assert outcome.workspace_id is not None
    assert outcome.ai_thread_id is not None
    assert outcome.authorization is not None
    assert outcome.authorization.allowed is True
    assert outcome.authorization.reason_code is ReasonCode.ALLOWED
    assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 1
    assert session.scalar(select(func.count()).select_from(AIThread)) == 1


def test_private_message_does_not_require_mention(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(private_candidate(is_mentioned=False))

    assert outcome.admitted is True
    assert outcome.authorization is not None
    assert outcome.authorization.is_mentioned is None


def test_non_allowlisted_private_message_is_denied(session: Session) -> None:
    provision_sender(session, create_user_policy=False)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(private_candidate())

    assert_access_denied(session, outcome, ReasonCode.USER_NOT_ALLOWED)


def test_unmapped_identity_is_denied(session: Session) -> None:
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(private_candidate())

    assert_access_denied(session, outcome, ReasonCode.IDENTITY_UNRESOLVED)
    assert outcome.enterprise_identity_id is None


def test_disabled_identity_is_denied_even_with_user_policy(session: Session) -> None:
    identity = provision_sender(session, identity_status=StoredIdentityStatus.DISABLED)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(private_candidate())

    assert_access_denied(session, outcome, ReasonCode.IDENTITY_DISABLED)
    assert outcome.authorization is not None
    assert outcome.authorization.enterprise_identity_id == identity.id


def test_self_message_short_circuits_before_identity_resolution(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_identity_resolution(*_: object, **__: object) -> None:
        pytest.fail("self messages must not resolve identity")

    monkeypatch.setattr(
        AccessPolicyService,
        "resolve_source_identity_facts",
        fail_identity_resolution,
    )

    outcome = AdmissionOrchestrator(session).admit(
        private_candidate(is_self=True, sender_type=SenderType.SYSTEM, message_type="system")
    )

    assert outcome.admitted is False
    assert outcome.should_create_task is False
    assert outcome.reason is AdmissionReason.SELF_MESSAGE
    assert outcome.enterprise_identity_id is None
    assert outcome.workspace_id is None
    assert outcome.ai_thread_id is None
    assert outcome.authorization is None
    assert_no_workspace_resources(session)


@pytest.mark.parametrize(
    ("sender_type", "message_type"),
    [
        (SenderType.SYSTEM, "text"),
        (SenderType.HUMAN, "system"),
    ],
)
def test_system_message_short_circuits_before_employee_mapping(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    sender_type: SenderType,
    message_type: str,
) -> None:
    def fail_identity_resolution(*_: object, **__: object) -> None:
        pytest.fail("system messages must not resolve an employee identity")

    monkeypatch.setattr(
        AccessPolicyService,
        "resolve_source_identity_facts",
        fail_identity_resolution,
    )

    outcome = AdmissionOrchestrator(session).admit(
        private_candidate(sender_type=sender_type, message_type=message_type)
    )

    assert outcome.admitted is False
    assert outcome.should_create_task is False
    assert outcome.reason is AdmissionReason.SYSTEM_MESSAGE
    assert outcome.enterprise_identity_id is None
    assert outcome.workspace_id is None
    assert outcome.ai_thread_id is None
    assert outcome.authorization is None
    assert_no_workspace_resources(session)


def test_human_message_without_sender_id_is_rejected_before_identity_resolution(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_identity_resolution(*_: object, **__: object) -> None:
        pytest.fail("a missing sender id must not reach identity resolution")

    monkeypatch.setattr(
        AccessPolicyService,
        "resolve_source_identity_facts",
        fail_identity_resolution,
    )

    outcome = AdmissionOrchestrator(session).admit(private_candidate(sender_id=None))

    assert outcome.admitted is False
    assert outcome.should_create_task is False
    assert outcome.reason is AdmissionReason.SENDER_UNRESOLVED
    assert outcome.enterprise_identity_id is None
    assert outcome.workspace_id is None
    assert outcome.ai_thread_id is None
    assert outcome.authorization is None
    assert_no_workspace_resources(session)


def test_enabled_allowlisted_mentioned_group_is_admitted(session: Session) -> None:
    identity = provision_sender(session)
    allow_group(session)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(group_candidate())

    assert outcome.admitted is True
    assert outcome.should_create_task is True
    assert outcome.enterprise_identity_id == identity.id
    assert outcome.workspace_id is not None
    assert outcome.ai_thread_id is not None
    assert outcome.authorization is not None
    assert outcome.authorization.group_allowed is True
    assert outcome.authorization.is_mentioned is True


@pytest.mark.parametrize("create_disabled_policy", [False, True])
def test_group_without_enabled_policy_is_denied(
    session: Session, create_disabled_policy: bool
) -> None:
    provision_sender(session)
    if create_disabled_policy:
        allow_group(session, enabled=False)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(group_candidate())

    assert_access_denied(session, outcome, ReasonCode.GROUP_NOT_ALLOWED)


def test_group_member_without_user_allowlist_is_denied(session: Session) -> None:
    provision_sender(session, create_user_policy=False)
    allow_group(session)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(group_candidate())

    assert_access_denied(session, outcome, ReasonCode.USER_NOT_ALLOWED)


@pytest.mark.parametrize("is_mentioned", [False, None])
def test_group_requires_an_explicit_structured_mention(
    session: Session, is_mentioned: bool | None
) -> None:
    provision_sender(session)
    allow_group(session)
    allow_gateway(session)

    outcome = AdmissionOrchestrator(session).admit(group_candidate(is_mentioned=is_mentioned))

    assert_access_denied(session, outcome, ReasonCode.BOT_NOT_MENTIONED)


def test_allowed_authorization_is_user_group_gateway_intersection(
    session: Session,
) -> None:
    common_scope = "messages:common"
    common_skill = "common-skill"
    provision_sender(
        session,
        permission_scope=frozenset({common_scope, "user-only", "user-group"}),
        allowed_skills=frozenset({common_skill, "user-only", "user-group"}),
    )
    allow_group(
        session,
        permission_scope=frozenset({common_scope, "group-only", "user-group"}),
        allowed_skills=frozenset({common_skill, "group-only", "user-group"}),
    )
    allow_gateway(
        session,
        permission_scope=frozenset({common_scope, "gateway-only"}),
        allowed_skills=frozenset({common_skill, "gateway-only"}),
    )

    outcome = AdmissionOrchestrator(session).admit(group_candidate())

    assert outcome.authorization is not None
    assert outcome.authorization.allowed is True
    assert outcome.authorization.permission_scope == frozenset({common_scope})
    assert outcome.authorization.allowed_skills == frozenset({common_skill})


def test_disallowed_requested_skill_is_denied(session: Session) -> None:
    provision_sender(session, allowed_skills=frozenset({"search"}))
    allow_gateway(session, allowed_skills=frozenset({"search"}))

    outcome = AdmissionOrchestrator(session).admit(private_candidate(requested_skill_ids={"admin"}))

    assert_access_denied(session, outcome, ReasonCode.SKILL_NOT_ALLOWED)


def test_disallowed_risk_level_is_denied(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session, allowed_risk_levels=frozenset({RiskLevel.NORMAL}))

    outcome = AdmissionOrchestrator(session).admit(private_candidate(risk_level=RiskLevel.HIGH))

    assert_access_denied(session, outcome, ReasonCode.RISK_NOT_ALLOWED)


def test_workspace_service_is_never_called_for_a_denied_request(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provision_sender(session, create_user_policy=False)
    allow_gateway(session)

    def fail_workspace_construction(*_: object, **__: object) -> None:
        pytest.fail("WorkspaceService must only run after an allowed decision")

    monkeypatch.setattr(
        "cf_agent_gateway.admission.service.WorkspaceService",
        fail_workspace_construction,
    )

    outcome = AdmissionOrchestrator(session).admit(private_candidate())

    assert_access_denied(session, outcome, ReasonCode.USER_NOT_ALLOWED)


def test_existing_workspace_does_not_bypass_access_control(session: Session) -> None:
    identity = provision_sender(session)
    allow_gateway(session)
    existing_thread = WorkspaceService(session).ensure_thread_for_authorized_request(
        enterprise_identity_id=identity.id,
        platform=SOURCE,
        account_id=SOURCE_ACCOUNT_ID,
        physical_conversation_id=PRIVATE_CONVERSATION_ID,
        conversation_type=ConversationType.PRIVATE,
        sender_id=SENDER_ID,
    )
    AccessPolicyService(session).upsert_user_policy(
        enterprise_identity_id=identity.id,
        enabled=False,
    )

    outcome = AdmissionOrchestrator(session).admit(private_candidate())

    assert outcome.admitted is False
    assert outcome.authorization is not None
    assert outcome.authorization.reason_code is ReasonCode.USER_NOT_ALLOWED
    assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 1
    assert session.scalar(select(func.count()).select_from(AIThread)) == 1
    assert session.get(AIThread, existing_thread.id) is not None


def test_repeated_allowed_request_returns_stable_workspace_and_thread(
    session: Session,
) -> None:
    provision_sender(session)
    allow_gateway(session)
    orchestrator = AdmissionOrchestrator(session)
    request = private_candidate()

    first = orchestrator.admit(request)
    second = orchestrator.admit(request)

    assert first.admitted is True
    assert second.admitted is True
    assert second.enterprise_identity_id == first.enterprise_identity_id
    assert second.workspace_id == first.workspace_id
    assert second.ai_thread_id == first.ai_thread_id
    assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 1
    assert session.scalar(select(func.count()).select_from(AIThread)) == 1


def test_different_employees_in_same_group_reuse_thread(session: Session) -> None:
    first_identity = provision_sender(
        session,
        sender_id="wxid-001",
        employee_id="employee-001",
    )
    second_identity = provision_sender(
        session,
        sender_id="wxid-002",
        employee_id="employee-002",
    )
    allow_group(session)
    allow_gateway(session)
    orchestrator = AdmissionOrchestrator(session)

    first = orchestrator.admit(group_candidate(message_id=1, sender_id="wxid-001"))
    second = orchestrator.admit(group_candidate(message_id=2, sender_id="wxid-002"))

    assert first.enterprise_identity_id == first_identity.id
    assert second.enterprise_identity_id == second_identity.id
    assert first.workspace_id != second.workspace_id
    assert first.ai_thread_id == second.ai_thread_id
    assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 2
    assert session.scalar(select(func.count()).select_from(AIThread)) == 1


def test_candidate_and_outcome_are_immutable_and_exclude_message_content(
    session: Session,
) -> None:
    admission_candidate = private_candidate()
    candidate_fields = {field.name for field in fields(AdmissionCandidate)}

    assert candidate_fields.isdisjoint({"display_name", "content", "message_content", "nickname"})
    with pytest.raises(FrozenInstanceError):
        admission_candidate.message_id = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        candidate(message_content="@bot")

    outcome = AdmissionOrchestrator(session).admit(private_candidate(is_self=True))
    with pytest.raises(FrozenInstanceError):
        outcome.admitted = True  # type: ignore[misc]
