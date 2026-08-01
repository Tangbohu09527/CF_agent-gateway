from __future__ import annotations


class IdentityError(Exception):
    """Base class for stable identity domain errors."""

    code = "identity_error"


class IdentityNotFoundError(IdentityError):
    code = "identity_not_found"

    def __init__(self, enterprise_identity_id: str) -> None:
        self.enterprise_identity_id = enterprise_identity_id
        super().__init__(f"enterprise identity not found: {enterprise_identity_id}")


class IdentityConflictError(IdentityError):
    code = "identity_conflict"


class EmployeeIdConflictError(IdentityConflictError):
    code = "employee_id_conflict"

    def __init__(self, employee_id: str, existing_identity_id: str) -> None:
        self.employee_id = employee_id
        self.existing_identity_id = existing_identity_id
        super().__init__(f"employee_id is already assigned: {employee_id}")


class SourceIdentityConflictError(IdentityConflictError):
    code = "source_identity_conflict"

    def __init__(
        self,
        *,
        platform: str,
        account_id: str,
        sender_id: str,
        existing_identity_id: str,
        requested_identity_id: str,
    ) -> None:
        self.platform = platform
        self.account_id = account_id
        self.sender_id = sender_id
        self.existing_identity_id = existing_identity_id
        self.requested_identity_id = requested_identity_id
        super().__init__("source identity is already mapped to a different enterprise identity")


class MappingNotFoundError(IdentityError):
    code = "source_identity_mapping_not_found"

    def __init__(self, *, platform: str, account_id: str, sender_id: str) -> None:
        self.platform = platform
        self.account_id = account_id
        self.sender_id = sender_id
        super().__init__("source identity mapping not found")
