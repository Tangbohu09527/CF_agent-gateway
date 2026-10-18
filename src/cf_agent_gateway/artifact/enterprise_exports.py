"""Import an authorized enterprise ZIP directly from FileBrowser to Gateway.

The model cannot choose an origin, credential, local path or recipient. The
current return capability and a separate operator-configured source scope both
have to authorize the import. No new privileged deployment or runtime API.
"""

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from cf_agent_gateway.hermes.return_bridge.enterprise_files import export_metadata, normalized_path
from cf_agent_gateway.hermes.return_bridge.zip_contract import MAX_ZIP_BYTES, verify_zip
from cf_agent_gateway.hermes.tls import verified_ssl_context


@dataclass(frozen=True)
class EnterpriseExportSettings:
    enabled: bool = False
    base_url: str = ""
    token_env: str = "CF_GATEWAY_ENTERPRISE_EXPORT_TOKEN"
    ca_file: str | None = None
    source: str = ""
    root: str = ""
    allowed_bindings: tuple = ()
    max_bytes: int = MAX_ZIP_BYTES
    timeout_seconds: int = 120

    def __post_init__(self):
        if type(self.enabled) is not bool or not self.token_env.isidentifier():
            raise ValueError("invalid enterprise_exports configuration")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_ZIP_BYTES:
            raise ValueError("invalid enterprise_exports limit")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 300:
            raise ValueError("invalid enterprise_exports timeout")
        if self.enabled:
            url = urlsplit(self.base_url)
            if (
                not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
                or url.port == 0
                or any(ord(c) < 33 or ord(c) > 126 for c in self.base_url)
                or not (
                    url.scheme == "https"
                    or url.scheme == "http"
                    and url.hostname in {"127.0.0.1", "localhost", "::1"}
                )
                or not self.source
            ):
                raise ValueError("invalid enterprise_exports origin or source")
            normalized_path(self.root)
            if not self.allowed_bindings or any(
                not isinstance(binding, (tuple, list))
                or len(binding) != 3
                or any(not isinstance(value, str) or not value for value in binding)
                for binding in self.allowed_bindings
            ):
                raise ValueError("enterprise_exports requires exact account/chat/identity bindings")


def permitted(settings, export, *, account, chat, identity):
    export_metadata(export)
    if (
        not settings.enabled
        or (account, chat, identity) not in {tuple(b) for b in settings.allowed_bindings}
        or export["source"] != settings.source
        or not export["path"].startswith(settings.root.rstrip("/") + "/")
        or export["size_bytes"] > settings.max_bytes
    ):
        raise ValueError("enterprise export unavailable")


def fetch_verified(settings, export, *, check_active):
    """Stream bytes into a private temporary file; never buffer the ZIP on Hermes."""
    token = os.environ.get(settings.token_env, "")
    if not token:
        raise ValueError("enterprise export unavailable")
    deadline = time.monotonic() + settings.timeout_seconds

    def active():
        check_active()
        if time.monotonic() >= deadline:
            raise ValueError("enterprise export timed out")

    content = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - ownership transfers to caller
    try:
        digest, size = hashlib.sha256(), 0
        with httpx.Client(
            verify=verified_ssl_context(settings.ca_file),
            trust_env=False,
            follow_redirects=False,
            timeout=min(10, settings.timeout_seconds),
        ) as client:
            active()
            with client.stream(
                "GET",
                settings.base_url.rstrip("/") + "/api/resources/download",
                params={"source": export["source"], "file": export["path"]},
                headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code != 200 or response.headers.get("content-encoding"):
                    raise ValueError("enterprise export unavailable")
                for chunk in response.iter_raw(65536):
                    active()
                    size += len(chunk)
                    if size > min(settings.max_bytes, export["size_bytes"]):
                        raise ValueError("enterprise export changed")
                    digest.update(chunk)
                    content.write(chunk)
        if size != export["size_bytes"] or digest.hexdigest() != export["sha256"]:
            raise ValueError("enterprise export changed")
        manifest = verify_zip(content, check_active=active)
        if manifest != sorted(export["manifest"], key=lambda item: item["path"]):
            raise ValueError("enterprise export members changed")
        active()
        content.seek(0)
        return content
    except BaseException:
        content.close()
        raise
