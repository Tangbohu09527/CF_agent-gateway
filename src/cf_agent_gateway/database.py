from __future__ import annotations

from collections.abc import Iterator
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import (
    CheckConstraint,
    Engine,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_CONNECT_TIMEOUT_SECONDS = 2.0


class Base(DeclarativeBase):
    pass


class DatabaseSchemaError(RuntimeError):
    """The existing database schema cannot safely run the current gateway."""


def create_database_engine(url: str) -> Engine:
    options: dict[str, object] = {"pool_pre_ping": True}
    database_url = make_url(url)
    backend_name = database_url.get_backend_name()
    if backend_name == "sqlite":
        options["connect_args"] = {
            "check_same_thread": False,
            "timeout": 30.0,
        }
        file_database = database_url.database not in {None, "", ":memory:"}
        if file_database:
            options["pool_timeout"] = DATABASE_CONNECT_TIMEOUT_SECONDS
        else:
            options["poolclass"] = StaticPool
    else:
        options["pool_timeout"] = DATABASE_CONNECT_TIMEOUT_SECONDS
        if backend_name == "postgresql":
            options["connect_args"] = {
                "connect_timeout": max(1, int(DATABASE_CONNECT_TIMEOUT_SECONDS)),
            }
    engine = create_engine(url, **options)
    if backend_name == "sqlite":
        event.listen(
            engine,
            "connect",
            partial(_configure_sqlite_connection, file_database=file_database),
        )
    return engine


def create_database_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_database(engine: Engine) -> None:
    if engine.dialect.name == "sqlite" and engine.url.database not in {None, "", ":memory:"}:
        Path(engine.url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)

    for model_module in (
        "cf_agent_gateway.message.models",
        "cf_agent_gateway.identity.models",
        "cf_agent_gateway.workspace.models",
        "cf_agent_gateway.access.policy_models",
        "cf_agent_gateway.adapters.wechat.polling_models",
        "cf_agent_gateway.hermes.models",
        "cf_agent_gateway.runtime.models",
    ):
        import_module(model_module)
    Base.metadata.create_all(engine)
    _validate_conversation_binding_constraints(engine)


def get_database_session(request: Request) -> Iterator[Session]:
    session_factory: sessionmaker[Session] = request.app.state.database_session_factory
    with session_factory() as session:
        yield session


def _validate_conversation_binding_constraints(engine: Engine) -> None:
    required_unique_keys = {
        "messages": (
            frozenset({"event_id"}),
            frozenset(
                {
                    "source",
                    "source_account_id",
                    "conversation_id",
                    "source_message_id",
                }
            ),
        ),
        "ai_threads": (frozenset({"thread_key"}),),
        "thread_source_bindings": (
            frozenset({"platform", "account_id", "physical_conversation_id"}),
        ),
        "wechat_sync_checkpoints": (frozenset({"source_account_id", "conversation_id"}),),
        "hermes_dispatch_records": (frozenset({"message_id"}),),
        "hermes_delivery_records": (frozenset({"message_id"}),),
    }
    inspector = inspect(engine)
    for table_name, required_keys in required_unique_keys.items():
        unique_keys = {
            frozenset(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table_name)
        }
        if not all(required_key in unique_keys for required_key in required_keys):
            raise DatabaseSchemaError(
                "database schema predates required idempotency or conversation-scoped binding; "
                "migrate or recreate the database"
            )

    checkpoint_columns = {
        column["name"]: column for column in inspector.get_columns("wechat_sync_checkpoints")
    }
    if "regression_generation" not in checkpoint_columns:
        raise DatabaseSchemaError(
            "database schema predates checkpoint regression generation; "
            "migrate or recreate the database"
        )
    if "last_message_fingerprint" not in checkpoint_columns:
        raise DatabaseSchemaError(
            "database schema predates checkpoint message anchors; migrate or recreate the database"
        )
    if checkpoint_columns["regression_generation"].get("nullable", True):
        raise DatabaseSchemaError(
            "database checkpoint regression generation is nullable; "
            "backfill and constrain it before startup"
        )
    _validate_required_schema_shape(inspector)

    with engine.connect() as connection:
        unsafe_unanchored_rows = connection.scalar(
            text(
                "SELECT COUNT(*) FROM wechat_sync_checkpoints "
                "WHERE last_local_id > 0 "
                "AND (regression_generation IS NULL OR "
                "(regression_generation = 0 AND last_message_fingerprint IS NULL))"
            )
        )
    if unsafe_unanchored_rows:
        raise DatabaseSchemaError(
            "database has unanchored legacy checkpoints; "
            "backfill anchors or perform a controlled replay migration"
        )


def _validate_required_schema_shape(inspector: Any) -> None:
    for table in Base.metadata.sorted_tables:
        actual_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        missing_columns = set(table.columns.keys()) - set(actual_columns)
        if missing_columns:
            raise DatabaseSchemaError(
                f"database table {table.name} is missing required columns; "
                "migrate or recreate the database"
            )

        nullable_mismatches = {
            column.name
            for column in table.columns
            if not column.nullable and actual_columns[column.name].get("nullable", True)
        }
        if nullable_mismatches:
            raise DatabaseSchemaError(
                f"database table {table.name} has nullable required columns; "
                "backfill and constrain them before startup"
            )

        expected_primary_key = frozenset(column.name for column in table.primary_key.columns)
        actual_primary_key = frozenset(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        )
        if actual_primary_key != expected_primary_key:
            raise DatabaseSchemaError(
                f"database table {table.name} has an incompatible primary key; "
                "migrate or recreate the database"
            )

        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name is not None
        }
        actual_checks = {
            constraint.get("name") for constraint in inspector.get_check_constraints(table.name)
        }
        if not expected_checks.issubset(actual_checks):
            raise DatabaseSchemaError(
                f"database table {table.name} is missing required check constraints; "
                "migrate or recreate the database"
            )

        expected_unique_keys = {
            frozenset(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique_keys = {
            frozenset(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table.name)
        }
        if not expected_unique_keys.issubset(actual_unique_keys):
            raise DatabaseSchemaError(
                f"database table {table.name} is missing required unique constraints; "
                "migrate or recreate the database"
            )

        expected_foreign_keys = {
            (
                tuple(constraint.column_keys),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        actual_foreign_keys = {
            (
                tuple(constraint.get("constrained_columns") or ()),
                constraint.get("referred_table"),
                tuple(constraint.get("referred_columns") or ()),
            )
            for constraint in inspector.get_foreign_keys(table.name)
        }
        if not expected_foreign_keys.issubset(actual_foreign_keys):
            raise DatabaseSchemaError(
                f"database table {table.name} is missing required foreign keys; "
                "migrate or recreate the database"
            )

        expected_indexes = {index.name for index in table.indexes if index.name is not None}
        actual_indexes = {index.get("name") for index in inspector.get_indexes(table.name)}
        if not expected_indexes.issubset(actual_indexes):
            raise DatabaseSchemaError(
                f"database table {table.name} is missing required indexes; "
                "migrate or recreate the database"
            )


def _configure_sqlite_connection(
    dbapi_connection: Any,
    connection_record: object,
    *,
    file_database: bool,
) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        if file_database:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()
