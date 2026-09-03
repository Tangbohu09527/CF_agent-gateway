from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event
from types import FrameType
from typing import Literal

from cf_agent_gateway.adapters.wechat import PollResult, WechatPollingLifecycleState
from cf_agent_gateway.config import Settings, load_settings
from cf_agent_gateway.logging import configure_logging
from cf_agent_gateway.runtime.errors import (
    HermesAPIKeyEnvironmentError,
    HermesRuntimeError,
    WechatRuntimeDisabledError,
    WechatRuntimeError,
    WechatTokenContractError,
)
from cf_agent_gateway.runtime.heartbeat import (
    HeartbeatPublisher,
    create_worker_heartbeat_from_environment,
)
from cf_agent_gateway.runtime.startup import (
    database_startup_check_enabled,
    run_database_startup,
)
from cf_agent_gateway.runtime.wechat import run_wechat_poll_once

DEFAULT_CONFIG_PATH = "config/config.yaml"

logger = logging.getLogger(__name__)

PollOnce = Callable[[Settings], PollResult]
_FATAL_POLL_ERRORS = (
    HermesAPIKeyEnvironmentError,
    WechatRuntimeDisabledError,
    WechatTokenContractError,
)
_PollChatShape = tuple[
    str | None,
    bool,
    int,
    int,
    int,
    str | None,
    tuple[tuple[str, str], ...],
]
_PollHistoryShape = tuple[
    str | None,
    int,
    int,
    int,
    int,
    int,
    int,
    tuple[_PollChatShape, ...],
]


def run_worker(
    settings: Settings,
    *,
    stop_event: Event | None = None,
    poll_once: PollOnce | None = None,
    heartbeat: HeartbeatPublisher | None = None,
) -> None:
    """Run serialized WeChat polling cycles until shutdown is requested."""

    shutdown = stop_event if stop_event is not None else Event()
    lifecycle_state = WechatPollingLifecycleState() if poll_once is None else None
    interval = settings.runtime.polling_interval_seconds
    cycle_sequence = 0
    last_history_shape: _PollHistoryShape | None = None
    final_heartbeat_state: Literal["stopped", "failed"] = "stopped"

    try:
        if heartbeat is not None:
            heartbeat.start()
            heartbeat.update("running", phase="idle", cycle_sequence=cycle_sequence)
        logger.info(
            "worker started",
            extra={"fields": {"polling_interval_seconds": interval}},
        )
        while not shutdown.is_set():
            cycle_sequence += 1
            if heartbeat is not None:
                heartbeat.update(
                    "running",
                    phase="polling",
                    cycle_sequence=cycle_sequence,
                )
            logger.debug("poll cycle started")
            try:
                if poll_once is None:
                    assert lifecycle_state is not None
                    result = run_wechat_poll_once(
                        settings,
                        lifecycle_state=lifecycle_state,
                    )
                else:
                    result = poll_once(settings)
            except _FATAL_POLL_ERRORS:
                raise
            except Exception as error:
                last_history_shape = None
                logger.error(
                    "poll cycle failed",
                    extra={"fields": {"error_code": _safe_error_code(error)}},
                )
                if heartbeat is not None:
                    heartbeat.update(
                        "running",
                        phase="waiting",
                        cycle_sequence=cycle_sequence,
                        last_cycle_succeeded=False,
                        wechat_auth="unknown",
                    )
            else:
                cycle_succeeded = (
                    result.logged_in and result.chats_failed == 0 and not result.failures
                )
                level, last_history_shape = _poll_result_log_level(
                    result,
                    previous_history_shape=last_history_shape,
                )
                logger.log(
                    level,
                    "poll cycle completed",
                    extra={
                        "fields": {
                            "logged_in": result.logged_in,
                            "chats_seen": result.chats_seen,
                            "chats_succeeded": result.chats_succeeded,
                            "chats_failed": result.chats_failed,
                            "messages_seen": result.messages_seen,
                            "messages_processed": result.messages_processed,
                            "messages_new": result.messages_new,
                            "messages_duplicate": result.messages_duplicate,
                            "messages_skipped_checkpoint": (result.messages_skipped_by_checkpoint),
                            "messages_skipped_self": result.messages_skipped_as_self,
                            "messages_failed": result.messages_failed,
                            "messages_without_server_id": result.messages_without_server_id,
                            "bootstrapped_chats": result.bootstrapped_chats,
                            "failure_count": len(result.failures),
                        }
                    },
                )
                if heartbeat is not None:
                    heartbeat.update(
                        "running",
                        phase="waiting",
                        cycle_sequence=cycle_sequence,
                        last_cycle_succeeded=cycle_succeeded,
                        wechat_auth="logged_in" if result.logged_in else "logged_out",
                    )

            if heartbeat is None:
                shutdown_requested = shutdown.wait(interval)
            else:
                shutdown_requested = heartbeat.wait(shutdown, interval)
            if shutdown_requested:
                break
    except BaseException:
        final_heartbeat_state = "failed"
        raise
    finally:
        if heartbeat is not None:
            heartbeat.stop(final_heartbeat_state)
        logger.info("worker stopped")


