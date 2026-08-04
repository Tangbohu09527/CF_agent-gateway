from __future__ import annotations


class AccessPolicyError(Exception):
    """Base class for stable access-policy domain errors."""

    code = "access_policy_error"


class InvalidPolicyWindowError(AccessPolicyError):
    code = "invalid_policy_window"

    def __init__(self) -> None:
        super().__init__("valid_until must be greater than or equal to valid_from")


class InvalidGatewayPolicyKeyError(AccessPolicyError):
    code = "invalid_gateway_policy_key"

    def __init__(self, policy_key: str) -> None:
        self.policy_key = policy_key
        super().__init__("gateway policy_key must be 'default' in V1")
