from dataclasses import FrozenInstanceError, fields

import pytest

from cf_agent_gateway.access import (
    ConversationFacts,
    ConversationType,
    Decision,
    GatewayPolicyFacts,
    IdentityFacts,
    IdentityStatus,
    ReasonCode,
    RequestFacts,
    RiskLevel,
    evaluate_access,
)


def identity_facts(**overrides: object) -> IdentityFacts:
    values = {
        "identity_resolved": True,
        "enterprise_identity_id": "enterprise-user-1",
        "identity_status": IdentityStatus.ACTIVE,
        "user_allowed": True,
        "user_permission_scope": frozenset({"messages:read", "messages:write"}),
        "user_allowed_skills": frozenset({"search", "summarize"}),
    }
    values.update(overrides)
    return IdentityFacts(**values)  # type: ignore[arg-type]


def private_conversation(**overrides: object) -> ConversationFacts:
    values = {
        "conversation_type": ConversationType.PRIVATE,
        "group_allowed": None,
        "is_mentioned": None,
    }
    values.update(overrides)
    return ConversationFacts(**values)  # type: ignore[arg-type]


def group_conversation(**overrides: object) -> ConversationFacts:
    values = {
        "conversation_type": ConversationType.GROUP,
        "group_allowed": True,
        "is_mentioned": True,
        "group_permission_scope": frozenset({"messages:read", "messages:write"}),
        "group_allowed_skills": frozenset({"search", "summarize"}),
    }
    values.update(overrides)
    return ConversationFacts(**values)  # type: ignore[arg-type]


def request_facts(**overrides: object) -> RequestFacts:
    values = {
        "requested_scope": frozenset(),
        "requested_skill_ids": frozenset(),
        "risk_level": RiskLevel.NORMAL,
    }
    values.update(overrides)
    return RequestFacts(**values)  # type: ignore[arg-type]


def policy_facts(**overrides: object) -> GatewayPolicyFacts:
    values = {
        "system_permission_scope": frozenset({"messages:read", "messages:write"}),
        "system_allowed_skills": frozenset({"search", "summarize"}),
        "allowed_risk_levels": frozenset({RiskLevel.LOW, RiskLevel.NORMAL}),
    }
    values.update(overrides)
    return GatewayPolicyFacts(**values)  # type: ignore[arg-type]


def evaluate(
    *,
    identity: IdentityFacts | None = None,
    conversation: ConversationFacts | None = None,
    request: RequestFacts | None = None,
    policy: GatewayPolicyFacts | None = None,
):
    return evaluate_access(
        identity or identity_facts(),
        conversation or private_conversation(),
        request or request_facts(),
        policy or policy_facts(),
    )


def test_allowed_private_conversation_for_active_allowlisted_identity() -> None:
    decision = evaluate()

    assert decision.allowed is True
    assert decision.decision is Decision.ALLOWED
    assert decision.reason_code is ReasonCode.ALLOWED


def test_private_conversation_does_not_require_mention() -> None:
    decision = evaluate(conversation=private_conversation(is_mentioned=None))

    assert decision.allowed is True
    assert decision.is_mentioned is None


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"group_allowed": True}, ReasonCode.INVALID_CONVERSATION_FACTS),
        ({"is_mentioned": False}, ReasonCode.INVALID_CONVERSATION_FACTS),
        (
            {"group_permission_scope": frozenset({"messages:read"})},
            ReasonCode.INVALID_CONVERSATION_FACTS,
        ),
        (
            {"group_allowed_skills": frozenset({"search"})},
            ReasonCode.INVALID_CONVERSATION_FACTS,
        ),
    ],
)
def test_private_conversation_rejects_group_facts(
    overrides: dict[str, object], reason_code: ReasonCode
) -> None:
    decision = evaluate(conversation=private_conversation(**overrides))

    assert decision.reason_code is reason_code


def test_non_allowlisted_private_identity_is_denied() -> None:
    decision = evaluate(identity=identity_facts(user_allowed=False))

    assert decision.reason_code is ReasonCode.USER_NOT_ALLOWED


@pytest.mark.parametrize(
    "identity",
    [
        identity_facts(identity_resolved=False),
        identity_facts(enterprise_identity_id=None),
        identity_facts(identity_status=IdentityStatus.UNRESOLVED),
    ],
)
def test_unresolved_or_missing_identity_is_denied(identity: IdentityFacts) -> None:
    decision = evaluate(identity=identity)

    assert decision.reason_code is ReasonCode.IDENTITY_UNRESOLVED


@pytest.mark.parametrize("status", [IdentityStatus.DISABLED, IdentityStatus.ARCHIVED])
def test_inactive_identity_is_denied(status: IdentityStatus) -> None:
    decision = evaluate(identity=identity_facts(identity_status=status))

    assert decision.reason_code is ReasonCode.IDENTITY_DISABLED


def test_enabled_mentioned_group_allows_allowlisted_identity() -> None:
    decision = evaluate(conversation=group_conversation())

    assert decision.allowed is True


@pytest.mark.parametrize("group_allowed", [False, None])
def test_group_must_be_explicitly_allowed(group_allowed: bool | None) -> None:
    decision = evaluate(conversation=group_conversation(group_allowed=group_allowed))

    assert decision.reason_code is ReasonCode.GROUP_NOT_ALLOWED


def test_group_user_must_be_allowlisted() -> None:
    decision = evaluate(
        identity=identity_facts(user_allowed=False), conversation=group_conversation()
    )

    assert decision.reason_code is ReasonCode.USER_NOT_ALLOWED