def main() -> int:
    stop_event: Event | None = None
    heartbeat: HeartbeatPublisher | None = None
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
        try:
            if database_startup_check_enabled():
                run_database_startup(settings)
        except Exception:
            logger.error(
                "worker failed",
                extra={"fields": {"error_code": "database_migration_required"}},
            )
            return 1

        heartbeat = create_worker_heartbeat_from_environment(
            error_handler=_log_heartbeat_failure,
        )
        stop_event = Event()
        with _shutdown_signal_handlers(stop_event):
            if heartbeat is None:
                run_worker(settings, stop_event=stop_event)
            else:
                run_worker(
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


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, (HermesRuntimeError, WechatRuntimeError)):
        return error.code
    return "poll_cycle_failed"


def _poll_result_log_level(
    result: PollResult,
    *,
    previous_history_shape: _PollHistoryShape | None,
) -> tuple[int, _PollHistoryShape | None]:
    if _poll_result_has_immediate_activity(result):
        return logging.INFO, None
    history_shape = _poll_history_shape(result)
    if history_shape is None:
        level = logging.INFO if previous_history_shape is not None else logging.DEBUG
        return level, None
    if any(
        chat_result.continuity_only and chat_result.continuity_state_changed
        for chat_result in result.chat_results
    ):
        return logging.INFO, history_shape
    level = logging.INFO if history_shape != previous_history_shape else logging.DEBUG
    return level, history_shape


def _poll_result_has_immediate_activity(result: PollResult) -> bool:
    return (
        not result.logged_in
        or _poll_result_has_ordinary_failure(result)
        or result.bootstrapped_chats > 0
        or any(
            count > 0
            for count in (
                result.messages_processed,
                result.messages_new,
                result.messages_duplicate,
                result.messages_skipped_as_self,
                result.messages_failed,
            )
        )
    )


def _poll_result_has_ordinary_failure(result: PollResult) -> bool:
    continuity_results = [
        chat_result for chat_result in result.chat_results if chat_result.continuity_only
    ]
    continuity_failure_count = sum(len(chat_result.failures) for chat_result in continuity_results)
    return (
        any(
            not chat_result.succeeded and not chat_result.continuity_only
            for chat_result in result.chat_results
        )
        or result.chats_failed > len(continuity_results)
        or len(result.failures) > continuity_failure_count
    )


def _poll_history_shape(result: PollResult) -> _PollHistoryShape | None:
    if result.messages_seen <= 0 and not any(
        chat_result.continuity_only for chat_result in result.chat_results
    ):
        return None
    return (
        result.source_account_id,
        result.chats_seen,
        result.chats_succeeded,
        result.chats_failed,
        result.messages_seen,
        result.messages_skipped_by_checkpoint,
        result.messages_without_server_id,
        tuple(
            (
                chat_result.conversation_id,
                chat_result.succeeded,
                chat_result.messages_seen,
                chat_result.messages_skipped_by_checkpoint,
                chat_result.messages_without_server_id,
                chat_result.continuity_state_ref,
                tuple((failure.stage.value, failure.code) for failure in chat_result.failures),
            )
            for chat_result in result.chat_results
        ),
    )


def _log_worker_failure(error: Exception) -> None:
    logger.error(
        "worker failed",
        extra={"fields": {"error_code": _safe_error_code(error)}},
    )


def _log_heartbeat_failure() -> None:
    logger.error(
        "worker heartbeat write failed",
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
