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
        "is_mentioned": None,
    }
    values.update(overrides)
    return ConversationFacts(**values)  # type: ignore[arg-type]


def group_conversation(**overrides: object) -> ConversationFacts:
    values = {
        "conversation_type": ConversationType.GROUP,
        "is_mentioned": True,
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


@pytest.mark.parametrize("is_mentioned", [False, True])
def test_private_conversation_rejects_mention_fact(is_mentioned: bool) -> None:
    decision = evaluate(conversation=private_conversation(is_mentioned=is_mentioned))

    assert decision.reason_code is ReasonCode.INVALID_CONVERSATION_FACTS


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


def test_mentioned_group_allows_authorized_identity() -> None:
    decision = evaluate(conversation=group_conversation())

    assert decision.allowed is True


def test_same_group_authorizes_each_sender_identity_independently() -> None:
    conversation = group_conversation()
    authorized = evaluate(
        identity=identity_facts(enterprise_identity_id="employee-a"),
        conversation=conversation,
    )
    unauthorized = evaluate(
        identity=identity_facts(
            enterprise_identity_id="customer-b",
            user_allowed=False,
            user_permission_scope=frozenset(),
            user_allowed_skills=frozenset(),
        ),
        conversation=conversation,
    )

    assert authorized.allowed is True
    assert authorized.enterprise_identity_id == "employee-a"
    assert unauthorized.allowed is False
    assert unauthorized.enterprise_identity_id == "customer-b"
    assert unauthorized.reason_code is ReasonCode.USER_NOT_ALLOWED


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
    assert {field.name for field in fields(ConversationFacts)} == {
        "conversation_type",
        "is_mentioned",
    }


def test_reply_quote_and_nickname_cannot_be_authorization_inputs() -> None:
    with pytest.raises(TypeError):
        ConversationFacts(  # type: ignore[call-arg]
            conversation_type=ConversationType.GROUP,
            is_mentioned=False,
            reply_to_message_id="message-1",
        )


@pytest.mark.parametrize("conversation", [private_conversation(), group_conversation()])
def test_user_and_gateway_permissions_are_intersected(
    conversation: ConversationFacts,
) -> None:
    decision = evaluate(
        identity=identity_facts(
            user_permission_scope=frozenset({"messages:read", "messages:write", "user-only"}),
            user_allowed_skills=frozenset({"search", "summarize", "user-only"}),
        ),
        conversation=conversation,
        policy=policy_facts(
            system_permission_scope=frozenset({"messages:write", "admin"}),
            system_allowed_skills=frozenset({"summarize", "admin"}),
        ),
    )

    assert decision.permission_scope == frozenset({"messages:write"})
    assert decision.allowed_skills == frozenset({"summarize"})


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
        evaluate(conversation=group_conversation(is_mentioned=False)),
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
        conversation=ConversationFacts(conversation_type="channel"),
    )

    assert decision.reason_code is ReasonCode.INVALID_CONVERSATION_TYPE


def test_denial_precedence_is_stable() -> None:
    decision = evaluate(
        identity=identity_facts(user_allowed=False),
        conversation=group_conversation(is_mentioned=False),
        request=request_facts(risk_level=RiskLevel.CRITICAL),
    )

    assert decision.reason_code is ReasonCode.USER_NOT_ALLOWED


def test_serialized_sets_are_sorted() -> None:
    decision = evaluate()

    assert decision.to_dict()["permission_scope"] == ["messages:read", "messages:write"]
    assert decision.to_dict()["allowed_skills"] == ["search", "summarize"]
