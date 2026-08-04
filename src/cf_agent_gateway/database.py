from __future__ import annotations

from collections.abc import Iterator
from importlib import import_module
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class DatabaseSchemaError(RuntimeError):
    """The existing database schema cannot safely run the current gateway."""


def create_database_engine(url: str) -> Engine:
    options: dict[str, object] = {"pool_pre_ping": True}
    database_url = make_url(url)
    if database_url.get_backend_name() == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
        if database_url.database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
    return create_engine(url, **options)


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
