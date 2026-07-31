from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import create_database_engine
from cf_agent_gateway.gateway.routes import router
from cf_agent_gateway.logging import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.logging.level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(settings.database.url)
        app.state.database_engine = engine
        logger.info(
            "gateway started",
            extra={"fields": {"host": settings.server.host, "port": settings.server.port}},
        )
        try:
            yield
        finally:
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
