from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cf_agent_gateway.config import ApiSettings, DatabaseSettings, Settings
from cf_agent_gateway.gateway import security
from cf_agent_gateway.gateway.app import create_app

TOKEN_ENV = "TEST_CF_GATEWAY_API_TOKEN"
TOKEN = "test-message-api-token"
AUTHORIZATION = {"Authorization": f"Bearer {TOKEN}"}


def message_event() -> dict[str, object]:
    return {
        "event_id": "event-001",
        "source": "test-channel",
        "source_account_id": "bot-001",
        "source_message_id": "source-message-001",
        "conversation_id": "conversation-001",
        "conversation_type": "private",
        "is_mentioned": None,
        "is_self": False,
        "sender_type": "human",
        "sender_id": "user-001",
        "message_type": "text",
        "content": "authenticated message",
        "timestamp": "2026-08-21T10:00:00+08:00",
    }


@pytest.fixture
def protected_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        api=ApiSettings(message_auth_enabled=True, bearer_token_env=TOKEN_ENV),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_health_remains_public(protected_client: TestClient) -> None:
    response = protected_client.get("/health")

    assert response.status_code == 200


def test_all_message_endpoints_require_bearer_token(protected_client: TestClient) -> None:
    responses = [
        protected_client.post("/internal/messages", json=message_event()),
        protected_client.get("/messages/1"),
        protected_client.get(
            "/sources/test-channel/accounts/bot-001/conversations/conversation-001/messages"
        ),
    ]

    for response in responses:
        assert response.status_code == 401
        assert response.json() == {"detail": "unauthorized"}
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    ["Basic dXNlcjpwYXNz", "Bearer wrong-token", "Digest opaque-value"],
)
def test_invalid_authorization_has_stable_non_sensitive_error(
    protected_client: TestClient,
    authorization: str,
) -> None:
    response = protected_client.get(
        "/messages/1",
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert TOKEN not in response.text


def test_missing_configured_secret_fails_closed(
    protected_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOKEN_ENV)

    response = protected_client.post(
        "/internal/messages",
        json=message_event(),
        headers=AUTHORIZATION,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_non_ascii_configured_secret_fails_closed_without_server_error(
    protected_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "non-ascii-\u5bc6\u94a5")

    response = protected_client.get(
        "/messages/1",
        headers=AUTHORIZATION,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_bearer_secret_uses_constant_time_comparison(
    protected_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return False

    monkeypatch.setattr(security, "compare_digest", compare)

    response = protected_client.get(
        "/messages/1",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert calls == [(b"wrong-token", TOKEN.encode("utf-8"))]


def test_valid_bearer_token_allows_message_workflow(protected_client: TestClient) -> None:
    created = protected_client.post(
        "/internal/messages",
        json=message_event(),
        headers=AUTHORIZATION,
    )

    assert created.status_code == 201
    message_id = created.json()["id"]
    fetched = protected_client.get(f"/messages/{message_id}", headers=AUTHORIZATION)
    listed = protected_client.get(
        "/sources/test-channel/accounts/bot-001/conversations/conversation-001/messages",
        headers=AUTHORIZATION,
    )

    assert fetched.status_code == 200
    assert fetched.json()["id"] == message_id
    assert listed.status_code == 200
    assert [message["id"] for message in listed.json()] == [message_id]
