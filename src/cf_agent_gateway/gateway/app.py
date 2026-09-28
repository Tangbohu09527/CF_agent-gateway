from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cf_agent_gateway.admin.routes import router as admin_router
from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.gateway.middleware import RequestBodyLimitMiddleware
from cf_agent_gateway.gateway.routes import router
from cf_agent_gateway.inbound.access import router as inbound_router
from cf_agent_gateway.inbound.host_binding import expire_bindings
from cf_agent_gateway.inbound.host_binding_routes import router as host_binding_router
from cf_agent_gateway.logging import configure_logging
from cf_agent_gateway.runtime.health import DatabaseReadinessMonitor, RuntimeHealthService
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
        cleanup_task = None
        try:
            if database_startup_check_enabled():
                check_database_migrations(engine)
            else:
                initialize_database(engine)
            app.state.database_engine = engine
            app.state.database_session_factory = create_database_session_factory(engine)
            if settings.host_binding.enabled:

                def cleanup():
                    with app.state.database_session_factory() as session:
                        expire_bindings(session)

                async def reap():
                    while True:
                        try:
                            await asyncio.to_thread(cleanup)
                        except Exception:
                            # No exception body/SQL/grant data reaches ordinary logs.
                            logger.warning("host binding cleanup unavailable")
                        await asyncio.sleep(1)

                cleanup_task = asyncio.create_task(reap())
            readiness_monitor = DatabaseReadinessMonitor(engine)
            app.state.database_readiness = readiness_monitor
            app.state.runtime_health = RuntimeHealthService(engine, settings)
            readiness_monitor.start()
            app.state.ready = True
            logger.info(
                "gateway started",
                extra={"fields": {"host": settings.server.host, "port": settings.server.port}},
            )
            yield
        finally:
            app.state.ready = False
            if cleanup_task is not None:
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup_task
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

    @app.exception_handler(RequestValidationError)
    async def sanitized_request_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del request
        details = [
            {key: value for key, value in item.items() if key in {"type", "loc", "msg"}}
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": details},
        )

    app.state.settings = settings
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=settings.api.max_request_body_bytes,
    )
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(inbound_router)
    app.include_router(host_binding_router)
    return app
