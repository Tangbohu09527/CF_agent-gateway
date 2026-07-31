from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"))
    with TestClient(create_app(settings)) as test_client:
        yield test_client
