from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from os import environ
from os import name as os_name
from pathlib import Path
from threading import Lock
from time import sleep

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Request
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

_MODEL_MODULES = (
    "cf_agent_gateway.message.models",
    "cf_agent_gateway.agent_profile.models",
    "cf_agent_gateway.identity.models",
    "cf_agent_gateway.workspace.models",
    "cf_agent_gateway.access.policy_models",
    "cf_agent_gateway.adapters.wechat.polling_models",
    "cf_agent_gateway.task.model.models",
    "cf_agent_gateway.artifact.models",
    "cf_agent_gateway.hermes.result_models",
    "cf_agent_gateway.response.models",
    "cf_agent_gateway.delivery.models",
)


class Base(DeclarativeBase):
    pass


class DatabaseSchemaError(RuntimeError):
    """The existing database schema cannot safely run the current gateway."""


_POSTGRES_MIGRATION_LOCK_ID = int.from_bytes(b"CFAGMIGR", byteorder="big", signed=True)
_SQLITE_MIGRATION_THREAD_LOCK = Lock()
_PACKAGED_SCRIPT_LOCATION = "cf_agent_gateway:migrations"
_EXPECTED_MIGRATION_HEAD = "20260807_03"


def create_database_engine(url: str) -> Engine:
    options: dict[str, object] = {"pool_pre_ping": True}
    database_url = make_url(url)
    if database_url.get_backend_name() == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
        if database_url.database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if database_url.get_backend_name() == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_database_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def ensure_database_directory(engine: Engine) -> None:
    if engine.dialect.name == "sqlite" and engine.url.database not in {None, "", ":memory:"}:
        Path(engine.url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def load_database_models() -> None:
    for model_module in _MODEL_MODULES:
        import_module(model_module)


def load_model_metadata() -> None:
    load_database_models()


def check_database_migrations(engine: Engine) -> None:
    """Verify without mutation that the database matches this build's migration head."""

    load_database_models()
    config = _create_alembic_config()
    _validate_migration_tree(config)
    with engine.connect() as connection:
        current_heads = set(MigrationContext.configure(connection).get_current_heads())
    if current_heads != {_EXPECTED_MIGRATION_HEAD}:
        raise DatabaseSchemaError("database migration is not at the required head")
    _validate_conversation_binding_constraints(engine)


def initialize_database(engine: Engine) -> None:
    ensure_database_directory(engine)
    load_database_models()
    config = _create_alembic_config()
    _validate_migration_tree(config)
    with _locked_migration_connection(engine) as connection:
        existing_tables = set(inspect(connection).get_table_names())
        if existing_tables and "alembic_version" not in existing_tables:
            if {"ai_threads", "thread_source_bindings"}.issubset(existing_tables):
                _validate_conversation_binding_constraints(connection)
            raise DatabaseSchemaError(
                "database schema is not versioned; back it up, stamp the main-schema "
                "baseline, and migrate to head"
            )

        if connection.in_transaction():
            connection.rollback()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    check_database_migrations(engine)


def _create_alembic_config() -> Config:
    configured_path = environ.get("CF_GATEWAY_ALEMBIC_CONFIG") or environ.get(
        "CF_AGENT_GATEWAY_ALEMBIC_CONFIG"
    )
    if configured_path:
        config_path = Path(configured_path).expanduser().resolve()
        if not config_path.is_file():
            raise DatabaseSchemaError(f"Alembic configuration not found: {config_path}")
        config = Config(config_path)
    else:
        config = Config()
        config.set_main_option("script_location", _PACKAGED_SCRIPT_LOCATION)
    config.attributes["configure_logger"] = False
    return config


def _validate_migration_tree(config: Config) -> None:
    if set(ScriptDirectory.from_config(config).get_heads()) != {_EXPECTED_MIGRATION_HEAD}:
        location = config.config_file_name or _PACKAGED_SCRIPT_LOCATION
        raise DatabaseSchemaError(f"unexpected Alembic migration tree configured by {location}")


@contextmanager
def _locked_migration_connection(engine: Engine) -> Iterator[Connection]:
    backend = engine.url.get_backend_name()
    if backend == "postgresql":
        with engine.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": _POSTGRES_MIGRATION_LOCK_ID},
            )
            connection.commit()
            try:
                yield connection
            except BaseException:
                if connection.in_transaction():
                    connection.rollback()
                raise
            else:
                if connection.in_transaction():
                    connection.commit()
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _POSTGRES_MIGRATION_LOCK_ID},
                )
                connection.commit()
        return

    if backend == "sqlite":
        with _SQLITE_MIGRATION_THREAD_LOCK:
            database_name = engine.url.database
            if database_name not in {None, "", ":memory:"}:
                lock_path = Path(f"{Path(database_name).expanduser().resolve()}.migration.lock")
                with _sqlite_migration_file_lock(lock_path), engine.connect() as connection:
                    yield connection
                return
            with engine.connect() as connection:
                yield connection
        return

    with engine.connect() as connection:
        yield connection


@contextmanager
def _sqlite_migration_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _lock_file(lock_file: object) -> None:
    lock_file.seek(0)  # type: ignore[attr-defined]
    if os_name == "nt":
        import msvcrt

        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return
            except OSError:
                sleep(0.05)

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]


def _unlock_file(lock_file: object) -> None:
    lock_file.seek(0)  # type: ignore[attr-defined]
    if os_name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def get_database_session(request: Request) -> Iterator[Session]:
    session_factory: sessionmaker[Session] = request.app.state.database_session_factory
    with session_factory() as session:
        yield session


def _validate_conversation_binding_constraints(engine: Engine | Connection) -> None:
    required_unique_keys = {
        "ai_threads": frozenset({"thread_key"}),
        "thread_source_bindings": frozenset({"platform", "account_id", "physical_conversation_id"}),
    }
    inspector = inspect(engine)
    for table_name, required_columns in required_unique_keys.items():
        unique_keys = {
            frozenset(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table_name)
        }
        if required_columns not in unique_keys:
            raise DatabaseSchemaError(
                "database schema predates conversation-scoped thread binding; "
                "migrate or recreate the database"
            )
