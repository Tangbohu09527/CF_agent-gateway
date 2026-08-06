from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, engine_from_config, make_url, pool

from cf_agent_gateway.database import Base, load_database_models

config = context.config

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
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        run_migrations(existing_connection)
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
