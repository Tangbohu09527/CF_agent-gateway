from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"))
    token = "test-gateway-api-token"
    monkeypatch.setenv(settings.api.token_env, token)
    with TestClient(
        create_app(settings),
        headers={"Authorization": f"Bearer {token}"},
    ) as test_client:
        yield test_client
