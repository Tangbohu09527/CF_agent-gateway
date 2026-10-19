from datetime import UTC, datetime
from pathlib import Path

import yaml

from cf_agent_gateway.database import Base, load_database_models

ROOT = Path(__file__).resolve().parents[1]


def test_all_production_services_have_local_process_timezone():
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    assert len(compose["services"]) == 6
    for name, service in compose["services"].items():
        assert service["environment"]["TZ"] == "Asia/Shanghai", name
    dev = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert dev["services"]["gateway"]["environment"]["TZ"] == "Asia/Shanghai"


def test_database_sql_clock_stays_utc_when_process_clock_is_local():
    service = yaml.safe_load((ROOT / "deploy/postgres/compose.yml").read_text())["services"][
        "postgres"
    ]
    assert service["environment"]["TZ"] == "Asia/Shanghai"
    assert service["command"] == [
        "postgres",
        "-c",
        "timezone=UTC",
        "-c",
        "log_timezone=Asia/Shanghai",
    ]
    # No initdb/data rewrite or role/database timezone mutation accompanies this change.
    assert service["environment"]["POSTGRES_INITDB_ARGS"] == "--auth-host=scram-sha-256"


def test_persisted_datetime_contract_and_absolute_deadlines_remain_aware():
    load_database_models()
    columns = [
        c
        for table in Base.metadata.tables.values()
        for c in table.columns
        if c.type.__class__.__name__ == "DateTime"
    ]
    assert columns
    assert all(c.type.timezone is True for c in columns)
    start = datetime.fromisoformat("2026-10-18T11:10:46+00:00")
    local = datetime.fromisoformat("2026-10-18T19:10:46+08:00")
    assert start.timestamp() == local.timestamp()
    assert local.astimezone(UTC) == start


def test_https_override_changes_only_process_timezone():
    override = yaml.safe_load((ROOT / "deploy/https/compose.timezone.yaml").read_text())
    assert override == {"services": {"https": {"environment": {"TZ": "Asia/Shanghai"}}}}
