from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence

from sqlalchemy import Engine

from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.database import (
    check_database_migrations,
    create_database_engine,
    initialize_database,
)
from cf_agent_gateway.logging import configure_logging

DEFAULT_CONFIG_PATH = "config/config.yaml"
STARTUP_MODE_ENV = "CF_GATEWAY_STARTUP_MIGRATION_MODE"
logger = logging.getLogger(__name__)


class StartupMigrationError(RuntimeError):
    """The database is unavailable or not at the schema required by this build."""


def prepare_database(engine: Engine, *, migrate: bool = False) -> None:
    """Optionally migrate, then prove this build can safely use the schema."""

    if migrate:
        initialize_database(engine)
    else:
        check_database_migrations(engine)


def run_database_startup(settings: Settings, *, migrate: bool = False) -> None:
    engine = create_database_engine(settings.database.url)
    try:
        prepare_database(engine, migrate=migrate)
    finally:
        engine.dispose()


def database_startup_check_enabled() -> bool:
    mode = os.getenv(STARTUP_MODE_ENV)
    if mode is None or not mode.strip():
        return False
    normalized = mode.strip().lower()
    if normalized != "check":
        raise StartupMigrationError(f"{STARTUP_MODE_ENV} must be check")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or upgrade the gateway database")
    parser.add_argument("action", choices=("check", "migrate"), nargs="?", default="check")
    arguments = parser.parse_args(argv)
    config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)

    try:
        settings = load_settings(config_path)
    except Exception:
        configure_logging("INFO")
        logger.error(
            "database startup failed",
            extra={"fields": {"error_code": "runtime_configuration_invalid"}},
        )
        return 1

    configure_logging(settings.logging.level)
    try:
        run_database_startup(settings, migrate=arguments.action == "migrate")
    except Exception:
        logger.error(
            "database startup failed",
            extra={"fields": {"error_code": "database_migration_required"}},
        )
        return 1

    print(
        json.dumps(
            {"action": arguments.action, "status": "ok"},
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