@pytest.mark.parametrize("is_mentioned", [False, None])
def test_group_requires_explicit_mention(is_mentioned: bool | None) -> None:
    decision = evaluate(conversation=group_conversation(is_mentioned=is_mentioned))

    assert decision.reason_code is ReasonCode.BOT_NOT_MENTIONED


def test_authorization_models_exclude_message_content_and_identity_labels() -> None:
    fact_fields = {
        field.name
        for fact_type in (IdentityFacts, ConversationFacts, RequestFacts, GatewayPolicyFacts)
        for field in fields(fact_type)
    }

    assert fact_fields.isdisjoint(
        {
            "display_name",
            "message_content",
            "nickname",
            "raw_text",
            "reply_to_message_id",
            "quoted_message_id",
        }
    )


def test_reply_quote_and_nickname_cannot_be_authorization_inputs() -> None:
    with pytest.raises(TypeError):
        ConversationFacts(  # type: ignore[call-arg]
            conversation_type=ConversationType.GROUP,
            group_allowed=True,
            is_mentioned=False,
            reply_to_message_id="message-1",
        )


def test_group_permissions_only_narrow_user_permissions() -> None:
    decision = evaluate(
        identity=identity_facts(
            user_permission_scope=frozenset({"messages:read", "messages:write"})
        ),
        conversation=group_conversation(
            group_permission_scope=frozenset({"messages:read", "admin"})
        ),
    )

    assert decision.permission_scope == frozenset({"messages:read"})


def test_system_permissions_only_narrow_user_and_group_permissions() -> None:
    decision = evaluate(
        conversation=group_conversation(
            group_permission_scope=frozenset({"messages:read", "messages:write", "admin"})
        ),
        policy=policy_facts(system_permission_scope=frozenset({"messages:write", "admin"})),
    )

    assert decision.permission_scope == frozenset({"messages:write"})


def test_nonempty_requested_scope_with_empty_intersection_is_denied() -> None:
    decision = evaluate(request=request_facts(requested_scope=frozenset({"admin"})))

    assert decision.reason_code is ReasonCode.PERMISSION_SCOPE_EMPTY


def test_empty_requested_scope_does_not_deny_an_ordinary_request() -> None:
    decision = evaluate(
        identity=identity_facts(user_permission_scope=frozenset()),
        policy=policy_facts(system_permission_scope=frozenset()),
    )

    assert decision.allowed is True
    assert decision.permission_scope == frozenset()


def test_nonempty_requested_skills_with_empty_intersection_is_denied() -> None:
    decision = evaluate(request=request_facts(requested_skill_ids=frozenset({"admin"})))

    assert decision.reason_code is ReasonCode.SKILL_NOT_ALLOWED


def test_requested_skills_return_only_the_effective_intersection() -> None:
    decision = evaluate(request=request_facts(requested_skill_ids=frozenset({"search", "admin"})))

    assert decision.allowed_skills == frozenset({"search"})


def test_risk_level_must_be_allowed_by_system_policy() -> None:
    decision = evaluate(request=request_facts(risk_level=RiskLevel.HIGH))

    assert decision.reason_code is ReasonCode.RISK_NOT_ALLOWED


@pytest.mark.parametrize(
    "decision",
    [
        evaluate(identity=identity_facts(identity_resolved=False)),
        evaluate(conversation=group_conversation(group_allowed=False)),
        evaluate(request=request_facts(requested_scope=frozenset({"admin"}))),
    ],
)
def test_denied_decisions_never_expose_permissions(decision) -> None:
    assert decision.allowed is False
    assert decision.decision is Decision.DENIED
    assert decision.permission_scope == frozenset()
    assert decision.allowed_skills == frozenset()


def test_same_input_always_returns_the_same_decision() -> None:
    identity = identity_facts()
    conversation = group_conversation()
    request = request_facts(requested_scope=frozenset({"messages:read"}))
    policy = policy_facts()

    first = evaluate_access(identity, conversation, request, policy)
    second = evaluate_access(identity, conversation, request, policy)

    assert first == second
    assert first.to_dict() == second.to_dict()
    with pytest.raises(FrozenInstanceError):
        first.allowed = False  # type: ignore[misc]


def test_inputs_are_immutable_snapshots_and_not_modified() -> None:
    user_scope = {"messages:read", "messages:write"}
    requested_scope = {"messages:read"}
    identity = identity_facts(user_permission_scope=user_scope)
    request = request_facts(requested_scope=requested_scope)

    before = (set(user_scope), set(requested_scope))
    evaluate(identity=identity, request=request)

    assert (user_scope, requested_scope) == before
    assert identity.user_permission_scope == frozenset(user_scope)
    with pytest.raises(FrozenInstanceError):
        identity.user_allowed = False  # type: ignore[misc]


def test_invalid_conversation_type_has_highest_priority() -> None:
    decision = evaluate(
        identity=identity_facts(identity_resolved=False),
        conversation=ConversationFacts(conversation_type="channel", group_allowed=True),
    )

    assert decision.reason_code is ReasonCode.INVALID_CONVERSATION_TYPE


def test_denial_precedence_is_stable() -> None:
    decision = evaluate(
        identity=identity_facts(user_allowed=False),
        conversation=group_conversation(group_allowed=False, is_mentioned=False),
        request=request_facts(risk_level=RiskLevel.CRITICAL),
    )

    assert decision.reason_code is ReasonCode.USER_NOT_ALLOWED


def test_serialized_sets_are_sorted() -> None:
    decision = evaluate()

    assert decision.to_dict()["permission_scope"] == ["messages:read", "messages:write"]
    assert decision.to_dict()["allowed_skills"] == ["search", "summarize"]
