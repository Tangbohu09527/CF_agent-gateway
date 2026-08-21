from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat import (
    AgentWechatAuthStatus,
    NormalizedWechatMessage,
    PollFailureStage,
    PollResult,
    WechatPollingService,
    WechatSyncCheckpointStore,
)
from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.database import create_database_engine, initialize_database
from cf_agent_gateway.runtime.models import RuntimeWorkerStatus
from cf_agent_gateway.runtime.status import (
    DatabaseWorkerStatusReporter,
    WorkerLeaseLostError,
)
from cf_agent_gateway.runtime.worker import run_worker

ACCOUNT_ID = "wxid_gateway"
CONVERSATION_ID = "wxid_restart_test"
MESSAGE_LOCAL_ID = 42


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ReplayWechatClient:
    def get_auth_status(self) -> AgentWechatAuthStatus:
        return AgentWechatAuthStatus(
            status="logged_in",
            loggedInUser=ACCOUNT_ID,
        )

    def list_chats(self) -> list[dict[str, Any]]:
        return [{"id": CONVERSATION_ID}]

    def list_messages(self, chat_id: str) -> list[dict[str, Any]]:
        assert chat_id == CONVERSATION_ID
        return [
            {
                "localId": MESSAGE_LOCAL_ID,
                "serverId": MESSAGE_LOCAL_ID,
                "chatId": CONVERSATION_ID,
                "sender": "wxid_sender",
                "senderName": "Sender",
                "type": 1,
                "content": "survives worker restart",
                "timestamp": "2026-08-21T10:00:00+08:00",
            }
        ]


class RecordingSink:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.attempts: list[NormalizedWechatMessage] = []

    def handle(self, message: NormalizedWechatMessage) -> None:
        self.attempts.append(message)
        if self.fail:
            raise RuntimeError("controlled sink failure before restart")


def status_reporter(
    database_url: str,
    *,
    clock: MutableClock | None = None,
    heartbeat_stale_after_seconds: float = 7201,
) -> DatabaseWorkerStatusReporter:
    return DatabaseWorkerStatusReporter(
        database_url,
        hermes_enabled=False,
        heartbeat_interval_seconds=3600,
        heartbeat_stale_after_seconds=heartbeat_stale_after_seconds,
        clock=clock,
    )


def run_one_cycle(
    settings: Settings,
    service: WechatPollingService,
    reporter: DatabaseWorkerStatusReporter,
) -> PollResult:
    stop_event = Event()
    results: list[PollResult] = []

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        try:
            result = service.poll_once()
            results.append(result)
            return result
        finally:
            stop_event.set()

    run_worker(
        settings,
        stop_event=stop_event,
        poll_once=poll_once,
        status_reporter=reporter,
    )
    assert len(results) == 1
    return results[0]


