from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.gateway.routes import router
from cf_agent_gateway.logging import configure_logging
from cf_agent_gateway.runtime.health import DatabaseReadinessMonitor
from cf_agent_gateway.runtime.startup import (
    check_database_migrations,
    database_startup_check_enabled,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.logging.level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        engine = create_database_engine(settings.database.url)
        readiness_monitor: DatabaseReadinessMonitor | None = None
        try:
            if database_startup_check_enabled():
                check_database_migrations(engine)
            else:
                initialize_database(engine)
            app.state.database_engine = engine
            app.state.database_session_factory = create_database_session_factory(engine)
            readiness_monitor = DatabaseReadinessMonitor(engine)
            app.state.database_readiness = readiness_monitor
            readiness_monitor.start()
            app.state.ready = True
            logger.info(
                "gateway started",
                extra={"fields": {"host": settings.server.host, "port": settings.server.port}},
            )
            yield
        finally:
            app.state.ready = False
            if readiness_monitor is not None:
                readiness_monitor.stop()
            engine.dispose()
            logger.info("gateway stopped")

    app = FastAPI(
        title="CF_agent-gateway",
        description="Enterprise AI Message Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)
    return app
