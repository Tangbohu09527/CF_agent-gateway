from pathlib import Path

import pytest

from cf_agent_gateway.database import create_database_engine
from cf_agent_gateway.runtime.errors import WechatPollGateUnavailableError
from cf_agent_gateway.runtime.poll_gate import acquire_wechat_poll_gate


def test_memory_poll_gate_rejects_a_duplicate_owner_and_allows_takeover() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with (
            acquire_wechat_poll_gate(engine),
            pytest.raises(WechatPollGateUnavailableError),
            acquire_wechat_poll_gate(engine),
        ):
            pass

        with acquire_wechat_poll_gate(engine):
            pass
    finally:
        engine.dispose()


def test_file_poll_gate_coordinates_independent_engines_and_releases_on_exit(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "gateway.db"
    url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    first = create_database_engine(url)
    second = create_database_engine(url)
    try:
        with (
            acquire_wechat_poll_gate(first),
            pytest.raises(WechatPollGateUnavailableError),
            acquire_wechat_poll_gate(second),
        ):
            pass

        with acquire_wechat_poll_gate(second):
            pass
    finally:
        first.dispose()
        second.dispose()
