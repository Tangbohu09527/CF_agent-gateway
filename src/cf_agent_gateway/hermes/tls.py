"""Explicit TLS trust shared by Hermes chat, session and diagnostic requests."""

from __future__ import annotations

import _ssl
import ssl
import sys

import certifi


def stdlib_ssl_context(protocol: int) -> ssl.SSLContext:
    """Create a local native context without undoing Hermes' truststore injection.

    The fixed official CLI injects truststore globally. Its Windows wrapper is
    client-oriented and attempts to verify peer certificates for a TLS server,
    including clients which correctly send no certificate. Use its saved
    original class only for our explicit server/trust boundaries.
    """
    try:
        from truststore._ssl_constants import _original_SSLContext
    except ModuleNotFoundError as exc:
        if exc.name != "truststore":
            raise ValueError("Unsupported Hermes TLS runtime") from None
        context_class = ssl.SSLContext
    else:
        context_class = _original_SSLContext
    context = context_class(protocol)
    # stdlib's Python setters resolve the globally replaced ssl.SSLContext;
    # its native descriptor avoids recursion without touching global state.
    _ssl._SSLContext.minimum_version.__set__(context, ssl.TLSVersion.TLSv1_2)
    return context


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
        context = stdlib_ssl_context(ssl.PROTOCOL_TLS_CLIENT)
        _ssl._SSLContext.verify_mode.__set__(context, ssl.CERT_REQUIRED)
        context.check_hostname = True
        # Match create_default_context's stricter Python 3.13+ validation flags.
        if sys.version_info >= (3, 13):
            _ssl._SSLContext.verify_flags.__set__(
                context,
                context.verify_flags | ssl.VERIFY_X509_PARTIAL_CHAIN | ssl.VERIFY_X509_STRICT,
            )
        context.load_verify_locations(cafile=ca_file.strip() if ca_file else certifi.where())
    except (OSError, ValueError, ssl.SSLError):
        # Do not expose an internal path or certificate parser details.
        raise ValueError("Hermes CA bundle could not be loaded") from None
    return context
