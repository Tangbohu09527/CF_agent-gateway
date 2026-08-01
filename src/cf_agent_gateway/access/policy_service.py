from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from cf_agent_gateway.access.enums import (
    ConversationType,
    IdentityStatus,
    RiskLevel,
)
from cf_agent_gateway.access.models import (
    ConversationFacts,
    GatewayPolicyFacts,
    IdentityFacts,
)
from cf_agent_gateway.access.policy_errors import GroupPolicyKeyRequiredError
from cf_agent_gateway.access.policy_models import (
    DEFAULT_GATEWAY_POLICY_KEY,
    GatewayAccessPolicy,
    GroupAccessPolicy,
    UserAccessPolicy,
)
from cf_agent_gateway.access.policy_schemas import (
    GatewayAccessPolicyUpsert,
    GroupAccessPolicyUpsert,
    UserAccessPolicyUpsert,
)
from cf_agent_gateway.access.policy_store import AccessPolicyStore
from cf_agent_gateway.identity.errors import IdentityNotFoundError
from cf_agent_gateway.identity.models import IdentityStatus as StoredIdentityStatus
from cf_agent_gateway.identity.schemas import IdentityResolution, IdentityResolutionStatus
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.identity.store import IdentityStore


class AccessPolicyService:
    """Manage authoritative policies and resolve immutable evaluator facts."""

    def __init__(self, session: Session) -> None:
        self._store = AccessPolicyStore(session)
        self._identity_service = IdentityService(session)
        self._identity_store = IdentityStore(session)

    def upsert_user_policy(
        self,
        *,
        enterprise_identity_id: str,
        enabled: bool = True,
        permission_scope: Iterable[str] = (),
        allowed_skills: Iterable[str] = (),
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> UserAccessPolicy:
        data = UserAccessPolicyUpsert(
            enterprise_identity_id=enterprise_identity_id,
            enabled=enabled,
            permission_scope=permission_scope,
            allowed_skills=allowed_skills,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        if self._identity_store.get_identity(data.enterprise_identity_id) is None:
            raise IdentityNotFoundError(data.enterprise_identity_id)
        policy, _ = self._store.upsert_user_policy(**data.model_dump())
        return policy

    set_user_policy = upsert_user_policy

    def upsert_group_policy(
        self,
        *,
        source: str,
        source_account_id: str,
        conversation_id: str,
        enabled: bool = True,
        permission_scope: Iterable[str] = (),
        allowed_skills: Iterable[str] = (),
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> GroupAccessPolicy:
        data = GroupAccessPolicyUpsert(
            source=source,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            enabled=enabled,
            permission_scope=permission_scope,
            allowed_skills=allowed_skills,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        policy, _ = self._store.upsert_group_policy(**data.model_dump())
        return policy

    set_group_policy = upsert_group_policy

    def upsert_gateway_policy(
        self,
        *,
        policy_key: str = DEFAULT_GATEWAY_POLICY_KEY,
        enabled: bool = True,
        permission_scope: Iterable[str] = (),
        allowed_skills: Iterable[str] = (),
        allowed_risk_levels: Iterable[RiskLevel | str] = (),
    ) -> GatewayAccessPolicy:
        data = GatewayAccessPolicyUpsert(
            policy_key=policy_key,
            enabled=enabled,
            permission_scope=permission_scope,
            allowed_skills=allowed_skills,
            allowed_risk_levels=allowed_risk_levels,
        )
        policy, _ = self._store.upsert_gateway_policy(**data.model_dump())
        return policy

    set_gateway_policy = upsert_gateway_policy

    def resolve_identity_facts(
        self,
        enterprise_identity_id: str | None = None,
        *,
        at: datetime | None = None,
    ) -> IdentityFacts:
        if enterprise_identity_id is None:
            return _empty_identity_facts(
                enterprise_identity_id=None,
                identity_resolved=False,
                identity_status=IdentityStatus.UNRESOLVED,
            )
        identity = self._identity_store.get_identity(enterprise_identity_id)
        if identity is None:
            return _empty_identity_facts(
                enterprise_identity_id=enterprise_identity_id,
                identity_resolved=False,
                identity_status=IdentityStatus.UNRESOLVED,
            )
        status = IdentityStatus(identity.status.value)
        if status is not IdentityStatus.ACTIVE:
            return _empty_identity_facts(
                enterprise_identity_id=enterprise_identity_id,
                identity_resolved=True,
                identity_status=status,
            )

        policy = self._store.get_user_policy(enterprise_identity_id)
        if policy is None or not _is_active_policy(
            enabled=policy.enabled,
            valid_from=policy.valid_from,
            valid_until=policy.valid_until,
            at=at,
        ):
            return _empty_identity_facts(
                enterprise_identity_id=enterprise_identity_id,
                identity_resolved=True,
                identity_status=IdentityStatus.ACTIVE,
            )
        return IdentityFacts(
            identity_resolved=True,
            enterprise_identity_id=enterprise_identity_id,
            identity_status=IdentityStatus.ACTIVE,
            user_allowed=True,
            user_permission_scope=policy.permission_scope,
            user_allowed_skills=policy.allowed_skills,
        )

    def resolve_identity_resolution_facts(
        self,
        resolution: IdentityResolution,
        *,
        at: datetime | None = None,
    ) -> IdentityFacts:
        if resolution.status is IdentityResolutionStatus.RESOLVED:
            return self.resolve_identity_facts(resolution.enterprise_identity_id, at=at)
        if resolution.status is IdentityResolutionStatus.DISABLED:
            identity = (
                self._identity_store.get_identity(resolution.enterprise_identity_id)
                if resolution.enterprise_identity_id is not None
                else None
            )
            status = (
                IdentityStatus.ARCHIVED
                if identity is not None and identity.status is StoredIdentityStatus.ARCHIVED
                else IdentityStatus.DISABLED
            )
            return _empty_identity_facts(
                enterprise_identity_id=resolution.enterprise_identity_id,
                identity_resolved=resolution.enterprise_identity_id is not None,
                identity_status=status,
            )
        return _empty_identity_facts(
            enterprise_identity_id=resolution.enterprise_identity_id,
            identity_resolved=False,
            identity_status=IdentityStatus.UNRESOLVED,
        )

    def resolve_source_identity_facts(
        self,
        *,
        source: str,
        source_account_id: str,
        sender_id: str,
        at: datetime | None = None,
    ) -> IdentityFacts:
        resolution = self._identity_service.resolve_identity(
            platform=source,
            account_id=source_account_id,
            sender_id=sender_id,
        )
        return self.resolve_identity_resolution_facts(resolution, at=at)

    def resolve_conversation_facts(
        self,
        *,
        conversation_type: ConversationType | str,
        source: str | None = None,
        source_account_id: str | None = None,
        conversation_id: str | None = None,
        is_mentioned: bool | None = None,
        at: datetime | None = None,
    ) -> ConversationFacts:
        normalized_type = ConversationType(conversation_type)
        if normalized_type is ConversationType.PRIVATE:
            return ConversationFacts(conversation_type=normalized_type)
        if source is None or source_account_id is None or conversation_id is None:
            raise GroupPolicyKeyRequiredError

        key = GroupAccessPolicyUpsert(
            source=source,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        policy = self._store.get_group_policy(
            source=key.source,
            source_account_id=key.source_account_id,
            conversation_id=key.conversation_id,
        )
        if policy is None or not _is_active_policy(
            enabled=policy.enabled,
            valid_from=policy.valid_from,
            valid_until=policy.valid_until,
            at=at,
        ):
            return ConversationFacts(
                conversation_type=normalized_type,
                group_allowed=False,
                is_mentioned=is_mentioned,
            )
        return ConversationFacts(
            conversation_type=normalized_type,
            group_allowed=True,
            is_mentioned=is_mentioned,
            group_permission_scope=policy.permission_scope,
            group_allowed_skills=policy.allowed_skills,
        )

    def resolve_gateway_policy_facts(
        self,
    ) -> GatewayPolicyFacts:
        policy = self._store.get_gateway_policy(DEFAULT_GATEWAY_POLICY_KEY)
        if policy is None or not policy.enabled:
            return GatewayPolicyFacts(
                system_permission_scope=frozenset(),
                system_allowed_skills=frozenset(),
                allowed_risk_levels=frozenset(),
            )
        return GatewayPolicyFacts(
            system_permission_scope=policy.permission_scope,
            system_allowed_skills=policy.allowed_skills,
            allowed_risk_levels=policy.allowed_risk_levels,
        )


def _empty_identity_facts(
    *,
    enterprise_identity_id: str | None,
    identity_resolved: bool,
    identity_status: IdentityStatus,
) -> IdentityFacts:
    return IdentityFacts(
        identity_resolved=identity_resolved,
        enterprise_identity_id=enterprise_identity_id,
        identity_status=identity_status,
        user_allowed=False,
    )


def _is_active_policy(
    *,
    enabled: bool,
    valid_from: datetime | None,
    valid_until: datetime | None,
    at: datetime | None,
) -> bool:
    if not enabled:
        return False
    resolved_at = _as_utc(at or datetime.now(UTC))
    return not (
        valid_from is not None
        and resolved_at < _as_utc(valid_from)
        or valid_until is not None
        and resolved_at > _as_utc(valid_until)
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
