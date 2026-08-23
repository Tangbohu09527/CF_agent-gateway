from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from cf_agent_gateway.access import (
    AccessPolicyService,
    AccessPolicyStore,
    GatewayAccessPolicy,
    GatewayPolicyFacts,
    IdentityFacts,
    RequestFacts,
    UserAccessPolicy,
    evaluate_access,
)
from cf_agent_gateway.admission.enums import AdmissionReason, SenderType
from cf_agent_gateway.admission.errors import AdmissionInvariantError
from cf_agent_gateway.admission.models import AdmissionCandidate, AdmissionOutcome
from cf_agent_gateway.routing import RouteResolver
from cf_agent_gateway.workspace import ThreadResolver, WorkspaceService
from cf_agent_gateway.workspace.store import WorkspaceStore

SYSTEM_MESSAGE_TYPE = "system"


class AdmissionOrchestrator:
    """Authorize the sender before binding the conversation as thread context."""

    def __init__(self, session: Session, *, v2_routing_enabled: bool = False) -> None:
        if not isinstance(v2_routing_enabled, bool):
            raise ValueError("v2_routing_enabled must be a boolean")
        self._session = session
        self._v2_routing_enabled = v2_routing_enabled

    def admit(self, candidate: AdmissionCandidate) -> AdmissionOutcome:
        if candidate.is_self:
            return self._preprocessing_denial(candidate, AdmissionReason.SELF_MESSAGE)

        if (
            candidate.sender_type is SenderType.SYSTEM
            or candidate.message_type == SYSTEM_MESSAGE_TYPE
        ):
            return self._preprocessing_denial(candidate, AdmissionReason.SYSTEM_MESSAGE)

        if candidate.sender_id is None or not candidate.sender_id.strip():
            return self._preprocessing_denial(candidate, AdmissionReason.SENDER_UNRESOLVED)

        access_policy_service = AccessPolicyService(self._session)
        identity = access_policy_service.resolve_source_identity_facts(
            source=candidate.source,
            source_account_id=candidate.source_account_id,
            sender_id=candidate.sender_id,
        )
        conversation = access_policy_service.resolve_conversation_facts(
            conversation_type=candidate.conversation_type,
            is_mentioned=candidate.is_mentioned,
        )
        gateway_policy = access_policy_service.resolve_gateway_policy_facts()
        policy_store = AccessPolicyStore(self._session)
        gateway_policy_record = policy_store.get_gateway_policy()
        user_policy_record = (
            policy_store.get_user_policy(identity.enterprise_identity_id)
            if identity.enterprise_identity_id is not None
            else None
        )
        policy_references = {
            "policy_snapshot": _policy_snapshot(
                identity_facts=identity,
                gateway_facts=gateway_policy,
                gateway_policy=gateway_policy_record,
                user_policy=user_policy_record,
            ),
            "gateway_policy_id": (
                gateway_policy_record.id if gateway_policy_record is not None else None
            ),
            "gateway_policy_updated_at": (
                gateway_policy_record.updated_at if gateway_policy_record is not None else None
            ),
            "user_policy_id": user_policy_record.id if user_policy_record is not None else None,
            "user_policy_updated_at": (
                user_policy_record.updated_at if user_policy_record is not None else None
            ),
        }
        request = RequestFacts(
            requested_scope=candidate.requested_scope,
            requested_skill_ids=candidate.requested_skill_ids,
            risk_level=candidate.risk_level,
        )
        authorization = evaluate_access(identity, conversation, request, gateway_policy)

        if not authorization.allowed:
            return AdmissionOutcome(
                message_id=candidate.message_id,
                admitted=False,
                should_create_task=False,
                reason=AdmissionReason.ACCESS_DENIED,
                enterprise_identity_id=authorization.enterprise_identity_id,
                authorization=authorization,
                **policy_references,
            )

        enterprise_identity_id = authorization.enterprise_identity_id
        if enterprise_identity_id is None:
            raise AdmissionInvariantError(
                "allowed authorization decision has no enterprise identity"
            )

        workspace_service = WorkspaceService(self._session)
        if self._v2_routing_enabled:
            route = RouteResolver(self._session).resolve(
                source=candidate.source,
                source_account_id=candidate.source_account_id,
                conversation_id=candidate.conversation_id,
                conversation_type=candidate.conversation_type.value,
                enterprise_identity_id=enterprise_identity_id,
            )
            thread = ThreadResolver(self._session).resolve(
                conversation=route,
                source_account=route,
                sender_identity={"identity_id": enterprise_identity_id},
                agent_profile={
                    "profile_id": route.agent_profile_id,
                    "revision": route.agent_profile_revision,
                },
                thread_policy=route.thread_policy,
            )
            thread = WorkspaceStore(self._session).bind_v2_route_snapshot(
                thread,
                agent_profile_id=route.agent_profile_id,
                thread_policy=route.thread_policy,
            )
            workspace = workspace_service.ensure_workspace_for_authorized_identity(
                enterprise_identity_id
            )
            routing_references = {
                "routing_mode": "v2",
                "route_conversation_record_id": route.conversation_record_id,
                "agent_profile_id": route.agent_profile_id,
                "agent_profile_key": route.agent_profile_key,
                "agent_profile_reference": route.agent_profile_reference,
                "agent_profile_revision": route.agent_profile_revision,
                "group_type_id": route.group_type_id,
                "group_type_key": route.group_type_key,
                "thread_policy": route.thread_policy.value,
            }
        else:
            workspace = workspace_service.ensure_workspace_for_authorized_identity(
                enterprise_identity_id
            )
            thread = workspace_service.ensure_thread_for_authorized_request(
                enterprise_identity_id=enterprise_identity_id,
                platform=candidate.source,
                account_id=candidate.source_account_id,
                physical_conversation_id=candidate.conversation_id,
                conversation_type=candidate.conversation_type.value,
                sender_id=candidate.sender_id,
            )
            routing_references = {"routing_mode": "v1"}
        return AdmissionOutcome(
            message_id=candidate.message_id,
            admitted=True,
            should_create_task=True,
            reason=AdmissionReason.ALLOWED,
            enterprise_identity_id=enterprise_identity_id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            authorization=authorization,
            **policy_references,
            **routing_references,
        )

    @staticmethod
    def _preprocessing_denial(
        candidate: AdmissionCandidate, reason: AdmissionReason
    ) -> AdmissionOutcome:
        return AdmissionOutcome(
            message_id=candidate.message_id,
            admitted=False,
            should_create_task=False,
            reason=reason,
        )


