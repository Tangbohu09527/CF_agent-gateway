from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TextIO

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine

from cf_agent_gateway.config import load_settings
from cf_agent_gateway.database import create_database_engine, ensure_database_directory

DEFAULT_CONFIG_PATH = "config/config.yaml"
PACKAGED_SCRIPT_LOCATION = "cf_agent_gateway:migrations"


def create_migration_config(config_path: str | Path | None = None) -> Config:
    if config_path is not None:
        return Config(str(Path(config_path).resolve()))

    migration_config = Config()
    migration_config.set_main_option("script_location", PACKAGED_SCRIPT_LOCATION)
    return migration_config


def upgrade_database(
    engine: Engine,
    revision: str = "head",
    *,
    config_path: str | Path | None = None,
) -> None:
    ensure_database_directory(engine)
    migration_config = create_migration_config(config_path)
    with engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.upgrade(migration_config, revision)


def get_schema_version(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def migrate_database(
    database_url: str,
    *,
    config_path: str | Path | None = None,
) -> str | None:
    engine = create_database_engine(database_url)
    try:
        upgrade_database(engine, config_path=config_path)
        return get_schema_version(engine)
    finally:
        engine.dispose()


def main() -> int:
    application_config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
    alembic_config_path = os.getenv("CF_GATEWAY_ALEMBIC_CONFIG") or None
    try:
        settings = load_settings(application_config_path)
        schema_version = migrate_database(
            settings.database.url,
            config_path=alembic_config_path,
        )
    except Exception:
        _write_json({"error_code": "database_migration_failed"}, file=sys.stderr)
        return 1

    _write_json({"schema_version": schema_version}, file=sys.stdout)
    return 0


def _write_json(payload: dict[str, str | None], *, file: TextIO) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=file)


if __name__ == "__main__":
    raise SystemExit(main())
