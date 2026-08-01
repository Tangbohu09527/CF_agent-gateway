from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.identity.errors import (
    EmployeeIdConflictError,
    IdentityConflictError,
    MappingNotFoundError,
    SourceIdentityConflictError,
)
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)


class IdentityStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_identity(
        self,
        *,
        employee_id: str | None = None,
        display_name: str | None = None,
        status: IdentityStatus = IdentityStatus.ACTIVE,
        enterprise_identity_id: str | None = None,
    ) -> EnterpriseIdentity:
        if employee_id is not None:
            existing = self.get_identity_by_employee_id(employee_id)
            if existing is not None:
                raise EmployeeIdConflictError(employee_id, existing.id)

        identity = EnterpriseIdentity(
            id=enterprise_identity_id or str(uuid4()),
            employee_id=employee_id,
            display_name=display_name,
            status=status,
        )
        self._session.add(identity)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            if employee_id is not None:
                existing = self.get_identity_by_employee_id(employee_id)
                if existing is not None:
                    raise EmployeeIdConflictError(employee_id, existing.id) from None
            if self.get_identity(identity.id) is not None:
                raise IdentityConflictError(
                    f"enterprise identity id is already assigned: {identity.id}"
                ) from None
            raise
        return identity

    def get_identity(self, enterprise_identity_id: str) -> EnterpriseIdentity | None:
        return self._session.get(EnterpriseIdentity, enterprise_identity_id)

    def get_identity_by_employee_id(self, employee_id: str) -> EnterpriseIdentity | None:
        statement = select(EnterpriseIdentity).where(EnterpriseIdentity.employee_id == employee_id)
        return self._session.scalar(statement)

    def create_mapping(
        self,
        *,
        platform: str,
        account_id: str,
        sender_id: str,
        enterprise_identity_id: str,
    ) -> tuple[SourceIdentityMapping, bool]:
        existing = self.get_mapping(
            platform=platform,
            account_id=account_id,
            sender_id=sender_id,
        )
        if existing is not None:
            return self._same_mapping_or_raise(existing, enterprise_identity_id), False

        mapping = SourceIdentityMapping(
            id=str(uuid4()),
            platform=platform,
            account_id=account_id,
            sender_id=sender_id,
            enterprise_identity_id=enterprise_identity_id,
        )
        self._session.add(mapping)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_mapping(
                platform=platform,
                account_id=account_id,
                sender_id=sender_id,
            )
            if existing is not None:
                return self._same_mapping_or_raise(existing, enterprise_identity_id), False
            raise
        return mapping, True

    def get_mapping(
        self, *, platform: str, account_id: str, sender_id: str
    ) -> SourceIdentityMapping | None:
        statement = select(SourceIdentityMapping).where(
            SourceIdentityMapping.platform == platform,
            SourceIdentityMapping.account_id == account_id,
            SourceIdentityMapping.sender_id == sender_id,
        )
        return self._session.scalar(statement)

    def list_mappings(
        self, *, platform: str, account_id: str, sender_id: str
    ) -> list[SourceIdentityMapping]:
        statement = select(SourceIdentityMapping).where(
            SourceIdentityMapping.platform == platform,
            SourceIdentityMapping.account_id == account_id,
            SourceIdentityMapping.sender_id == sender_id,
        )
        return list(self._session.scalars(statement))

    def disable_mapping(
        self, *, platform: str, account_id: str, sender_id: str
    ) -> SourceIdentityMapping:
        mapping = self.get_mapping(
            platform=platform,
            account_id=account_id,
            sender_id=sender_id,
        )
        if mapping is None:
            raise MappingNotFoundError(
                platform=platform,
                account_id=account_id,
                sender_id=sender_id,
            )
        if mapping.enabled:
            mapping.enabled = False
            self._session.commit()
        return mapping

    @staticmethod
    def _same_mapping_or_raise(
        mapping: SourceIdentityMapping, requested_identity_id: str
    ) -> SourceIdentityMapping:
        if mapping.enterprise_identity_id != requested_identity_id:
            raise SourceIdentityConflictError(
                platform=mapping.platform,
                account_id=mapping.account_id,
                sender_id=mapping.sender_id,
                existing_identity_id=mapping.enterprise_identity_id,
                requested_identity_id=requested_identity_id,
            )
        return mapping