def test_worker_restart_replays_uncheckpointed_message_and_takes_over_lease(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-restart.db'}"
    settings = Settings(database=DatabaseSettings(url=database_url))
    client = ReplayWechatClient()

    first_engine = create_database_engine(database_url)
    initialize_database(first_engine)
    first_session = Session(first_engine, expire_on_commit=False)
    first_reporter = status_reporter(database_url)
    first_sink = RecordingSink(fail=True)
    try:
        first_store = WechatSyncCheckpointStore(first_session)
        first_result = run_one_cycle(
            settings,
            WechatPollingService(
                client,
                first_store,
                first_sink,
                bootstrap_mode="backfill",
            ),
            first_reporter,
        )

        assert first_result.messages_processed == 0
        assert first_result.failures[0].stage is PollFailureStage.SINK
        first_checkpoint = first_store.get(
            source_account_id=ACCOUNT_ID,
            conversation_id=CONVERSATION_ID,
        )
        assert first_checkpoint is not None
        assert first_checkpoint.last_local_id == 0
        assert len(first_sink.attempts) == 1
        first_source_message_id = first_sink.attempts[0].source_message_id
        first_instance_id = first_reporter.instance_id
    finally:
        first_session.close()
        first_engine.dispose()

    second_engine = create_database_engine(database_url)
    initialize_database(second_engine)
    second_session = Session(second_engine, expire_on_commit=False)
    second_reporter = status_reporter(database_url)
    second_sink = RecordingSink(fail=False)
    try:
        second_store = WechatSyncCheckpointStore(second_session)
        second_result = run_one_cycle(
            settings,
            WechatPollingService(
                client,
                second_store,
                second_sink,
                bootstrap_mode="backfill",
            ),
            second_reporter,
        )

        assert second_result.failures == []
        assert second_result.messages_processed == 1
        assert second_result.bootstrapped_chats == 0
        assert [message.source_message_id for message in second_sink.attempts] == [
            first_source_message_id
        ]

        second_checkpoint = second_store.get(
            source_account_id=ACCOUNT_ID,
            conversation_id=CONVERSATION_ID,
        )
        assert second_checkpoint is not None
        assert second_checkpoint.last_local_id == MESSAGE_LOCAL_ID

        second_session.expire_all()
        worker_status = second_session.scalar(
            select(RuntimeWorkerStatus).where(RuntimeWorkerStatus.worker_name == "wechat")
        )
        assert worker_status is not None
        assert worker_status.instance_id == second_reporter.instance_id
        assert worker_status.instance_id != first_instance_id
        assert worker_status.state == "stopped"
    finally:
        second_session.close()
        second_engine.dispose()


def test_worker_restart_recovers_stale_lease_and_processes_uncheckpointed_message(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'worker-stale-restart.db'}"
    settings = Settings(database=DatabaseSettings(url=database_url))
    client = ReplayWechatClient()
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))

    first_engine = create_database_engine(database_url)
    initialize_database(first_engine)
    first_session = Session(first_engine, expire_on_commit=False)
    first_reporter = status_reporter(
        database_url,
        clock=clock,
        heartbeat_stale_after_seconds=30,
    )
    first_reporter.start()
    try:
        first_reporter.cycle_started()
        first_store = WechatSyncCheckpointStore(first_session)
        first_sink = RecordingSink(fail=True)
        first_result = WechatPollingService(
            client,
            first_store,
            first_sink,
            bootstrap_mode="backfill",
        ).poll_once()

        assert first_result.messages_processed == 0
        assert first_result.failures[0].stage is PollFailureStage.SINK
        first_checkpoint = first_store.get(
            source_account_id=ACCOUNT_ID,
            conversation_id=CONVERSATION_ID,
        )
        assert first_checkpoint is not None
        assert first_checkpoint.last_local_id == 0
        first_source_message_id = first_sink.attempts[0].source_message_id
        first_instance_id = first_reporter.instance_id

        first_session.expire_all()
        stale_status = first_session.scalar(
            select(RuntimeWorkerStatus).where(RuntimeWorkerStatus.worker_name == "wechat")
        )
        assert stale_status is not None
        assert stale_status.instance_id == first_instance_id
        assert stale_status.state == "polling"

        # Simulate an unclean process exit: the first reporter is not stopped and its
        # heartbeat remains at the original time until the lease becomes stale.
        clock.value += timedelta(seconds=31)

        second_engine = create_database_engine(database_url)
        initialize_database(second_engine)
        second_session = Session(second_engine, expire_on_commit=False)
        second_reporter = status_reporter(
            database_url,
            clock=clock,
            heartbeat_stale_after_seconds=30,
        )
        second_sink = RecordingSink(fail=False)
        try:
            second_store = WechatSyncCheckpointStore(second_session)
            second_result = run_one_cycle(
                settings,
                WechatPollingService(
                    client,
                    second_store,
                    second_sink,
                    bootstrap_mode="backfill",
                ),
                second_reporter,
            )

            assert second_result.failures == []
            assert second_result.messages_processed == 1
            assert [message.source_message_id for message in second_sink.attempts] == [
                first_source_message_id
            ]
            second_checkpoint = second_store.get(
                source_account_id=ACCOUNT_ID,
                conversation_id=CONVERSATION_ID,
            )
            assert second_checkpoint is not None
            assert second_checkpoint.last_local_id == MESSAGE_LOCAL_ID

            second_session.expire_all()
            recovered_status = second_session.scalar(
                select(RuntimeWorkerStatus).where(RuntimeWorkerStatus.worker_name == "wechat")
            )
            assert recovered_status is not None
            assert recovered_status.instance_id == second_reporter.instance_id
            assert recovered_status.instance_id != first_instance_id
            assert recovered_status.state == "stopped"
            with pytest.raises(WorkerLeaseLostError):
                first_reporter.ensure_active()
        finally:
            second_session.close()
            second_engine.dispose()
    finally:
        first_reporter.stop()
        first_session.close()
        first_engine.dispose()
