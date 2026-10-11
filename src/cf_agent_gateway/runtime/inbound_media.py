"""Media intake lifecycle owned by the polling process, independent of AI slots."""

import logging
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

from cf_agent_gateway.adapters.wechat.inbound_media_http import InboundMediaHTTPClient
from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.inbound.worker import InboundMediaWorker
from cf_agent_gateway.runtime.startup import (
    check_database_migrations,
    database_startup_check_enabled,
)
from cf_agent_gateway.runtime.wechat_token import resolve_wechat_token

logger = logging.getLogger(__name__)


@contextmanager
def media_intake_runtime(settings, *, enabled=True):
    if not enabled or not settings.inbound_media.enabled:
        yield
        return
    if not settings.wechat.enabled:
        raise ValueError("inbound media requires WeChat polling")
    staging = InboundMediaStaging(Path(settings.inbound_media.staging_root))
    token = resolve_wechat_token(settings.wechat.token_env)
    engine = create_database_engine(settings.database.url)
    stop = Event()
    thread = None
    try:
        if database_startup_check_enabled():
            check_database_migrations(engine)
        else:
            initialize_database(engine)
        worker = InboundMediaWorker(
            create_database_session_factory(engine),
            InboundMediaHTTPClient(settings.wechat.base_url, token),
            staging,
        )

        def run():
            while not stop.is_set():
                try:
                    worker.run_once()
                except Exception:
                    logger.error(
                        "inbound media cycle failed",
                        extra={
                            "fields": {
                                "error_code": "media_cycle_failed",
                            }
                        },
                    )
                stop.wait(1)

        thread = Thread(target=run, name="inbound-media", daemon=False)
        thread.start()
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        engine.dispose()
