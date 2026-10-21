"""Synthetic native Hermes HTTP/WS peer; candidate routes and persistence are real."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp import WSMsgType, web
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_console import Harness

from cf_agent_gateway.management_console import nodes
from cf_agent_gateway.management_console.nodes import (
    NodeRegistry,
    install_node_routes,
    probe_connection,
    validate_url,
)

TOKEN = "synthetic-hermes-key-2026-not-real"


class Peer:
    def __init__(self):
        self.calls = []
        self.profiles = []
        self.reject_auth = True
        self.management_available = True
        self.bad_format = False
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run, daemon=True)

    async def handle(self, request):
        self.calls.append((request.method, request.path))
        valid = request.headers.get("Authorization") == "Bearer " + TOKEN
        if request.path == "/api/ws":
            valid = request.query.get("token") == TOKEN
        if self.reject_auth and not valid:
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.path == "/v1/models":
            return web.json_response(
                {"object": "list", "data": []} if not self.bad_format else {"ok": True}
            )
        if request.path == "/api/profiles":
            if not self.management_available:
                return web.json_response({}, status=404)
            if request.method == "POST":
                body = await request.json()
                assert body["clone_from_default"] is False
                assert body["clone_all"] is False
                assert body["clone_channels"] is False
                self.profiles.append({"name": body["name"], "description": body["description"]})
            return web.json_response({"profiles": self.profiles})
        if request.path == "/api/config/schema":
            return web.json_response({"fields": [{"path": "display.language"}]})
        if request.path == "/api/health":
            return web.json_response({"version": "synthetic-contract-peer", "commit": "synthetic"})
        if request.path == "/api/ws":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    body = json.loads(msg.data)
                    assert body["method"] == "gateway.ping"
                    await ws.send_json(
                        {"jsonrpc": "2.0", "id": body["id"], "result": {"pong": True}}
                    )
            return ws
        return web.json_response({}, status=404)

    def run(self):
        asyncio.set_event_loop(self.loop)

        async def start():
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", self.handle)
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self.ready.set()

        self.loop.run_until_complete(start())
        self.loop.run_forever()
        self.loop.run_until_complete(self.runner.cleanup())
        self.loop.close()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *_):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    if not hasattr(harness.app.state, "console_nodes"):
        install_node_routes(harness.app, harness.config)
    try:
        yield harness
    finally:
        harness.close()


def create(h, body=None, key="synthetic-request-one"):
    response = h.client.post(
        "/admin/console/nodes/drafts",
        json=body or {"name": "合成 AI 电脑", "business_url": "http://127.0.0.1:18642"},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_generated_configuration_idempotency_secret_masking_registration_and_restart(h):
    draft = create(h)
    again = create(h)
    assert again["id"] == draft["id"]
    assert not draft["registered"]
    assert draft["profile_create"]["status"] == "not_tested"
    reveal = h.post(f"/nodes/{draft['id']}/reveal", {})
    token = reveal["business"]["token"]
    assert len(token) >= 48
    state = h.get("/nodes")
    assert token not in json.dumps(state)
    assert "business_token" not in json.dumps(state)
    registered = h.post(f"/nodes/{draft['id']}/register", {})
    assert registered["registered"]
    assert h.post(f"/nodes/{draft['id']}/register", {})["node_id"] == registered["node_id"]
    registry = NodeRegistry(h.config)
    with h.app.state.database_session_factory() as session:
        rows = registry.listing(session)
    found = next(row for row in rows if row["id"] == draft["id"])
    assert found["node_id"] == registered["node_id"]
    assert registry.find(draft["id"])["business"]["token"] == token
    conflict = h.client.post(
        "/admin/console/nodes/drafts",
        json={"name": "不同请求", "business_url": "http://127.0.0.1:18642"},
        headers={"Idempotency-Key": "synthetic-request-one"},
    )
    assert conflict.status_code == 409


def test_existing_connection_keeps_credential_and_no_reveal_or_duplicate_takeover(h):
    body = {
        "name": "已有主机",
        "business_url": "http://127.0.0.1:18642",
        "setup": "existing",
        "business_token": TOKEN,
    }
    item = create(h, body)
    h.post(f"/nodes/{item['id']}/reveal", {}, expected=403)
    with h.app.state.database_session_factory() as session:
        assert h.app.state.console_nodes.find(item["id"])["business"]["token"] == TOKEN
        assert TOKEN not in json.dumps(h.app.state.console_nodes.listing(session))
    duplicate = h.client.post(
        "/admin/console/nodes/drafts",
        json=body,
        headers={"Idempotency-Key": "different-explicit-request"},
    )
    assert duplicate.status_code == 409


def test_new_business_does_not_generate_management_credentials(h):
    body = {
        "name": "新业务与已有管理",
        "business_url": "http://127.0.0.1:18642",
        "management_url": "http://127.0.0.1:19119",
        "setup": "new",
    }
    before = len(h.get("/nodes")["items"])
    rejected = h.client.post(
        "/admin/console/nodes/drafts",
        json=body,
        headers={"Idempotency-Key": "new-business-existing-management"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "existing_management_credential_required"
    assert len(h.get("/nodes")["items"]) == before

    body["management_token"] = TOKEN
    item = create(h, body, "new-business-existing-management")
    assert item["business"]["credential_generated"]
    assert item["management"]["configured"]
    reveal = h.post(f"/nodes/{item['id']}/reveal", {})
    assert len(reveal["business"]["token"]) >= 48
    assert reveal["management"] is None
    assert TOKEN not in json.dumps(reveal)
    registry = NodeRegistry(h.config)
    raw = registry.find(item["id"])
    assert raw["management"]["token"] == TOKEN
    assert not raw["management"]["generated"]
    assert raw["business"]["token"] != TOKEN
    assert TOKEN not in json.dumps(h.get("/nodes"))
    # A stale pre-fix draft marker must not make a management secret revealable.
    h.app.state.console_nodes.find(item["id"])["management"]["generated"] = True
    assert h.post(f"/nodes/{item['id']}/reveal", {})["management"] is None


def test_new_mode_management_only_uses_existing_secret_without_reveal(h):
    item = create(
        h,
        {
            "name": "已有独立管理入口",
            "management_url": "http://127.0.0.1:19119",
            "management_token": TOKEN,
            "setup": "new",
        },
    )
    assert not item["business"]["credential_generated"]
    assert item["management"]["configured"]
    h.post(f"/nodes/{item['id']}/reveal", {}, expected=403)
    tutorial = h.get("/nodes")["tutorial"]
    assert tutorial["prerequisites"]
    assert "hermes gateway run" in tutorial["business"]["activation"]
    assert "自动配置尚未接通" in tutorial["management"]["limits"]


def test_concurrent_create_requests_reuse_one_draft_and_registration(h):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create(h), range(4)))
    assert len({item["id"] for item in results}) == 1
    key = results[0]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        registrations = list(pool.map(lambda _: h.post(f"/nodes/{key}/register", {}), range(4)))
    assert len({item["node_id"] for item in registrations}) == 1


def test_readonly_network_business_management_rpc_wrong_key_and_no_creates():
    with Peer() as peer:
        endpoint = {"url": peer.url, "token": TOKEN}
        business = probe_connection(endpoint, "business")
        management = probe_connection(endpoint, "management")
        assert business["status"] == management["status"] == "available"
        assert management["checks"]["rpc"] == "read_only_ping_passed"
        assert business["checks"]["invalid_credential_rejected"]
        assert business["model_execution"] == "not_tested"
        assert management["profile_create"] == "not_tested"
        assert all(method == "GET" for method, _ in peer.calls)
        assert peer.profiles == []
        assert (
            probe_connection({"url": peer.url, "token": "wrong"}, "business")["code"]
            == "authentication_failed"
        )
        peer.management_available = False
        assert probe_connection(endpoint, "business")["status"] == "available"
        assert probe_connection(endpoint, "management")["code"] == "interface_not_supported"
        peer.management_available = True
        peer.reject_auth = False
        assert probe_connection(endpoint, "business")["code"] == "authentication_not_enforced"
        peer.reject_auth = True
        peer.bad_format = True
        assert probe_connection(endpoint, "business")["code"] == "invalid_response_format"


def test_actual_node_routes_test_and_explicit_empty_profile_create_are_separate(h):
    with Peer() as peer:
        body = {
            "name": "真实候选后端与合成官方契约",
            "business_url": peer.url,
            "management_url": peer.url,
            "setup": "existing",
            "business_token": TOKEN,
            "management_token": TOKEN,
        }
        item = create(h, body)
        h.post(f"/nodes/{item['id']}/verify-profile", {"acknowledge_creation": True}, expected=409)
        result = h.post(f"/nodes/{item['id']}/test", {})
        assert result["business"]["check"]["status"] == "available"
        assert result["management"]["check"]["status"] == "available"
        assert peer.profiles == []
        h.post(f"/nodes/{item['id']}/verify-profile", {}, expected=422)
        verified = h.post(f"/nodes/{item['id']}/verify-profile", {"acknowledge_creation": True})
        assert verified["profile_create"]["status"] == "verified"
        assert len(peer.profiles) == 1
        h.post(f"/nodes/{item['id']}/verify-profile", {"acknowledge_creation": True})
        assert len(peer.profiles) == 1
        assert not any("prompt" in path or "completions" in path for _, path in peer.calls)


def test_auth_csrf_same_origin_and_validation_never_echo_secret(h):
    no_cookie = TestClient(h.app)
    assert no_cookie.get("/admin/console/nodes").status_code == 401
    response = h.client.post(
        "/admin/console/nodes/drafts",
        json={"business_token": TOKEN},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403
    response = h.client.post(
        "/admin/console/nodes/drafts",
        json={"business_token": TOKEN},
        headers={"Origin": "https://evil.invalid"},
    )
    assert response.status_code == 403
    response = h.client.post(
        "/admin/console/nodes/drafts",
        json={"business_token": TOKEN},
        headers={"Idempotency-Key": "bad-data"},
    )
    assert response.status_code == 422
    assert TOKEN not in response.text


def test_add_management_to_same_node_preserves_business_credentials_and_identity(h):
    item = create(h)
    key = item["id"]
    registered = h.post(f"/nodes/{key}/register", {})
    original = h.post(f"/nodes/{key}/reveal", {})["business"]["token"]
    updated = h.post(f"/nodes/{key}/management", {"url": "http://127.0.0.1:19119", "token": TOKEN})
    assert updated["id"] == key
    assert updated["node_id"] == registered["node_id"]
    assert updated["management"]["configured"]
    assert h.post(f"/nodes/{key}/reveal", {})["business"]["token"] == original
    assert h.post(f"/nodes/{key}/reveal", {})["management"] is None
    h.post(
        f"/nodes/{key}/management", {"url": "http://127.0.0.1:19120", "token": TOKEN}, expected=409
    )
    with h.app.state.database_session_factory() as session:
        state = h.app.state.console_nodes.view(h.app.state.console_nodes.find(key), session)
    assert state["management"]["address"] == "http://127.0.0.1:19119"
    assert TOKEN not in json.dumps(state)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254:8642",
        "https://8.8.8.8:8642",
        "https://127.0.0.1:8642/a",
        "https://user:password@127.0.0.1:8642",
        "https://127.0.0.1:8642/?x=1",
        "http://127.0.0.1:22",
        "file:///etc/passwd",
        "https://0.0.0.0:8642",
    ],
)
def test_node_url_cannot_be_general_proxy_or_metadata_reader(url):
    with pytest.raises(HTTPException):
        validate_url(url)


def test_tls_timeout_network_and_private_dns_policy_are_distinct(monkeypatch):
    def fail_tls(_):
        raise nodes.ssl.SSLError("CERTIFICATE_VERIFY_FAILED")

    def fail_timeout(_):
        raise TimeoutError

    assert (
        probe_connection(
            {"url": "https://127.0.0.1:8642", "token": TOKEN}, "business", factory=fail_tls
        )["code"]
        == "tls_failed"
    )
    assert (
        probe_connection(
            {"url": "https://127.0.0.1:8642", "token": TOKEN}, "business", factory=fail_timeout
        )["code"]
        == "timeout"
    )
    monkeypatch.setattr(
        nodes.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("169.254.169.254", 8642))],
    )
    assert (
        probe_connection({"url": "https://node.example:8642", "token": TOKEN}, "business")["code"]
        == "address_outside_lan"
    )


def test_invalid_imported_credential_is_not_sent_as_a_header():
    def must_not_connect(_):
        raise AssertionError("invalid credential must not be sent")

    assert (
        probe_connection(
            {"url": "http://127.0.0.1:8642", "token": "bad\r\nheader"},
            "business",
            factory=must_not_connect,
        )["code"]
        == "credential_format_invalid"
    )


def test_import_business_and_management_as_one_node_without_auto_register(h, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_BUSINESS_TOKEN", TOKEN)
    (h.root / "existing-nodes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "identity": "gateway-config-stable-id",
                        "name": "既有 Gateway 配置",
                        "source": "gateway_config",
                        "business": {
                            "url": "http://127.0.0.1:18642",
                            "token_env": "SYNTHETIC_BUSINESS_TOKEN",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = NodeRegistry(h.config)
    with h.app.state.database_session_factory() as session:
        item = next(x for x in registry.listing(session) if x["source"] == "gateway_config")
        assert item["business"]["configured"]
        assert not item["management"]["configured"]
        assert not item["registered"]
        assert TOKEN not in json.dumps(item)
        assert registry.filebrowser_status()["storage"] == "not_collected"
        assert registry.filebrowser_status()["status"] == "unknown"


def test_registered_node_without_connection_has_readonly_detail_and_honest_test(h):
    item = next(row for row in h.get("/nodes")["items"] if row["source"] == "candidate_registered")
    key = item["id"]
    before = len(h.peer.calls)
    detail = h.get("/nodes/" + key)
    assert detail["registered"] and detail["node_id"] == item["node_id"]
    assert not detail["business"]["configured"] and not detail["management"]["configured"]
    result = h.post("/nodes/" + key + "/test", {})
    assert result["business"]["check"]["status"] == "not_configured"
    assert result["management"]["check"]["status"] == "not_configured"
    assert result["profile_create"]["status"] == "not_tested"
    assert result["node_id"] == item["node_id"]
    assert len(h.peer.calls) == before
    h.post("/nodes/" + key + "/verify-profile", {"acknowledge_creation": True}, expected=409)
