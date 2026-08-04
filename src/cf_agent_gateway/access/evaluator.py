from __future__ import annotations

from cf_agent_gateway.access.enums import (
    ConversationType,
    Decision,
    IdentityStatus,
    ReasonCode,
)
from cf_agent_gateway.access.models import (
    AuthorizationDecision,
    ConversationFacts,
    GatewayPolicyFacts,
    IdentityFacts,
    RequestFacts,
)


def evaluate_access(
    identity: IdentityFacts,
    conversation: ConversationFacts,
    request: RequestFacts,
    policy: GatewayPolicyFacts,
) -> AuthorizationDecision:
    """Authorize the sender; the conversation only contributes mention state."""
    conversation_type = conversation.conversation_type
    if conversation_type not in (ConversationType.PRIVATE, ConversationType.GROUP):
        return _denied(ReasonCode.INVALID_CONVERSATION_TYPE, identity, conversation, request)

    is_group = conversation_type == ConversationType.GROUP
    if not is_group and conversation.is_mentioned is not None:
        return _denied(ReasonCode.INVALID_CONVERSATION_FACTS, identity, conversation, request)

    if (
        not identity.identity_resolved
        or not identity.enterprise_identity_id
        or identity.identity_status == IdentityStatus.UNRESOLVED
    ):
        return _denied(ReasonCode.IDENTITY_UNRESOLVED, identity, conversation, request)

    if identity.identity_status in (IdentityStatus.DISABLED, IdentityStatus.ARCHIVED):
        return _denied(ReasonCode.IDENTITY_DISABLED, identity, conversation, request)

    if not identity.user_allowed:
        return _denied(ReasonCode.USER_NOT_ALLOWED, identity, conversation, request)

    if is_group and conversation.is_mentioned is not True:
        return _denied(ReasonCode.BOT_NOT_MENTIONED, identity, conversation, request)

    if request.risk_level not in policy.allowed_risk_levels:
        return _denied(ReasonCode.RISK_NOT_ALLOWED, identity, conversation, request)

    permission_scope = identity.user_permission_scope & policy.system_permission_scope
    allowed_skills = identity.user_allowed_skills & policy.system_allowed_skills

    if request.requested_scope:
        permission_scope &= request.requested_scope
        if not permission_scope:
            return _denied(ReasonCode.PERMISSION_SCOPE_EMPTY, identity, conversation, request)

    if request.requested_skill_ids:
        allowed_skills &= request.requested_skill_ids
        if not allowed_skills:
            return _denied(ReasonCode.SKILL_NOT_ALLOWED, identity, conversation, request)

    return AuthorizationDecision(
        allowed=True,
        decision=Decision.ALLOWED,
        reason_code=ReasonCode.ALLOWED,
        enterprise_identity_id=identity.enterprise_identity_id,
        user_allowed=identity.user_allowed,
        is_mentioned=conversation.is_mentioned,
        permission_scope=permission_scope,
        allowed_skills=allowed_skills,
        risk_level=request.risk_level,
    )


def _denied(
    reason_code: ReasonCode,
    identity: IdentityFacts,
    conversation: ConversationFacts,
    request: RequestFacts,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False,
        decision=Decision.DENIED,
        reason_code=reason_code,
        enterprise_identity_id=identity.enterprise_identity_id,
        user_allowed=identity.user_allowed,
        is_mentioned=conversation.is_mentioned,
        permission_scope=frozenset(),
        allowed_skills=frozenset(),
        risk_level=request.risk_level,
    )