def _policy_snapshot(
    *,
    identity_facts: IdentityFacts,
    gateway_facts: GatewayPolicyFacts,
    gateway_policy: GatewayAccessPolicy | None,
    user_policy: UserAccessPolicy | None,
) -> dict[str, object]:
    return {
        "evaluated_identity": {
            "identity_resolved": identity_facts.identity_resolved,
            "enterprise_identity_id": identity_facts.enterprise_identity_id,
            "identity_status": identity_facts.identity_status.value,
            "user_allowed": identity_facts.user_allowed,
            "permission_scope": sorted(identity_facts.user_permission_scope),
            "allowed_skills": sorted(identity_facts.user_allowed_skills),
        },
        "evaluated_gateway": {
            "permission_scope": sorted(gateway_facts.system_permission_scope),
            "allowed_skills": sorted(gateway_facts.system_allowed_skills),
            "allowed_risk_levels": sorted(
                level.value for level in gateway_facts.allowed_risk_levels
            ),
        },
        "gateway": (
            {
                "id": gateway_policy.id,
                "policy_key": gateway_policy.policy_key,
                "enabled": gateway_policy.enabled,
                "permission_scope": sorted(gateway_policy.permission_scope),
                "allowed_skills": sorted(gateway_policy.allowed_skills),
                "allowed_risk_levels": sorted(gateway_policy.allowed_risk_levels),
                "updated_at": _datetime_snapshot(gateway_policy.updated_at),
            }
            if gateway_policy is not None
            else None
        ),
        "user": (
            {
                "id": user_policy.id,
                "enterprise_identity_id": user_policy.enterprise_identity_id,
                "enabled": user_policy.enabled,
                "permission_scope": sorted(user_policy.permission_scope),
                "allowed_skills": sorted(user_policy.allowed_skills),
                "valid_from": _datetime_snapshot(user_policy.valid_from),
                "valid_until": _datetime_snapshot(user_policy.valid_until),
                "updated_at": _datetime_snapshot(user_policy.updated_at),
            }
            if user_policy is not None
            else None
        ),
    }


def _datetime_snapshot(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
