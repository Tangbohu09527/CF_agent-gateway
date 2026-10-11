"""Explicit TLS trust shared by Hermes chat, session and diagnostic requests."""

from __future__ import annotations

import ssl

import certifi


def verified_ssl_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Use a configured PEM bundle or HTTPX's default roots, never environment trust.

    Passing an explicit default cafile avoids SSL_CERT_FILE/SSL_CERT_DIR, just as
    HTTPX's ``trust_env=False`` does. A custom bundle replaces the root set; it
    must contain every CA needed by the configured endpoint. Hostname checking
    and certificate verification cannot be disabled through this interface.
    """
    if ca_file is not None and (
        not isinstance(ca_file, str)
        or not ca_file.strip()
        or any(ord(character) < 0x20 for character in ca_file)
    ):
        raise ValueError("Hermes CA file must reference a PEM CA bundle")
    try:
        context = ssl.create_default_context(cafile=ca_file.strip() if ca_file else certifi.where())
    except (OSError, ValueError, ssl.SSLError):
        # Do not expose an internal path or certificate parser details.
        raise ValueError("Hermes CA bundle could not be loaded") from None
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context
