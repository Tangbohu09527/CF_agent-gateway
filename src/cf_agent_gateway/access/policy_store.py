from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.access.policy_errors import InvalidGatewayPolicyKeyError
from cf_agent_gateway.access.policy_models import (
    DEFAULT_GATEWAY_POLICY_KEY,
    GatewayAccessPolicy,
    GroupAccessPolicy,
    UserAccessPolicy,
)


def _string_set(values: Iterable[str | Enum]) -> frozenset[str]:
    return frozenset(
        str(value.value) if isinstance(value, Enum) else str(value) for value in values
    )


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class AccessPolicyStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_user_policy(
        self,
        *,
        enterprise_identity_id: str,
        enabled: bool = True,
        permission_scope: Iterable[str] = (),
        allowed_skills: Iterable[str] = (),
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> tuple[UserAccessPolicy, bool]:
        values = {
            "enabled": enabled,
            "permission_scope": _string_set(permission_scope),
            "allowed_skills": _string_set(allowed_skills),
            "valid_from": _utc_datetime(valid_from),
            "valid_until": _utc_datetime(valid_until),
        }
        existing = self.get_user_policy(enterprise_identity_id)
        if existing is not None:
            self._update(existing, values)
            return existing, False

        policy = UserAccessPolicy(
            id=str(uuid4()),
            enterprise_identity_id=enterprise_identity_id,
            **values,
        )
        self._session.add(policy)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_user_policy(enterprise_identity_id)
            if existing is None:
                raise
            self._update(existing, values)
            return existing, False
        return policy, True

    def get_user_policy(self, enterprise_identity_id: str) -> UserAccessPolicy | None:
        statement = select(UserAccessPolicy).where(
            UserAccessPolicy.enterprise_identity_id == enterprise_identity_id
        )
        return self._session.scalar(statement)

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
    ) -> tuple[GroupAccessPolicy, bool]:
        values = {
            "enabled": enabled,
            "permission_scope": _string_set(permission_scope),
            "allowed_skills": _string_set(allowed_skills),
            "valid_from": _utc_datetime(valid_from),
            "valid_until": _utc_datetime(valid_until),
        }
        existing = self.get_group_policy(
            source=source,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        if existing is not None:
            self._update(existing, values)
            return existing, False

        policy = GroupAccessPolicy(
            id=str(uuid4()),
            source=source,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            **values,
        )
        self._session.add(policy)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_group_policy(
                source=source,
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
            if existing is None:
                raise
            self._update(existing, values)
            return existing, False
        return policy, True

    def get_group_policy(
        self,
        *,
        source: str,
        source_account_id: str,
        conversation_id: str,
    ) -> GroupAccessPolicy | None:
        statement = select(GroupAccessPolicy).where(
            GroupAccessPolicy.source == source,
            GroupAccessPolicy.source_account_id == source_account_id,
            GroupAccessPolicy.conversation_id == conversation_id,
        )
        return self._session.scalar(statement)

    def upsert_gateway_policy(
        self,
        *,
        policy_key: str = DEFAULT_GATEWAY_POLICY_KEY,
        enabled: bool = True,
        permission_scope: Iterable[str] = (),
        allowed_skills: Iterable[str] = (),
        allowed_risk_levels: Iterable[str | Enum] = (),
    ) -> tuple[GatewayAccessPolicy, bool]:
        if policy_key != DEFAULT_GATEWAY_POLICY_KEY:
            raise InvalidGatewayPolicyKeyError(policy_key)
        values = {
            "enabled": enabled,
            "permission_scope": _string_set(permission_scope),
            "allowed_skills": _string_set(allowed_skills),
            "allowed_risk_levels": _string_set(allowed_risk_levels),
        }
        existing = self.get_gateway_policy(policy_key)
        if existing is not None:
            self._update(existing, values)
            return existing, False

        policy = GatewayAccessPolicy(
            id=str(uuid4()),
            policy_key=policy_key,
            **values,
        )
        self._session.add(policy)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_gateway_policy(policy_key)
            if existing is None:
                raise
            self._update(existing, values)
            return existing, False
        return policy, True

    def get_gateway_policy(
        self, policy_key: str = DEFAULT_GATEWAY_POLICY_KEY
    ) -> GatewayAccessPolicy | None:
        statement = select(GatewayAccessPolicy).where(GatewayAccessPolicy.policy_key == policy_key)
        return self._session.scalar(statement)

    def _update(self, policy: object, values: dict[str, object]) -> None:
        changed = False
        for field_name, value in values.items():
            if getattr(policy, field_name) != value:
                setattr(policy, field_name, value)
                changed = True
        if changed:
            self._session.commit()
