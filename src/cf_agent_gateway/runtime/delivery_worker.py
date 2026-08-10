from __future__ import annotations

import logging
import math
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import TIMEOUT_MAX, Event
from types import FrameType

from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.delivery import DeliveryBatchResult
from cf_agent_gateway.logging import configure_logging
from cf_agent_gateway.runtime.delivery import run_wechat_delivery_once
from cf_agent_gateway.runtime.errors import WechatRuntimeDisabledError, WechatRuntimeError
from cf_agent_gateway.runtime.heartbeat import (
    HeartbeatPublisher,
    create_worker_heartbeat_from_environment,
    resident_heartbeat,
)

DEFAULT_CONFIG_PATH = "config/config.yaml"
DEFAULT_IDLE_POLL_SECONDS = 1.0

logger = logging.getLogger(__name__)

DeliverOnce = Callable[[Settings], DeliveryBatchResult]


def run_delivery_worker(
    settings: Settings,
    *,
    stop_event: Event | None = None,
    deliver_once: DeliverOnce | None = None,
    heartbeat: HeartbeatPublisher | None = None,
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
) -> None:
    """Continuously invoke the existing bounded delivery drain until shutdown."""

    if (
        isinstance(idle_poll_seconds, bool)
        or not isinstance(idle_poll_seconds, (int, float))
        or not math.isfinite(idle_poll_seconds)
        or idle_poll_seconds <= 0
        or idle_poll_seconds > TIMEOUT_MAX
    ):
        raise ValueError("idle_poll_seconds must be a finite positive number")

    shutdown = stop_event if stop_event is not None else Event()
    execute_delivery = deliver_once if deliver_once is not None else run_wechat_delivery_once
    try:
        with resident_heartbeat(heartbeat, phase="delivery"):
            logger.info(
                "delivery worker started",
                extra={"fields": {"idle_poll_seconds": idle_poll_seconds}},
            )
            while not shutdown.is_set():
                result = execute_delivery(settings)
                if result.processed:
                    logger.info(
                        "delivery batch processed",
                        extra={
                            "fields": {
                                "processed": result.processed,
                                "delivered": result.delivered,
                                "failed": result.failed,
                                "uncertain": result.uncertain,
                            }
                        },
                    )
                if shutdown.wait(idle_poll_seconds):
                    break
    finally:
        logger.info("delivery worker stopped")


def main() -> int:
    stop_event: Event | None = None
    try:
        config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
        try:
            settings = load_settings(config_path)
        except Exception:
            configure_logging("INFO")
            logger.error(
                "delivery worker failed",
                extra={"fields": {"error_code": "runtime_configuration_invalid"}},
            )
            return 1

        configure_logging(settings.logging.level)
        heartbeat = create_worker_heartbeat_from_environment(
            error_handler=_log_heartbeat_failure,
        )
        stop_event = Event()
        with _shutdown_signal_handlers(stop_event):
            run_delivery_worker(
                settings,
                stop_event=stop_event,
                heartbeat=heartbeat,
            )
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


def _log_worker_failure(error: Exception) -> None:
    error_code = error.code if isinstance(error, WechatRuntimeError) else "delivery_worker_failed"
    logger.error(
        "delivery worker failed",
        extra={"fields": {"error_code": error_code}},
    )


def _log_heartbeat_failure() -> None:
    logger.error(
        "delivery worker heartbeat write failed",
        extra={"fields": {"error_code": "worker_heartbeat_write_failed"}},
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
