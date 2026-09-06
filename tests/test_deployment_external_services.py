"""Keep A's external substitute faithful; no Gateway deployment assets are prepared here."""

from __future__ import annotations

import importlib.util
import json
import secrets
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def wechat_external(tmp_path: Path):
    path = Path(__file__).parent / "deployment/external_services.py"
    spec = importlib.util.spec_from_file_location("deployment_external", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = secrets.token_urlsafe(32)
    token_file = tmp_path / "test-token"
    token_file.write_text(token)
    server = module.ExternalServer(0, "wechat", token_file, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path: str, *, session: bool = True, authenticated: bool = True):
        headers = {}
        if authenticated:
            headers["Authorization"] = "Bearer " + token
        if session:
            headers["X-Session-Id"] = "default"
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}{path}", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        yield module, server, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_substitute_requires_real_bearer_and_session_contract(wechat_external) -> None:
    _, server, request = wechat_external
    assert request("/api/status/auth", authenticated=False)[0] == 401
    assert request("/api/status/auth", session=False)[0] == 400
    assert request("/api/status/auth") == (200, {"status": "logged_out"})
    assert server.allow_login is False
    assert request("/health", authenticated=False, session=False)[0] == 200


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"account_present": False}, {"status": "logged_in"}),
        ({"account": None}, {"status": "logged_in", "loggedInUser": None}),
        (
            {"account": {"unexpected": "account"}},
            {"status": "logged_in", "loggedInUser": {"unexpected": "account"}},
        ),
    ],
)
def test_substitute_preserves_invalid_auth_for_real_client_to_reject(
    wechat_external, change, expected
) -> None:
    _, server, request = wechat_external
    server.control({"auth": "logged_in", **change})
    assert request("/api/status/auth") == (200, expected)


def test_substitute_retains_separate_account_histories_and_message_sequences(
    wechat_external,
) -> None:
    module, server, request = wechat_external
    server.control({"auth": "logged_in", "message": "first"})
    server.control({"message": "second"})
    original = request("/api/messages/" + module.CHAT)[1]["messages"]
    assert [item["localId"] for item in original] == [1, 2]
    server.control({"account": "wxid_other_account", "message": "other", "local_id": 1})
    other = request("/api/messages/" + module.CHAT)[1]["messages"]
    assert len(other) == 1 and other[0]["localId"] == 1
    server.control({"account": module.ACCOUNT, "message": "third"})
    restored = request("/api/messages/" + module.CHAT)[1]["messages"]
    assert [item["localId"] for item in restored] == [1, 2, 3]
    assert restored[:2] == original
    assert server.account_polls[json.dumps(module.ACCOUNT)] == 2
    assert server.account_polls[json.dumps("wxid_other_account")] == 1


def test_substitute_announces_an_empty_conversation_before_new_messages(wechat_external) -> None:
    module, server, request = wechat_external
    extra = module.CHAT + "_unbound"
    server.control({"conversation_id": extra})
    assert {chat["chatId"] for chat in request("/api/chats")[1]["chats"]} == {module.CHAT, extra}
    assert request("/api/messages/" + extra)[1]["messages"] == []
    server.control({"conversation_id": extra, "message": "unbound"})
    observed = request("/api/messages/" + extra)[1]["messages"]
    assert len(observed) == 1 and observed[0]["sender"] == module.SENDER
    assert request("/api/messages/" + module.CHAT)[1]["messages"] == []
    assert server.conversation_polls[json.dumps([module.ACCOUNT, extra])] == 2
