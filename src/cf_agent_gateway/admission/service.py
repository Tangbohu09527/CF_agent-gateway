from __future__ import annotations

from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RequestFacts, evaluate_access
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
                authorization=authorization,
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
        return AdmissionOutcome(
            message_id=candidate.message_id,
            admitted=True,
            should_create_task=True,
            reason=AdmissionReason.ALLOWED,
            enterprise_identity_id=enterprise_identity_id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            authorization=authorization,
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
