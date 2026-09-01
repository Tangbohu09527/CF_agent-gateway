from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cf_agent_gateway.config import APISettings, DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app

TOKEN_ENV = "TEST_GATEWAY_API_TOKEN"
TOKEN = "test-only-gateway-token"
ADMIN_TOKEN_ENV = "TEST_GATEWAY_ADMIN_TOKEN"
ADMIN_TOKEN = "test-only-admin-token"


def _settings(*, max_request_body_bytes: int = 1_048_576) -> Settings:
    return Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        api=APISettings(
            token_env=TOKEN_ENV,
            max_request_body_bytes=max_request_body_bytes,
        ),
    )


def _event(index: int) -> dict[str, object]:
    return {
        "event_id": f"security-event-{index}",
        "source": "wechat",
        "source_account_id": "security-account",
        "source_message_id": f"security-message-{index}",
        "conversation_id": "security-conversation",
        "conversation_type": "private",
        "is_mentioned": None,
        "is_self": False,
        "sender_type": "human",
        "sender_id": "security-sender",
        "message_type": "text",
        "content": "bounded test content",
        "timestamp": datetime(2026, 8, 23, 1, index, tzinfo=UTC).isoformat(),
    }


def test_message_api_fails_closed_without_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with TestClient(create_app(_settings())) as client:
        response = client.post("/internal/messages", json=_event(1))

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_message_api_rejects_missing_and_wrong_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with TestClient(create_app(_settings())) as client:
        missing = client.post("/internal/messages", json=_event(1))
        wrong = client.post(
            "/internal/messages",
            json=_event(2),
            headers={"Authorization": "Bearer wrong-test-token"},
        )
        accepted = client.post(
            "/internal/messages",
            json=_event(3),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 201
    assert TOKEN not in missing.text + wrong.text + accepted.text


def test_health_endpoints_remain_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with TestClient(create_app(_settings())) as client:
        liveness = client.get("/health")
        readiness = client.get("/ready")

    assert liveness.status_code == 200
    assert readiness.status_code == 200


def test_admin_api_uses_the_configured_token_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.delenv("CF_AGENT_GATEWAY_ADMIN_TOKEN", raising=False)
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        api=APISettings(
            token_env=TOKEN_ENV,
            admin_token_env=ADMIN_TOKEN_ENV,
        ),
    )

    with TestClient(create_app(settings)) as client:
        wrong = client.get(
            "/admin/dispatches/1",
            headers={"Authorization": "Bearer wrong-admin-token"},
        )
        accepted = client.get(
            "/admin/dispatches/1",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    assert wrong.status_code == 401
    assert accepted.status_code == 404
    assert ADMIN_TOKEN not in wrong.text + accepted.text


def test_admin_thread_path_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        api=APISettings(
            token_env=TOKEN_ENV,
            admin_token_env=ADMIN_TOKEN_ENV,
        ),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/admin/threads/{'x' * 37}",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    assert response.status_code == 422
    assert ADMIN_TOKEN not in response.text


def test_request_body_limit_rejects_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with TestClient(create_app(_settings(max_request_body_bytes=64))) as client:
        response = client.post(
            "/internal/messages",
            content=b"x" * 65,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_conversation_history_is_paginated_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(_settings()), headers=headers) as client:
        for index in range(3):
            assert client.post("/internal/messages", json=_event(index)).status_code == 201
        page = client.get(
            "/sources/wechat/accounts/security-account/"
            "conversations/security-conversation/messages",
            params={"limit": 2, "offset": 1},
        )
        excessive = client.get(
            "/sources/wechat/accounts/security-account/"
            "conversations/security-conversation/messages",
            params={"limit": 101},
        )

    assert page.status_code == 200
    assert [item["source_message_id"] for item in page.json()] == [
        "security-message-1",
        "security-message-2",
    ]
    assert excessive.status_code == 422
