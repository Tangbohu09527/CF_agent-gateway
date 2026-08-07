from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import partial
from threading import Event
from types import FrameType
from typing import Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.adapters.wechat import WechatHttpMessageSender
from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import HermesChatClient, HermesClient, HermesDispatchService
from cf_agent_gateway.hermes.worker import HermesDispatchWorker
from cf_agent_gateway.logging import configure_logging
from cf_agent_gateway.runtime.delivery import AccountScopedHermesResponseProcessor
from cf_agent_gateway.runtime.errors import (
    DispatchWorkerDisabledError,
    DispatchWorkerRuntimeError,
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
    HermesRuntimeDisabledError,
)
from cf_agent_gateway.runtime.wechat import (
    ClosableHermesChatClient,
    WechatMessageSenderFactory,
)

DEFAULT_CONFIG_PATH = "config/config.yaml"

logger = logging.getLogger(__name__)


class HermesClientFactory(Protocol):
    def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
    ) -> ClosableHermesChatClient: ...


def build_dispatch_worker(
    settings: Settings,
    *,
    session_factory: sessionmaker[Session],
    hermes_client: HermesChatClient,
    sender_factory: WechatMessageSenderFactory,
) -> HermesDispatchWorker:
    """Build the public worker core around injected runtime dependencies."""

    return HermesDispatchWorker(
        session_factory,
        lambda session: HermesDispatchService(session, hermes_client),
        lease_seconds=settings.worker.lease_seconds,
        retry_limit=settings.worker.retry_limit,
        response_processor_factory=lambda session: AccountScopedHermesResponseProcessor(
            session,
            sender_factory,
        ),
    )


def run_dispatch_worker(
    settings: Settings,
    *,
    stop_event: Event,
    hermes_client_factory: HermesClientFactory = HermesClient,
    sender_factory: WechatMessageSenderFactory | None = None,
    engine_factory: Callable[[str], Engine] = create_database_engine,
    environment_reader: Callable[[str], str | None] = os.getenv,
) -> None:
    """Run the independent durable Hermes dispatch worker process."""

    if not settings.worker.enabled:
        raise DispatchWorkerDisabledError()
    if not settings.hermes.enabled:
        raise HermesRuntimeDisabledError()

    api_key = environment_reader(settings.hermes.api_key_env)
    if api_key is None or not api_key.strip():
        raise HermesAPIKeyEnvironmentError(settings.hermes.api_key_env)

    engine: Engine | None = None
    hermes_client: ClosableHermesChatClient | None = None
    try:
        client_initialization_failed = False
        try:
            hermes_client = hermes_client_factory(
                base_url=settings.hermes.base_url,
                api_key=api_key,
                model=settings.hermes.model,
            )
        except Exception:
            client_initialization_failed = True
        if client_initialization_failed:
            raise HermesClientInitializationError()

        engine = engine_factory(settings.database.url)
        initialize_database(engine)
        session_factory = create_database_session_factory(engine)
        resolved_sender_factory = sender_factory
        if resolved_sender_factory is None:
            resolved_sender_factory = partial(
                WechatHttpMessageSender,
                base_url=settings.wechat.base_url,
                token_env=settings.wechat.token_env,
                environment_reader=environment_reader,
            )
        worker = build_dispatch_worker(
            settings,
            session_factory=session_factory,
            hermes_client=hermes_client,
            sender_factory=resolved_sender_factory,
        )
        logger.info(
            "dispatch worker started",
            extra={
                "fields": {
                    "concurrency": settings.worker.concurrency,
                    "lease_seconds": settings.worker.lease_seconds,
                    "retry_limit": settings.worker.retry_limit,
                }
            },
        )
        try:
            worker.run(
                stop_event=stop_event,
                concurrency=settings.worker.concurrency,
            )
        finally:
            logger.info("dispatch worker stopped")
    finally:
        if hermes_client is not None:
            with suppress(Exception):
                hermes_client.close()
        if engine is not None:
            engine.dispose()


def main() -> int:
    stop_event: Event | None = None
    try:
        config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
        try:
            settings = load_settings(config_path)
        except Exception:
            configure_logging("INFO")
            logger.error(
                "dispatch worker failed",
                extra={"fields": {"error_code": "runtime_configuration_invalid"}},
            )
            return 1

        configure_logging(settings.logging.level)
        stop_event = Event()
        with _shutdown_signal_handlers(stop_event):
            run_dispatch_worker(settings, stop_event=stop_event)
    except KeyboardInterrupt:
        if stop_event is not None:
            stop_event.set()
    except DispatchWorkerDisabledError as error:
        _log_worker_failure(error)
        return 2
    except Exception as error:
        _log_worker_failure(error)
        return 1
    return 0


def _log_worker_failure(error: Exception) -> None:
    error_code = (
        error.code
        if isinstance(
            error,
            (
                DispatchWorkerRuntimeError,
                HermesAPIKeyEnvironmentError,
                HermesClientInitializationError,
            ),
        )
        else "dispatch_worker_failed"
    )
    logger.error(
        "dispatch worker failed",
        extra={"fields": {"error_code": error_code}},
    )


@contextmanager
def _shutdown_signal_handlers(stop_event: Event) -> Iterator[None]:
    handled_signals = (signal.SIGINT, signal.SIGTERM)
    previous_handlers: dict[signal.Signals, signal.Handlers] = {}

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop_event.set()

    try:
        for shutdown_signal in handled_signals:
            previous_handlers[shutdown_signal] = signal.signal(
                shutdown_signal,
                request_shutdown,
            )
        yield
    finally:
        for shutdown_signal, previous_handler in previous_handlers.items():
            signal.signal(shutdown_signal, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
