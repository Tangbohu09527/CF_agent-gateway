"""Child-only truststore regression: synthetic TLS and authenticated GET only."""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace

import truststore

# Match the official CLI's injection before importing our plugin/helpers.
truststore.inject_into_ssl()
injected_class = ssl.SSLContext

import httpx  # noqa: E402
from aiohttp import web  # noqa: E402
from truststore._ssl_constants import _original_SSLContext  # noqa: E402

from cf_agent_gateway.hermes.return_bridge import plugin  # noqa: E402
from cf_agent_gateway.hermes.tls import verified_ssl_context  # noqa: E402


async def run(directory):
    ca, certificate, private_key = (
        directory / name for name in ("ca.pem", "server.pem", "key.pem")
    )
    wrong_ca = directory / "other/ca.pem"
    work = directory / "work"
    work.mkdir(mode=0o700)
    key = "synthetic-local-readonly-truststore-key"
    calls = []
    os.environ["SSL_CERT_FILE"] = str(ca)
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
    app = web.Application()

    async def models(request):
        calls.append(request.path)
        if request.headers.get("Authorization") != "Bearer " + key:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"object": "list", "data": []})

    app.router.add_get("/v1/models", models)
    adapter = SimpleNamespace(_host="127.0.0.1", _runner=None)
    settings = plugin.BridgeSettings(
        gateway_origin="https://localhost:443",
        gateway_ca_file=str(ca),
        work_root=str(work),
        profile_reference="isolated-truststore-probe",
        tls_host="127.0.0.1",
        tls_port=0,
        tls_cert_file=str(certificate),
        tls_key_file=str(private_key),
    )
    state = plugin.configure_app(app, adapter, settings)
    adapter._runner = web.AppRunner(app)
    await adapter._runner.setup()
    try:
        async with asyncio.timeout(6):
            while not state["ready"]:
                await asyncio.sleep(0.01)
        server_context = state["site"]._ssl_context
        assert type(server_context) is _original_SSLContext
        assert server_context.minimum_version >= ssl.TLSVersion.TLSv1_2
        assert server_context.verify_mode == ssl.CERT_NONE  # No client certificate protocol.
        port = state["site"]._server.sockets[0].getsockname()[1]
        context = verified_ssl_context(str(ca))
        assert type(context) is _original_SSLContext
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
        assert len(context.get_ca_certs()) == 1  # Explicit CA replaces system roots.
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            result = await client.get(
                f"https://localhost:{port}/v1/models", headers={"Authorization": "Bearer " + key}
            )
            assert result.status_code == 200 and result.json()["object"] == "list"
            result = await client.get(
                f"https://localhost:{port}/v1/models", headers={"Authorization": "Bearer wrong"}
            )
            assert result.status_code == 401
        for host, ca_file in (
            ("127.0.0.1", str(ca)),
            ("localhost", str(wrong_ca)),
            ("localhost", None),  # Environment-only CA must not become trusted.
        ):
            async with httpx.AsyncClient(
                verify=verified_ssl_context(ca_file), trust_env=False
            ) as client:
                try:
                    await client.get(f"https://{host}:{port}/v1/models")
                except httpx.ConnectError as exc:
                    assert "CERTIFICATE_VERIFY_FAILED" in str(exc)
                else:
                    raise AssertionError("Untrusted TLS peer accepted")
        assert calls == ["/v1/models", "/v1/models"]
        assert ssl.SSLContext is injected_class
    finally:
        await adapter._runner.cleanup()
    assert ssl.SSLContext is injected_class
    print("truststore-local-context-ok")


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1])))
