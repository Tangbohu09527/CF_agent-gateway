from __future__ import annotations

from logging.config import fileConfig
from os import environ
from pathlib import Path

from alembic import context
from sqlalchemy import Connection, engine_from_config, make_url, pool

from cf_agent_gateway.database import Base, load_database_models

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if database_url := environ.get("CF_AGENT_GATEWAY_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

load_database_models()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    database_url = config.attributes.get("database_url")
    if not isinstance(database_url, str):
        database_url = config.get_main_option("sqlalchemy.url")
    if not database_url:
        raise RuntimeError("a database URL is required for offline migrations")

    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=make_url(database_url).get_backend_name() == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations(connection: Connection) -> None:
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        _set_sqlite_foreign_keys(connection, enabled=False)
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=is_sqlite,
        )

        with context.begin_transaction():
            context.run_migrations()
    finally:
        if is_sqlite:
            _set_sqlite_foreign_keys(connection, enabled=True)


def run_migrations_online() -> None:
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        run_migrations(existing_connection)
        return

    database_url = make_url(config.get_main_option("sqlalchemy.url"))
    if database_url.get_backend_name() == "sqlite" and database_url.database not in {
        None,
        "",
        ":memory:",
    }:
        Path(database_url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        run_migrations(connection)


def _set_sqlite_foreign_keys(connection: Connection, *, enabled: bool) -> None:
    cursor = connection.connection.cursor()
    try:
        cursor.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
    finally:
        cursor.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
