import math
from dataclasses import dataclass
from dataclasses import field as dataclass_field


@dataclass(frozen=True, slots=True)
class HostBindingSettings:
    enabled: bool = False
    dedicated_endpoint_confirmed: bool = False
    host_id: str = ""
    profile_reference: str = ""
    profile_revision: int = 1
    service_token_env: str = "CF_GATEWAY_HERMES_HOST_TOKEN"
    encryption_key_env: str = "CF_GATEWAY_HOST_GRANT_KEY"
    lease_seconds: float = 30.0
    runtime_model: str = ""
    runtime_provider: str = ""
    runtime_model_options: dict = dataclass_field(default_factory=dict)
    legacy_runtime_confirmed: bool = False

    def __post_init__(self):
        if not isinstance(self.enabled, bool) or not isinstance(
            self.dedicated_endpoint_confirmed, bool
        ):
            raise ValueError("host_binding flags must be boolean")
        for field in ("service_token_env", "encryption_key_env"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.isidentifier():
                raise ValueError("host_binding secret must name an environment variable")
        if self.service_token_env == self.encryption_key_env:
            raise ValueError("host_binding secrets must be separate")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, (float, int))
            or not math.isfinite(self.lease_seconds)
            or not 0 < self.lease_seconds <= 30
        ):
            raise ValueError("host_binding.lease_seconds must be in (0, 30]")
        if (
            isinstance(self.profile_revision, bool)
            or not isinstance(self.profile_revision, int)
            or self.profile_revision < 1
        ):
            raise ValueError("host_binding.profile_revision must be positive")
        for field in ("host_id", "profile_reference"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or len(value) > 128
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
            ):
                raise ValueError("invalid host_binding identity")
        if self.enabled and (
            not self.dedicated_endpoint_confirmed or not self.host_id or not self.profile_reference
        ):
            raise ValueError("host_binding requires an approved dedicated endpoint and profile")
        if not isinstance(self.legacy_runtime_confirmed, bool):
            raise ValueError("host_binding.legacy_runtime_confirmed must be boolean")
        for field_name in ("runtime_model", "runtime_provider"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) > 200
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
                or "://" in value
            ):
                raise ValueError("invalid host_binding runtime identity")
        if self.enabled and (
            not self.runtime_model
            or not self.runtime_provider
            or self.runtime_model == "hermes-agent"
        ):
            raise ValueError("host_binding requires explicit provider and real model ID")
        options = self.runtime_model_options
        if not isinstance(options, dict) or set(options) - {"reasoning", "service_tier", "fast"}:
            raise ValueError("unsupported host_binding runtime options")
        if "fast" in options and not isinstance(options["fast"], bool):
            raise ValueError("runtime fast must be boolean")
        if "service_tier" in options and options["service_tier"] not in {
            "auto",
            "default",
            "flex",
            "priority",
            "standard",
        }:
            raise ValueError("unsupported runtime service tier")
        if "reasoning" in options:
            reasoning = options["reasoning"]
            if not isinstance(reasoning, dict) or set(reasoning) - {"enabled", "effort"}:
                raise ValueError("unsupported runtime reasoning options")
            if "enabled" in reasoning and not isinstance(reasoning["enabled"], bool):
                raise ValueError("runtime reasoning enabled must be boolean")
            if "effort" in reasoning and reasoning["effort"] not in {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
            }:
                raise ValueError("unsupported runtime reasoning effort")
