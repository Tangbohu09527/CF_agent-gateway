from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event
from types import FrameType

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.logging import configure_logging
from cf_agent_gateway.runtime.errors import (
    HermesAPIKeyEnvironmentError,
    HermesRuntimeError,
    WechatRuntimeDisabledError,
    WechatRuntimeError,
    WechatTokenEnvironmentError,
)
from cf_agent_gateway.runtime.wechat import run_wechat_poll_once

DEFAULT_CONFIG_PATH = "config/config.yaml"

logger = logging.getLogger(__name__)

PollOnce = Callable[[Settings], PollResult]
_FATAL_POLL_ERRORS = (
    HermesAPIKeyEnvironmentError,
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
)


def run_worker(
    settings: Settings,
    *,
    stop_event: Event | None = None,
    poll_once: PollOnce | None = None,
) -> None:
    """Run serialized WeChat polling cycles until shutdown is requested."""

    shutdown = stop_event if stop_event is not None else Event()
    execute_poll = poll_once if poll_once is not None else run_wechat_poll_once
    interval = settings.runtime.polling_interval_seconds

    logger.info(
        "worker started",
        extra={"fields": {"polling_interval_seconds": interval}},
    )
    try:
        while not shutdown.is_set():
            logger.info("poll cycle started")
            try:
                result = execute_poll(settings)
            except _FATAL_POLL_ERRORS:
                raise
            except Exception as error:
                logger.error(
                    "poll cycle failed",
                    extra={"fields": {"error_code": _safe_error_code(error)}},
                )
            else:
                logger.info(
                    "messages processed",
                    extra={
                        "fields": {
                            "logged_in": result.logged_in,
                            "chats_seen": result.chats_seen,
                            "chats_failed": result.chats_failed,
                            "messages_seen": result.messages_seen,
                            "messages_processed": result.messages_processed,
                        }
                    },
                )

            if shutdown.wait(interval):
                break
    finally:
        logger.info("worker stopped")


def main() -> int:
    stop_event: Event | None = None
    try:
        config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
        try:
            settings = load_settings(config_path)
        except Exception:
            configure_logging("INFO")
            logger.error(
                "worker failed",
                extra={"fields": {"error_code": "runtime_configuration_invalid"}},
            )
            return 1

        configure_logging(settings.logging.level)
        stop_event = Event()
        with _shutdown_signal_handlers(stop_event):
            run_worker(settings, stop_event=stop_event)
    except KeyboardInterrupt:
        if stop_event is not None:
            stop_event.set()
    except WechatRuntimeDisabledError as error:
        _log_worker_failure(error)
        return 2
    except Exception as error:
        _log_worker_failure(error)
        return 1
    return 0


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, (HermesRuntimeError, WechatRuntimeError)):
        return error.code
    return "poll_cycle_failed"


def _log_worker_failure(error: Exception) -> None:
    logger.error(
        "worker failed",
        extra={"fields": {"error_code": _safe_error_code(error)}},
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
