"""Opt-in task output handoff; no channel credentials are shared with the host."""

from dataclasses import dataclass
from urllib.parse import urlsplit

RETURN_URL_HEADER = "X-CF-Artifact-Return-URL"
RETURN_AUTH_HEADER = "X-CF-Artifact-Return-Authorization"
RETURN_ACK_HEADER = "X-CF-Artifact-Return-Accepted"


@dataclass(frozen=True, slots=True)
class ArtifactReturnSettings:
    enabled: bool = False
    host_contract_confirmed: bool = False
    public_base_url: str = ""
    profile_reference: str = ""
    profile_revision: int = 1
    signing_key_env: str = "CF_GATEWAY_ARTIFACT_RETURN_KEY"
    max_bytes: int = 1_048_576
    max_artifacts: int = 4
    ttl_seconds: int = 900

    def __post_init__(self):
        if not isinstance(self.enabled, bool) or not isinstance(self.host_contract_confirmed, bool):
            raise ValueError("artifact_return flags must be boolean")
        if not isinstance(self.signing_key_env, str) or not self.signing_key_env.isidentifier():
            raise ValueError("artifact_return signing key must name an environment variable")
        for name, maximum in (
            ("max_bytes", 1_048_576),
            ("max_artifacts", 8),
            ("ttl_seconds", 3600),
            ("profile_revision", 2_147_483_647),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"invalid artifact_return.{name}")
        if (
            not isinstance(self.profile_reference, str)
            or len(self.profile_reference) > 128
            or any(ord(c) < 33 or ord(c) > 126 for c in self.profile_reference)
        ):
            raise ValueError("invalid artifact_return profile reference")
        if not isinstance(self.public_base_url, str):
            raise ValueError("invalid artifact_return public URL")
        if self.public_base_url:
            try:
                url = urlsplit(self.public_base_url)
                valid = (
                    bool(url.hostname)
                    and url.port != 0
                    and not url.username
                    and not url.password
                    and not url.query
                    and not url.fragment
                    and url.path in {"", "/"}
                    and (
                        url.scheme == "https"
                        or (
                            url.scheme == "http"
                            and url.hostname in {"127.0.0.1", "::1", "localhost"}
                        )
                    )
                    and not any(c.isspace() or ord(c) < 32 for c in self.public_base_url)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("artifact_return requires HTTPS (HTTP only on loopback)")
        if self.enabled and not (
            self.host_contract_confirmed and self.public_base_url and self.profile_reference
        ):
            raise ValueError("artifact_return requires verified host support, URL and profile")
