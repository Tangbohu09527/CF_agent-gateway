from __future__ import annotations

from collections.abc import Iterator
from importlib import import_module
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


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
    ):
        import_module(model_module)
    Base.metadata.create_all(engine)


def get_database_session(request: Request) -> Iterator[Session]:
    session_factory: sessionmaker[Session] = request.app.state.database_session_factory
    with session_factory() as session:
        yield session
