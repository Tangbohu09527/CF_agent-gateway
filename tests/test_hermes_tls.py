"""Real loopback TLS, with synthetic HTTP peers and no model or live credentials."""

from __future__ import annotations

import hashlib
import json
import ssl
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from cf_agent_gateway.config import HermesSettings, load_settings
from cf_agent_gateway.hermes.client import HermesClient
from cf_agent_gateway.hermes.diagnose import diagnose
from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesResponseError,
    HermesTransportError,
)
from cf_agent_gateway.hermes.tls import verified_ssl_context
from cf_agent_gateway.runtime import dispatch_worker

KEY = "synthetic-local-tls-service-key"
CONTEXT = {
    "url": "https://gateway.invalid/internal/hermes/returns/42",
    "authorization": "Bearer synthetic-local-tls-return-capability",
}


def _test_certificates(directory: Path) -> tuple[Path, Path, Path]:
    """Create throwaway CA/server certificates, never production trust material."""
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isolated Hermes TLS test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, None, None),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, None, None),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, cert_file, key_file = (
        directory / name for name in ("ca.pem", "server.pem", "key.pem")
    )
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_file, cert_file, key_file


@pytest.fixture
def certificates(tmp_path):
    return _test_certificates(tmp_path)


@contextmanager
def _tls_peer(certificates, *, ack="valid", redirect=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def _handle(self):
            path = urlsplit(self.path).path
            calls.append((self.command, path))
            payload = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
            )
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                self._reply(401, {})
                return
            if redirect:
                self._reply(302, {}, {"Location": "http://localhost:1/never-follow"})
                return
            if path == "/v1/models":
                self._reply(200, {"object": "list", "data": [{"id": "test-model"}]})
            elif path == "/v1/chat/completions":
                content = payload["messages"][0]["content"]
                headers = {"X-Hermes-Session-Id": "tls-session"}
                capability = self.headers.get("X-CF-Artifact-Return-Authorization")
                if capability and ack != "missing":
                    headers["X-CF-Artifact-Return-Accepted"] = (
                        hashlib.sha256(capability.encode()).hexdigest()
                        if ack == "valid"
                        else "forged"
                    )
                self._reply(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": content.split("Reply exactly: ")[-1],
                                }
                            }
                        ]
                    },
                    headers,
                )
            elif path.endswith("/messages"):
                self._reply(200, {"object": "list", "session_id": "tls-session", "data": []})
            elif path.endswith("/model"):
                self._reply(
                    200,
                    {
                        "object": "hermes.session.model_lock",
                        "session_id": "tls-session",
                        "runtime": {
                            "model_lock": "accepted",
                            "model": "test-model",
                            "provider": "custom",
                            "requested": {"model": "test-model", "provider": "custom"},
                        },
                    },
                )
            else:
                self._reply(
                    201 if path == "/api/sessions" else 200,
                    {
                        "object": "hermes.session",
                        "session": {
                            "id": "tls-session",
                            "model": "test-model",
                            "parent_session_id": None,
                        },
                    },
                )

        def _reply(self, status, body, headers=None):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates[1], certificates[2])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_explicit_ca_verifies_chat_session_and_diagnostic_https(certificates, monkeypatch):
    # Environment trust/proxies must not substitute for the configured CA.
    monkeypatch.setenv("SSL_CERT_FILE", "missing-and-untrusted-environment.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with _tls_peer(certificates) as (origin, calls):
        with HermesClient(origin, KEY, "test-model", ca_file=str(certificates[0])) as client:
            result = client.chat("read only", artifact_return_context=CONTEXT)
            assert result.assistant_content == "read only"
            proofs = []
            client.prepare_inbound_session(
                "tls-session",
                parent_session_id=None,
                runtime_model="test-model",
                runtime_provider="custom",
                runtime_model_options={},
                allow_create=True,
                expected_history_digest=None,
                record_history=proofs.append,
            )
            client.verify_inbound_session_tip("tls-session")
            assert len(proofs) == 1
        result = diagnose(
            HermesSettings(enabled=True, base_url=origin, ca_file=str(certificates[0])),
            environ={"HERMES_API_KEY": KEY},
            check_auth=True,
            allow_model_call=True,
            profile_reference="synthetic-test",
            profile_revision=1,
        )
    assert result["ok"] is True
    assert result["network"] == "tls_verified"
    assert result["application"] == "unique_marker_returned"
    assert ("POST", "/api/sessions") in calls
    assert ("GET", "/api/sessions/tls-session/messages") in calls
    assert ("GET", "/v1/models") in calls


@pytest.mark.parametrize("fault", ["wrong_ca", "wrong_hostname", "environment_ca_only"])
def test_tls_failure_never_reaches_http_or_falls_back(certificates, tmp_path, monkeypatch, fault):
    other = tmp_path / "other"
    other.mkdir()
    wrong_ca = _test_certificates(other)[0]
    with _tls_peer(certificates) as (origin, calls):
        ca_file = str(wrong_ca if fault == "wrong_ca" else certificates[0])
        if fault == "wrong_hostname":
            origin = origin.replace("localhost", "127.0.0.1")
        if fault == "environment_ca_only":
            monkeypatch.setenv("SSL_CERT_FILE", str(certificates[0]))
            ca_file = None
        with HermesClient(origin, KEY, "test-model", ca_file=ca_file) as client:
            with pytest.raises(HermesTransportError):
                client.chat("never delivered", artifact_return_context=CONTEXT)
            with pytest.raises(HermesResponseError):
                client.verify_inbound_session_tip("tls-session")
        result = diagnose(
            HermesSettings(enabled=True, base_url=origin, ca_file=ca_file),
            environ={"HERMES_API_KEY": KEY},
            check_auth=True,
        )
        assert result["error"] == "tls_verification_failed"
        assert calls == []


@pytest.mark.parametrize("ack", ["missing", "forged"])
def test_authenticated_https_still_requires_request_ack(certificates, ack):
    with _tls_peer(certificates, ack=ack) as (origin, calls):
        with (
            HermesClient(origin, KEY, "test-model", ca_file=str(certificates[0])) as client,
            pytest.raises(HermesResponseError),
        ):
            client.chat("do not trust an HTTP 200", artifact_return_context=CONTEXT)
        assert calls == [("POST", "/v1/chat/completions")]


def test_verified_https_rejects_wrong_source_and_plaintext_redirect(certificates):
    with _tls_peer(certificates) as (origin, calls):
        with HermesClient(
            origin, "wrong-source", "test-model", ca_file=str(certificates[0])
        ) as client:
            with pytest.raises(HermesAPIError) as exc:
                client.chat("unauthorized", artifact_return_context=CONTEXT)
            assert exc.value.status_code == 401
        assert len(calls) == 1
    with _tls_peer(certificates, redirect=True) as (origin, calls):
        with HermesClient(origin, KEY, "test-model", ca_file=str(certificates[0])) as client:
            with pytest.raises(HermesAPIError) as exc:
                client.chat("no redirect", artifact_return_context=CONTEXT)
            assert exc.value.status_code == 302
        assert len(calls) == 1


def test_ca_configuration_loading_and_fail_closed_errors(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("hermes:\n  ca_file: /protected/company-ca.pem\n", encoding="utf-8")
    assert load_settings(config).hermes.ca_file == "/protected/company-ca.pem"
    assert HermesSettings().ca_file is None
    for invalid in ("", "  ", True, 7, "bad\npath"):
        with pytest.raises(ValueError, match="hermes.ca_file"):
            HermesSettings(ca_file=invalid)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ignored.pem"))
    context = verified_ssl_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(ValueError, match="Hermes CA bundle could not be loaded") as exc:
        verified_ssl_context(str(tmp_path / "private-missing-ca.pem"))
    assert "private-missing" not in str(exc.value)


def test_dispatch_runtime_passes_ca_reference_without_reading_extra_environment(monkeypatch):
    from cf_agent_gateway.config import DatabaseSettings, Settings, WorkerSettings

    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        hermes=HermesSettings(enabled=True, base_url="https://hermes.invalid"),
        worker=WorkerSettings(enabled=True),
    )
    settings = replace(settings, hermes=replace(settings.hermes, ca_file="configured-ca.pem"))
    seen = []

    class Client:
        def close(self):
            pass

    class Worker:
        def run(self, **_kwargs):
            pass

    def factory(**kwargs):
        seen.append(kwargs)
        return Client()

    def environment(name):
        assert name == settings.hermes.api_key_env
        return KEY

    monkeypatch.setattr(dispatch_worker, "build_dispatch_worker", lambda *a, **k: Worker())
    dispatch_worker.run_dispatch_worker(
        settings,
        stop_event=Event(),
        hermes_client_factory=factory,
        environment_reader=environment,
    )
    assert seen[0]["ca_file"] == "configured-ca.pem"
