from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from threading import Event
from types import FrameType

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.database import DatabaseSchemaError
from cf_agent_gateway.logging import configure_logging
from cf_agent_gateway.runtime.errors import (
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
    HermesRuntimeError,
    WechatClientInitializationError,
    WechatRuntimeDisabledError,
    WechatRuntimeError,
    WechatTokenEnvironmentError,
)
from cf_agent_gateway.runtime.status import (
    DatabaseWorkerStatusReporter,
    WorkerLeaseError,
    poll_result_error_code,
)
from cf_agent_gateway.runtime.wechat import run_wechat_poll_once

DEFAULT_CONFIG_PATH = "config/config.yaml"

logger = logging.getLogger(__name__)

PollOnce = Callable[[Settings], PollResult]
_FATAL_POLL_ERRORS = (
    DatabaseSchemaError,
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
    WechatClientInitializationError,
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
    WorkerLeaseError,
)


def run_worker(
    settings: Settings,
    *,
    stop_event: Event | None = None,
    poll_once: PollOnce | None = None,
    status_reporter: DatabaseWorkerStatusReporter | None = None,
) -> None:
    """Run serialized WeChat polling cycles until shutdown is requested."""

    if poll_once is None and not settings.wechat.enabled:
        raise WechatRuntimeDisabledError()

    shutdown = stop_event if stop_event is not None else Event()
    interval = settings.runtime.polling_interval_seconds
    reporter = status_reporter
    if reporter is None and poll_once is None:
        reporter = DatabaseWorkerStatusReporter(
            settings.database.url,
            hermes_enabled=settings.hermes.enabled,
            heartbeat_interval_seconds=settings.runtime.heartbeat_interval_seconds,
            heartbeat_stale_after_seconds=settings.runtime.heartbeat_stale_after_seconds,
        )
    execute_poll = (
        poll_once
        if poll_once is not None
        else partial(
            run_wechat_poll_once,
            lease_guard=reporter.ensure_active if reporter is not None else None,
        )
    )
    reporter_started = False
    consecutive_failures = 0

    try:
        if reporter is not None:
            reporter.start()
            reporter_started = True
        logger.info(
            "worker started",
            extra={"fields": {"polling_interval_seconds": interval}},
        )
        while not shutdown.is_set():
            if reporter is not None:
                reporter.ensure_active()
                reporter.cycle_started()
            logger.info("poll cycle started")
            retry_delay = interval
            try:
                result = execute_poll(settings)
            except _FATAL_POLL_ERRORS as error:
                if reporter is not None:
                    reporter.cycle_failed(_safe_error_code(error))
                raise
            except Exception as error:
                error_code = _safe_error_code(error)
                if reporter is not None:
                    reporter.cycle_failed(error_code)
                consecutive_failures += 1
                retry_delay = _retry_delay(
                    interval,
                    settings.runtime.polling_retry_max_seconds,
                    consecutive_failures,
                )
                logger.error(
                    "poll cycle failed",
                    extra={
                        "fields": {
                            "error_code": error_code,
                            "consecutive_failures": consecutive_failures,
                            "retry_delay_seconds": retry_delay,
                        }
                    },
                )
            else:
                result_error_code = poll_result_error_code(result)
                if reporter is not None:
                    reporter.cycle_succeeded(result)
                cycle_degraded = result_error_code is not None
                if cycle_degraded:
                    consecutive_failures += 1
                    retry_delay = _retry_delay(
                        interval,
                        settings.runtime.polling_retry_max_seconds,
                        consecutive_failures,
                    )
                else:
                    consecutive_failures = 0
                log_result = logger.warning if cycle_degraded else logger.info
                log_result(
                    "messages processed",
                    extra={
                        "fields": {
                            "logged_in": result.logged_in,
                            "chats_seen": result.chats_seen,
                            "chats_failed": result.chats_failed,
                            "messages_seen": result.messages_seen,
                            "messages_processed": result.messages_processed,
                            "cycle_degraded": cycle_degraded,
                            "consecutive_failures": consecutive_failures,
                            "retry_delay_seconds": retry_delay,
                            "failure_codes": (
                                [failure.code for failure in result.failures]
                                if result.failures
                                else ([result_error_code] if result_error_code else [])
                            ),
                        }
                    },
                )

            if shutdown.wait(retry_delay):
                break
    finally:
        if reporter_started and reporter is not None:
            try:
                reporter.stop()
            except Exception:
                logger.error(
                    "worker status cleanup failed",
                    extra={"fields": {"error_code": "worker_status_cleanup_failed"}},
                )
        logger.info("worker stopped")


def _retry_delay(interval: float, maximum: float, consecutive_failures: int) -> float:
    exponent = min(max(consecutive_failures - 1, 0), 62)
    return min(max(interval, maximum), interval * (2**exponent))


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
    if isinstance(error, DatabaseSchemaError):
        return "database_schema_invalid"
    if isinstance(error, (HermesRuntimeError, WechatRuntimeError, WorkerLeaseError)):
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
