from __future__ import annotations

from sqlalchemy.orm import Session

from cf_agent_gateway.identity.errors import IdentityNotFoundError
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.identity.schemas import (
    IdentityCreate,
    IdentityResolution,
    IdentityResolutionStatus,
    SourceIdentityKey,
    SourceIdentityMappingCreate,
)
from cf_agent_gateway.identity.store import IdentityStore


class IdentityService:
    """Maps source accounts to enterprise identities without granting access."""

    def __init__(self, session: Session) -> None:
        self._store = IdentityStore(session)

    def create_identity(
        self,
        *,
        employee_id: str | None = None,
        display_name: str | None = None,
        status: IdentityStatus = IdentityStatus.ACTIVE,
    ) -> EnterpriseIdentity:
        data = IdentityCreate(
            employee_id=employee_id,
            display_name=display_name,
            status=status,
        )
        return self._store.create_identity(**data.model_dump())

    def create_mapping(
        self,
        *,
        platform: str,
        account_id: str,
        sender_id: str,
        enterprise_identity_id: str,
    ) -> SourceIdentityMapping:
        data = SourceIdentityMappingCreate(
            platform=platform,
            account_id=account_id,
            sender_id=sender_id,
            enterprise_identity_id=enterprise_identity_id,
        )
        if self._store.get_identity(data.enterprise_identity_id) is None:
            raise IdentityNotFoundError(data.enterprise_identity_id)
        mapping, _ = self._store.create_mapping(**data.model_dump())
        return mapping

    def resolve_identity(
        self, *, platform: str, account_id: str, sender_id: str
    ) -> IdentityResolution:
        key = SourceIdentityKey(
            platform=platform,
            account_id=account_id,
            sender_id=sender_id,
        )
        mappings = self._store.list_mappings(**key.model_dump())
        if not mappings:
            return IdentityResolution(status=IdentityResolutionStatus.UNRESOLVED)
        if len(mappings) != 1:
            return IdentityResolution(status=IdentityResolutionStatus.CONFLICT)

        mapping = mappings[0]
        identity = self._store.get_identity(mapping.enterprise_identity_id)
        if identity is None:
            return IdentityResolution(
                status=IdentityResolutionStatus.CONFLICT,
                enterprise_identity_id=mapping.enterprise_identity_id,
                mapping_id=mapping.id,
            )
        if not mapping.enabled or identity.status is not IdentityStatus.ACTIVE:
            return IdentityResolution(
                status=IdentityResolutionStatus.DISABLED,
                enterprise_identity_id=identity.id,
                employee_id=identity.employee_id,
                mapping_id=mapping.id,
            )
        return IdentityResolution(
            status=IdentityResolutionStatus.RESOLVED,
            enterprise_identity_id=identity.id,
            employee_id=identity.employee_id,
            mapping_id=mapping.id,
        )

    def disable_mapping(
        self, *, platform: str, account_id: str, sender_id: str
    ) -> SourceIdentityMapping:
        key = SourceIdentityKey(
            platform=platform,
            account_id=account_id,
            sender_id=sender_id,
        )
        return self._store.disable_mapping(**key.model_dump())
