from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import BigInteger, CheckConstraint, String, UniqueConstraint, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat import polling_service as polling_service_module
from cf_agent_gateway.adapters.wechat.normalized_models import (
    NormalizedWechatMessage,
    WechatMessageType,
    WechatSenderType,
)
from cf_agent_gateway.adapters.wechat.normalizer import (
    build_wechat_checkpoint_fingerprint,
    normalize_wechat_message,
)
from cf_agent_gateway.adapters.wechat.polling_errors import (
    InvalidBootstrapModeError,
    WechatCheckpointContinuityError,
    WechatCheckpointFingerprintError,
    WechatCheckpointGenerationError,
    WechatCheckpointStateConflictError,
    WechatCheckpointValueError,
)
from cf_agent_gateway.adapters.wechat.polling_models import (
    PollFailureStage,
    PollResult,
    WechatSyncCheckpoint,
)
from cf_agent_gateway.adapters.wechat.polling_service import (
    WechatPollingLifecycleState,
    WechatPollingService,
)
from cf_agent_gateway.adapters.wechat.polling_store import WechatSyncCheckpointStore
from cf_agent_gateway.adapters.wechat.raw_models import (
    AgentWechatAuthStatus,
    RawWechatMessage,
)
from cf_agent_gateway.admission.models import MessageAdmissionOutcome
from cf_agent_gateway.config import RuntimeSettings, Settings
from cf_agent_gateway.database import create_database_engine, initialize_database
from cf_agent_gateway.delivery.models import (
    DeliveryAttempt,
    DeliveryOutboxRecord,
    DeliveryReceipt,
)
from cf_agent_gateway.ingestion import MessageAdmissionService, MessageStoreAdmissionSink
from cf_agent_gateway.message.models import Attachment, Message, MessageRawPayload
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.runtime import worker as runtime_worker_module
from cf_agent_gateway.task.model.models import HermesDispatchRecord

ACCOUNT_ID = "wxid_gateway"
CHAT_ID = "wxid_alice"
TIMESTAMP = "2026-08-01T10:15:00+08:00"
EMPTY_WINDOW_OBSERVED_AT = datetime(2026, 8, 30, 9, 20, tzinfo=UTC)


def raw_message(
    local_id: object,
    *,
    chat_id: str = CHAT_ID,
    server_id: object | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "localId": local_id,
        "serverId": local_id if server_id is None else server_id,
        "chatId": chat_id,
        "sender": "wxid_sender",
        "senderName": "Sender",
        "type": 1,
        "content": f"message-{local_id}",
        "timestamp": TIMESTAMP,
    }
    message.update(overrides)
    return message


class FakeWechatClient:
    def __init__(
        self,
        *,
        account_id: str | None = ACCOUNT_ID,
        status: str = "logged_in",
        chats: list[dict[str, Any]] | None = None,
        messages: Mapping[str, list[RawWechatMessage | Mapping[str, Any]]] | None = None,
    ) -> None:
        self.account_id = account_id
        self.status = status
        self.chats = chats if chats is not None else [{"id": CHAT_ID}]
        self.messages = dict(messages or {})
        self.auth_calls = 0
        self.list_chats_calls = 0
        self.list_message_calls: list[str] = []

    def get_auth_status(self) -> AgentWechatAuthStatus:
        self.auth_calls += 1
        return AgentWechatAuthStatus(status=self.status, loggedInUser=self.account_id)

    def list_chats(self) -> list[dict[str, Any]]:
        self.list_chats_calls += 1
        return self.chats

    def list_messages(self, chat_id: str) -> list[RawWechatMessage | Mapping[str, Any]]:
        self.list_message_calls.append(chat_id)
        return self.messages.get(chat_id, [])


class RecordingSink:
    def __init__(self, *, fail_counts: Mapping[str, int] | None = None) -> None:
        self.fail_counts = dict(fail_counts or {})
        self.attempts: list[NormalizedWechatMessage] = []
        self.handled: list[NormalizedWechatMessage] = []

    def handle(self, message: NormalizedWechatMessage) -> None:
        self.attempts.append(message)
        remaining = self.fail_counts.get(message.source_message_id, 0)
        if remaining:
            self.fail_counts[message.source_message_id] = remaining - 1
            raise RuntimeError("controlled fake sink failure")
        self.handled.append(message)


class TrackingCheckpointStore(WechatSyncCheckpointStore):
    def __init__(
        self,
        session: Session,
        *,
        fail_advance_once: bool = False,
    ) -> None:
        super().__init__(session)
        self.fail_advance_once = fail_advance_once
        self.initialize_calls: list[int] = []
        self.advance_calls: list[int] = []

    def initialize(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        last_local_id: int,
        last_message_fingerprint: str | None = None,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        self.initialize_calls.append(last_local_id)
        return super().initialize(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            last_local_id=last_local_id,
            last_message_fingerprint=last_message_fingerprint,
        )

    def advance_cas(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        expected_last_local_id: int,
        expected_generation: int,
        expected_message_fingerprint: str | None,
        last_local_id: int,
        last_message_fingerprint: str | None,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        self.advance_calls.append(last_local_id)
        if self.fail_advance_once:
            self.fail_advance_once = False
            raise RuntimeError("controlled checkpoint advance failure")
        return super().advance_cas(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            expected_last_local_id=expected_last_local_id,
            expected_generation=expected_generation,
            expected_message_fingerprint=expected_message_fingerprint,
            last_local_id=last_local_id,
            last_message_fingerprint=last_message_fingerprint,
        )


class InitializeRaceCheckpointStore(TrackingCheckpointStore):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.hide_first_get = True

    def get(self, *, source_account_id: str, conversation_id: str) -> WechatSyncCheckpoint | None:
        if self.hide_first_get:
            self.hide_first_get = False
            return None
        return super().get(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )

    def initialize(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        last_local_id: int,
        last_message_fingerprint: str | None = None,
    ) -> tuple[WechatSyncCheckpoint, bool]:
        del last_message_fingerprint
        self.initialize_calls.append(last_local_id)
        existing = WechatSyncCheckpointStore.get(
            self,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        assert existing is not None
        return existing, False


@pytest.fixture
def engine() -> Iterator[Engine]:
    database_engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(database_engine)
    try:
        yield database_engine
    finally:
        database_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session


@pytest.fixture
def checkpoint_store(session: Session) -> WechatSyncCheckpointStore:
    return WechatSyncCheckpointStore(session)


def source_ids(messages: list[NormalizedWechatMessage]) -> list[str]:
    return [message.source_message_id for message in messages]


def checkpoint(
    store: WechatSyncCheckpointStore,
    *,
    account_id: str = ACCOUNT_ID,
    conversation_id: str = CHAT_ID,
) -> WechatSyncCheckpoint | None:
    return store.get(
        source_account_id=account_id,
        conversation_id=conversation_id,
    )


def checkpoint_fingerprint(message: RawWechatMessage | Mapping[str, Any]) -> str:
    fingerprint = build_wechat_checkpoint_fingerprint(message)
    assert fingerprint is not None
    return fingerprint


def pipeline_counts(session: Session) -> dict[str, int]:
    models = (
        Message,
        MessageRawPayload,
        Attachment,
        MessageAdmissionOutcome,
        HermesDispatchRecord,
        ResponseRecord,
        DeliveryOutboxRecord,
        DeliveryAttempt,
        DeliveryReceipt,
    )
    return {
        model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in models
    }


def polling_log_counts(caplog: pytest.LogCaptureFixture) -> tuple[int, int, int]:
    continuity_warnings = sum(
        record.getMessage() == "checkpoint continuity unverified"
        and record.levelno == logging.WARNING
        for record in caplog.records
    )
    chat_info = sum(
        record.name == polling_service_module.__name__
        and record.getMessage() == "poll chat completed"
        and record.levelno == logging.INFO
        for record in caplog.records
    )
    cycle_info = sum(
        record.name == runtime_worker_module.logger.name
        and record.getMessage() == "poll cycle completed"
        and record.levelno == logging.INFO
        for record in caplog.records
    )
    return continuity_warnings, chat_info, cycle_info


def run_shared_lifecycle_worker_cycles(
    *,
    checkpoint_store: WechatSyncCheckpointStore,
    client: FakeWechatClient,
    sink: RecordingSink,
    lifecycle_state: WechatPollingLifecycleState,
    caplog: pytest.LogCaptureFixture,
    cycles: int,
    before_cycle: Callable[[int], None] | None = None,
) -> tuple[list[PollResult], list[tuple[int, int, int]]]:
    settings = Settings(runtime=RuntimeSettings(polling_interval_seconds=3))
    stop_event = Event()
    results: list[PollResult] = []
    snapshots: list[tuple[int, int, int]] = []
    calls = 0

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal calls
        assert candidate is settings
        if calls:
            snapshots.append(polling_log_counts(caplog))
        calls += 1
        if before_cycle is not None:
            before_cycle(calls)
        result = WechatPollingService(
            client,
            checkpoint_store,
            sink,
            lifecycle_state=lifecycle_state,
        ).poll_once()
        results.append(result)
        if calls == cycles:
            stop_event.set()
        return result

    caplog.set_level(logging.INFO, logger=polling_service_module.__name__)
    caplog.set_level(logging.INFO, logger=runtime_worker_module.logger.name)
    runtime_worker_module.run_worker(
        settings,
        stop_event=stop_event,
        poll_once=poll_once,
    )
    snapshots.append(polling_log_counts(caplog))
    return results, snapshots


def test_logged_out_does_not_list_chats_or_messages(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(account_id=None, status="logged_out")
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    assert result.logged_in is False
    assert result.source_account_id is None
    assert result.chats_seen == 0
    assert result.messages_seen == 0
    assert result.failures == []
    assert client.auth_calls == 1
    assert client.list_chats_calls == 0
    assert client.list_message_calls == []
    assert sink.attempts == []


def test_account_and_conversation_checkpoints_are_independent(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    other_chat = "wxid_bob"
    client = FakeWechatClient(
        chats=[{"id": CHAT_ID}, {"id": other_chat}],
        messages={
            CHAT_ID: [raw_message(2)],
            other_chat: [raw_message(4, chat_id=other_chat)],
        },
    )
    sink = RecordingSink()
    service = WechatPollingService(client, checkpoint_store, sink, bootstrap_mode="backfill")

    first_result = service.poll_once()
    client.account_id = "wxid_second_gateway"
    client.chats = [{"id": CHAT_ID}]
    client.messages = {CHAT_ID: [raw_message(3)]}
    second_result = service.poll_once()

    first_chat = checkpoint(checkpoint_store)
    first_other_chat = checkpoint(checkpoint_store, conversation_id=other_chat)
    second_account = checkpoint(checkpoint_store, account_id="wxid_second_gateway")
    assert first_result.messages_processed == 2
    assert second_result.messages_processed == 1
    assert first_chat is not None and first_chat.last_local_id == 2
    assert first_other_chat is not None and first_other_chat.last_local_id == 4
    assert second_account is not None and second_account.last_local_id == 3
    assert len({first_chat.id, first_other_chat.id, second_account.id}) == 3


def test_reverse_api_order_reaches_sink_in_numeric_local_id_order(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message("10"), raw_message(3), raw_message(2)]}
    )
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert source_ids(sink.handled) == ["2", "3", "10"]
    assert result.messages_processed == 3
    assert checkpoint(checkpoint_store).last_local_id == 10  # type: ignore[union-attr]


def test_messages_at_or_below_checkpoint_are_not_delivered_again(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=2,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(2)),
    )
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(2), raw_message(1)]})
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert sink.attempts == []
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 2
    assert result.chats_succeeded == 1


def test_message_above_checkpoint_is_delivered_and_persisted(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=2,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(2)),
    )
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(3, isSelf=False), raw_message(2), raw_message(1)]}
    )
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    assert source_ids(sink.handled) == ["3"]
    assert result.messages_processed == 1
    assert result.messages_skipped_by_checkpoint == 2
    assert checkpoint(checkpoint_store).last_local_id == 3  # type: ignore[union-attr]


def test_latest_checkpoint_regression_rebases_visible_window_without_replay(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(12), raw_message(10), raw_message(11)]}
    )
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_succeeded == 1
    assert result.messages_seen == 3
    assert result.messages_processed == 0
    assert result.messages_new == 0
    assert result.messages_duplicate == 0
    assert result.messages_failed == 0
    assert result.messages_skipped_by_checkpoint == 3
    assert result.messages_without_server_id == 0
    assert result.bootstrapped_chats == 1
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (12, 1)
    assert stored.last_message_fingerprint == checkpoint_fingerprint(raw_message(12))


def test_backfill_checkpoint_regression_rewinds_and_replays_visible_window(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(12), raw_message(10), raw_message(11)]}
    )
    sink = RecordingSink()

    result = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_succeeded == 1
    assert result.messages_processed == 3
    assert source_ids(sink.handled) == ["10", "11", "12"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (12, 1)
    assert stored.last_message_fingerprint == checkpoint_fingerprint(raw_message(12))


def test_forward_window_after_checkpoint_advances_without_rewind(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
    )
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(13), raw_message(11), raw_message(12)]}
    )
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 3
    assert source_ids(sink.handled) == ["11", "12", "13"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (13, 0)


def test_latest_empty_window_fails_closed_without_rebasing_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
    )
    sink = RecordingSink()

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: []}),
        checkpoint_store,
        sink,
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.messages_seen == 0
    assert result.failures[0].code == WechatCheckpointContinuityError.code
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (15, 0)


def test_latest_single_server_message_without_empty_window_evidence_is_still_skipped(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    sink = RecordingSink()

    result = WechatPollingService(
        FakeWechatClient(
            messages={
                CHAT_ID: [
                    raw_message(
                        1,
                        server_id="single-server-id",
                        timestamp="2026-08-30T09:20:37Z",
                    )
                ]
            }
        ),
        checkpoint_store,
        sink,
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 1
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


def test_latest_empty_window_without_checkpoint_fingerprint_cannot_admit_live_candidate(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )

    service.poll_once()
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:20:37Z"),
    ]
    result = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 1
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


@pytest.mark.parametrize(
    "conversation_id",
    [CHAT_ID, "engineering@chatroom"],
)
def test_latest_empty_window_preserves_first_strictly_later_live_message_once(
    conversation_id: str,
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=conversation_id,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15, chat_id=conversation_id)),
    )
    client = FakeWechatClient(
        chats=[{"id": conversation_id}],
        messages={conversation_id: []},
    )
    sink = RecordingSink()
    lifecycle_state = WechatPollingLifecycleState(
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )
    empty_service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        lifecycle_state=lifecycle_state,
    )

    first_empty = empty_service.poll_once()
    client.messages[conversation_id] = [
        raw_message(
            1,
            chat_id=conversation_id,
            server_id="live-server-1",
            timestamp="2026-08-30T09:20:37Z",
            isMentioned=True if conversation_id.endswith("@chatroom") else None,
        )
    ]
    live = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        lifecycle_state=lifecycle_state,
    ).poll_once()
    repeated = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        lifecycle_state=lifecycle_state,
    ).poll_once()

    stored = checkpoint(
        checkpoint_store,
        conversation_id=conversation_id,
    )
    assert first_empty.chats_failed == 1
    assert live.chats_succeeded == 1
    assert live.messages_processed == 1
    assert live.messages_new == 1
    assert live.messages_skipped_by_checkpoint == 0
    assert repeated.messages_processed == 0
    assert repeated.messages_skipped_by_checkpoint == 1
    assert source_ids(sink.handled) == ["live-server-1"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


def test_latest_empty_window_preserves_earliest_empty_since(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    observations = iter(
        [
            datetime(2026, 8, 30, 9, 20, tzinfo=UTC),
            datetime(2026, 8, 30, 9, 21, tzinfo=UTC),
        ]
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: next(observations),
    )

    service.poll_once()
    service.poll_once()
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:20:30Z"),
    ]
    result = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 1
    assert result.messages_new == 1
    assert source_ids(sink.handled) == ["1"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


def test_latest_empty_window_matches_cfserver_visibility_delay_timeline(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    empty_observations = iter(
        [
            datetime(2026, 8, 31, 9, 19, 32, tzinfo=UTC),
            datetime(2026, 8, 31, 9, 20, 39, tzinfo=UTC),
            datetime(2026, 8, 31, 9, 20, 42, tzinfo=UTC),
            datetime(2026, 8, 31, 9, 20, 46, tzinfo=UTC),
        ]
    )
    lifecycle_state = WechatPollingLifecycleState(
        clock=lambda: next(empty_observations),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()

    for _ in range(4):
        empty_result = WechatPollingService(
            client,
            checkpoint_store,
            sink,
            lifecycle_state=lifecycle_state,
        ).poll_once()
        assert empty_result.chats_failed == 1

    client.messages[CHAT_ID] = [
        raw_message(
            1,
            server_id="live-server-1",
            timestamp="2026-08-31T09:20:37Z",
        )
    ]
    live = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        lifecycle_state=lifecycle_state,
    ).poll_once()
    repeated = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        lifecycle_state=lifecycle_state,
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert live.messages_processed == 1
    assert live.messages_new == 1
    assert repeated.messages_processed == 0
    assert repeated.messages_skipped_by_checkpoint == 1
    assert source_ids(sink.handled) == ["live-server-1"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


def test_new_polling_lifecycle_state_does_not_reuse_old_empty_window_marker(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    old_lifecycle = WechatPollingLifecycleState(
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )
    WechatPollingService(
        client,
        checkpoint_store,
        sink,
        lifecycle_state=old_lifecycle,
    ).poll_once()
    client.messages[CHAT_ID] = [
        raw_message(
            1,
            server_id="live-server-1",
            timestamp="2026-08-30T09:20:37Z",
        )
    ]

    result = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        lifecycle_state=WechatPollingLifecycleState(),
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 1
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


def test_latest_empty_window_marker_is_invalidated_by_normal_advance(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )

    service.poll_once()
    client.messages[CHAT_ID] = [
        raw_message(15),
        raw_message(16, timestamp="2026-08-30T09:20:10Z"),
    ]
    advanced = service.poll_once()
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:20:37Z"),
    ]
    regressed = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert advanced.messages_processed == 1
    assert regressed.messages_processed == 0
    assert regressed.messages_skipped_by_checkpoint == 1
    assert source_ids(sink.handled) == ["16"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


def test_latest_empty_window_marker_is_invalidated_by_account_change(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    other_account = "wxid_other_gateway"
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )

    service.poll_once()
    client.account_id = other_account
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:20:37Z"),
    ]
    other_account_bootstrap = service.poll_once()
    client.account_id = ACCOUNT_ID
    original_account_regression = service.poll_once()

    original_checkpoint = checkpoint(checkpoint_store)
    other_checkpoint = checkpoint(
        checkpoint_store,
        account_id=other_account,
    )
    assert other_account_bootstrap.messages_processed == 0
    assert original_account_regression.messages_processed == 0
    assert sink.attempts == []
    assert original_checkpoint is not None
    assert (original_checkpoint.last_local_id, original_checkpoint.regression_generation) == (1, 1)
    assert other_checkpoint is not None
    assert (other_checkpoint.last_local_id, other_checkpoint.regression_generation) == (1, 0)


def test_latest_empty_window_skips_message_not_strictly_later_than_marker(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )

    service.poll_once()
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:20:00Z"),
    ]
    result = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_succeeded == 1
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 1
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 1)


def test_latest_empty_window_skips_history_prefix_and_processes_live_suffix(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )

    service.poll_once()
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:00:00Z"),
        raw_message(2, timestamp="2026-08-30T09:19:59Z"),
        raw_message(3, timestamp="2026-08-30T09:20:01Z"),
        raw_message(4, timestamp="2026-08-30T09:20:02Z"),
    ]
    result = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_succeeded == 1
    assert result.messages_processed == 2
    assert result.messages_new == 2
    assert result.messages_skipped_by_checkpoint == 2
    assert source_ids(sink.handled) == ["3", "4"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (4, 1)


@pytest.mark.parametrize("failure", ["missing_timestamp", "time_order", "local_id_gap"])
def test_latest_empty_window_time_ambiguity_fails_closed(
    failure: str,
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )
    service.poll_once()
    if failure == "missing_timestamp":
        ambiguous = raw_message(1)
        ambiguous.pop("timestamp")
        client.messages[CHAT_ID] = [ambiguous]
    elif failure == "time_order":
        client.messages[CHAT_ID] = [
            raw_message(1, timestamp="2026-08-30T09:20:02Z"),
            raw_message(2, timestamp="2026-08-30T09:19:59Z"),
        ]
    else:
        client.messages[CHAT_ID] = [
            raw_message(1, timestamp="2026-08-30T09:19:59Z"),
            raw_message(3, timestamp="2026-08-30T09:20:02Z"),
        ]

    result = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].code == WechatCheckpointContinuityError.code
    assert result.messages_processed == 0
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (15, 0)


@pytest.mark.parametrize(
    "mismatch",
    ["last_local_id", "generation", "fingerprint"],
)
def test_latest_empty_window_marker_checkpoint_mismatch_fails_closed(
    mismatch: str,
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
) -> None:
    stored, _ = checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )

    service.poll_once()
    if mismatch == "last_local_id":
        stored.last_local_id = 16
    elif mismatch == "generation":
        stored.regression_generation = 1
    else:
        stored.last_message_fingerprint = "f" * 64
    session.commit()
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:20:37Z"),
    ]
    result = service.poll_once()

    persisted = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].code == WechatCheckpointContinuityError.code
    assert result.messages_processed == 0
    assert sink.attempts == []
    assert persisted is not None
    assert persisted.last_local_id == (16 if mismatch == "last_local_id" else 15)
    assert persisted.regression_generation == (1 if mismatch == "generation" else 0)
    assert persisted.last_message_fingerprint == (
        "f" * 64 if mismatch == "fingerprint" else checkpoint_fingerprint(raw_message(15))
    )


def test_latest_empty_window_live_suffix_retry_uses_anchored_history_baseline(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink(fail_counts={"3": 1})
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )
    service.poll_once()
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:19:58Z"),
        raw_message(2, timestamp="2026-08-30T09:19:59Z"),
        raw_message(3, timestamp="2026-08-30T09:20:01Z"),
    ]

    failed = service.poll_once()
    baseline = checkpoint(checkpoint_store)
    assert baseline is not None
    baseline_state = (
        baseline.last_local_id,
        baseline.regression_generation,
        baseline.last_message_fingerprint,
    )
    retried = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert failed.chats_failed == 1
    assert failed.failures[0].stage is PollFailureStage.SINK
    assert baseline_state == (
        2,
        1,
        checkpoint_fingerprint(raw_message(2, timestamp="2026-08-30T09:19:59Z")),
    )
    assert retried.messages_processed == 1
    assert source_ids(sink.attempts) == ["3", "3"]
    assert source_ids(sink.handled) == ["3"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (3, 1)


def test_backfill_empty_window_does_not_rewind_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
    )
    sink = RecordingSink()

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: []}),
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_succeeded == 1
    assert result.messages_seen == 0
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (15, 0)


def test_list_messages_failure_does_not_rewind_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    class FailingListClient(FakeWechatClient):
        def list_messages(self, chat_id: str) -> list[RawWechatMessage | Mapping[str, Any]]:
            self.list_message_calls.append(chat_id)
            raise RuntimeError("controlled upstream failure")

    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )

    result = WechatPollingService(
        FailingListClient(),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].stage is PollFailureStage.LIST_MESSAGES
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (15, 0)


def test_incomplete_invalid_window_does_not_rewind_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    incomplete = raw_message(11)
    incomplete.pop("localId")

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: [raw_message(10), incomplete]}),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].stage is PollFailureStage.VALIDATE_MESSAGE
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (15, 0)


def test_matching_checkpoint_anchor_does_not_rewind(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    anchor = raw_message(10, server_id="stable-server-10")
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
        last_message_fingerprint=checkpoint_fingerprint(anchor),
    )
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(12), anchor, raw_message(11)]})
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 2
    assert source_ids(sink.handled) == ["11", "12"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (12, 0)


def test_latest_mismatched_checkpoint_anchor_rebases_without_replay(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
        last_message_fingerprint=checkpoint_fingerprint(
            raw_message(10, server_id="old-session-server-10")
        ),
    )
    replacement = raw_message(10, server_id="new-session-server-10")
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(12), replacement, raw_message(11)]})
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_succeeded == 1
    assert result.messages_processed == 0
    assert result.messages_new == 0
    assert result.messages_duplicate == 0
    assert result.messages_skipped_by_checkpoint == 3
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (12, 1)
    assert stored.last_message_fingerprint == checkpoint_fingerprint(raw_message(12))


def test_latest_rebase_has_no_history_side_effects_and_processes_one_new_message_once(
    session: Session,
) -> None:
    store = WechatSyncCheckpointStore(session)
    store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    history = [raw_message(12), raw_message(10), raw_message(11)]
    client = FakeWechatClient(messages={CHAT_ID: history})
    sink = MessageStoreAdmissionSink(MessageAdmissionService(session))
    service = WechatPollingService(client, store, sink)
    empty_counts = {
        "messages": 0,
        "message_raw_payloads": 0,
        "attachments": 0,
        "message_admission_outcomes": 0,
        "hermes_dispatch_records": 0,
        "hermes_responses": 0,
        "delivery_outbox": 0,
        "delivery_attempts": 0,
        "delivery_receipts": 0,
    }

    recovered = service.poll_once()
    repeated_history = service.poll_once()

    assert recovered.messages_skipped_by_checkpoint == 3
    assert repeated_history.messages_skipped_by_checkpoint == 3
    assert pipeline_counts(session) == empty_counts

    client.messages[CHAT_ID] = [*history, raw_message(13)]
    new_message = service.poll_once()
    repeated_new_message = service.poll_once()

    assert new_message.messages_processed == 1
    assert new_message.messages_new == 1
    assert new_message.messages_duplicate == 0
    assert repeated_new_message.messages_processed == 0
    assert repeated_new_message.messages_skipped_by_checkpoint == 4
    assert pipeline_counts(session) == {
        **empty_counts,
        "messages": 1,
        "message_raw_payloads": 1,
        "message_admission_outcomes": 1,
    }
    stored = checkpoint(store)
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (13, 1)


def test_legacy_checkpoint_enrolls_anchor_before_forward_progress(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
    )
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(12), raw_message(10), raw_message(11)]}
    )
    sink = RecordingSink()
    service = WechatPollingService(client, checkpoint_store, sink)

    enrollment = service.poll_once()

    enrolled = checkpoint(checkpoint_store)
    assert enrollment.chats_failed == 1
    assert enrollment.failures[0].code == WechatCheckpointContinuityError.code
    assert sink.attempts == []
    assert enrolled is not None
    assert (enrolled.last_local_id, enrolled.regression_generation) == (10, 0)
    assert enrolled.last_message_fingerprint == checkpoint_fingerprint(raw_message(10))

    resumed = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert resumed.chats_succeeded == 1
    assert resumed.messages_processed == 2
    assert source_ids(sink.handled) == ["11", "12"]
    assert stored is not None and stored.last_local_id == 12


def test_legacy_checkpoint_without_server_anchor_enrolls_fallback_then_resumes(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
    )
    client = FakeWechatClient(
        messages={
            CHAT_ID: [
                raw_message(10, serverId=None),
                raw_message(11),
            ]
        }
    )
    sink = RecordingSink()
    service = WechatPollingService(client, checkpoint_store, sink)

    enrollment = service.poll_once()

    enrolled = checkpoint(checkpoint_store)
    assert enrollment.chats_failed == 1
    assert enrollment.failures[0].code == WechatCheckpointContinuityError.code
    assert enrollment.messages_without_server_id == 1
    assert sink.attempts == []
    assert enrolled is not None
    assert (enrolled.last_local_id, enrolled.regression_generation) == (10, 0)
    assert enrolled.last_message_fingerprint == checkpoint_fingerprint(
        raw_message(10, serverId=None)
    )

    resumed = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert resumed.chats_succeeded == 1
    assert resumed.messages_processed == 1
    assert resumed.messages_without_server_id == 1
    assert source_ids(sink.handled) == ["11"]
    assert stored is not None and stored.last_local_id == 11


def test_backfill_server_identity_remains_stable_when_regression_replays_message(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    replayed = raw_message(10, server_id="stable-server-10")
    before_regression = normalize_wechat_message(
        replayed,
        source_account_id=ACCOUNT_ID,
        regression_generation=0,
    )
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(
            raw_message(15, server_id="old-session-server-15")
        ),
    )
    sink = RecordingSink()

    WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: [replayed]}),
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
    ).poll_once()

    assert len(sink.handled) == 1
    assert sink.handled[0].source_message_id == before_regression.source_message_id
    assert sink.handled[0].event_id == before_regression.event_id


def test_latest_regression_without_server_ids_rebases_without_fallback_replay(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(
        messages={
            CHAT_ID: [
                raw_message(10, serverId=None),
                raw_message(11, serverId=None),
            ]
        }
    )
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 0
    assert result.messages_new == 0
    assert result.messages_without_server_id == 2
    assert result.messages_skipped_by_checkpoint == 2
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (11, 1)
    assert stored.last_message_fingerprint == checkpoint_fingerprint(raw_message(11, serverId=None))


def test_backfill_fallback_identity_is_isolated_by_regression_generation(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    replayed = raw_message(10, serverId=None)
    previous_generation = normalize_wechat_message(
        replayed,
        source_account_id=ACCOUNT_ID,
        regression_generation=0,
    )
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    sink = RecordingSink()
    client = FakeWechatClient(messages={CHAT_ID: [replayed]})
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
    )

    recovered = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert recovered.messages_processed == 1
    assert recovered.messages_without_server_id == 1
    assert len(sink.handled) == 1
    assert sink.handled[0].source_message_id.startswith("local:v2:")
    assert sink.handled[0].source_message_id != previous_generation.source_message_id
    assert stored is not None
    assert stored.regression_generation == 1
    assert stored.last_message_fingerprint == checkpoint_fingerprint(replayed)

    confirmed = service.poll_once()

    assert confirmed.chats_succeeded == 1
    assert confirmed.chats_failed == 0
    assert confirmed.messages_processed == 0
    assert confirmed.messages_skipped_by_checkpoint == 1
    assert confirmed.failures == []
    assert len(sink.handled) == 1


def test_rewind_compare_and_swap_allows_only_one_winner(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    anchor = checkpoint_fingerprint(raw_message(15))
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=anchor,
    )

    first, first_changed = checkpoint_store.rewind(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        expected_last_local_id=15,
        expected_generation=0,
        expected_message_fingerprint=anchor,
        last_local_id=9,
    )
    first_state = (first.last_local_id, first.regression_generation)
    second, second_changed = checkpoint_store.rewind(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        expected_last_local_id=15,
        expected_generation=0,
        expected_message_fingerprint=anchor,
        last_local_id=9,
    )

    assert first_changed is True
    assert first_state == (9, 1)
    assert second_changed is False
    assert (second.last_local_id, second.regression_generation) == (9, 1)


def test_latest_rebase_compare_and_swap_allows_only_one_winner(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    anchor = checkpoint_fingerprint(raw_message(15))
    latest_fingerprint = checkpoint_fingerprint(raw_message(12))
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=anchor,
    )

    first, first_changed = checkpoint_store.rebase_latest_after_regression(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        expected_last_local_id=15,
        expected_generation=0,
        expected_message_fingerprint=anchor,
        remote_latest_local_id=12,
        remote_latest_message_fingerprint=latest_fingerprint,
    )
    first_state = (
        first.last_local_id,
        first.regression_generation,
        first.last_message_fingerprint,
    )
    second, second_changed = checkpoint_store.rebase_latest_after_regression(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        expected_last_local_id=15,
        expected_generation=0,
        expected_message_fingerprint=anchor,
        remote_latest_local_id=12,
        remote_latest_message_fingerprint=latest_fingerprint,
    )

    assert first_changed is True
    assert first_state == (12, 1, latest_fingerprint)
    assert second_changed is False
    assert (
        second.last_local_id,
        second.regression_generation,
        second.last_message_fingerprint,
    ) == (12, 1, latest_fingerprint)


def test_latest_rebase_cas_loser_fails_closed_without_processing(
    checkpoint_store: WechatSyncCheckpointStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    anchor = checkpoint_fingerprint(raw_message(15))
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=anchor,
    )
    original_rebase = checkpoint_store.rebase_latest_after_regression

    def lose_rebase(**kwargs: Any) -> tuple[WechatSyncCheckpoint, bool]:
        _, winner_changed = original_rebase(**kwargs)
        assert winner_changed is True
        return original_rebase(**kwargs)

    monkeypatch.setattr(checkpoint_store, "rebase_latest_after_regression", lose_rebase)
    caplog.set_level(logging.WARNING, logger=polling_service_module.__name__)
    sink = RecordingSink()

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: [raw_message(10), raw_message(11)]}),
        checkpoint_store,
        sink,
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].code == WechatCheckpointStateConflictError.code
    assert result.messages_skipped_by_checkpoint == 2
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (11, 1)
    conflict_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint regression rebase conflicted"
    )
    assert conflict_record.__dict__["fields"]["cas_result"] is False
    assert conflict_record.__dict__["fields"]["recovery_action"] == ("stop_chat_after_cas_loss")
    assert conflict_record.__dict__["fields"]["messages_skipped"] == 2


def test_latest_empty_window_live_suffix_cas_loser_fails_closed(
    checkpoint_store: WechatSyncCheckpointStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        clock=lambda: EMPTY_WINDOW_OBSERVED_AT,
    )
    service.poll_once()
    original_transition = checkpoint_store.begin_live_suffix_after_empty_window

    def lose_transition(**kwargs: Any) -> tuple[WechatSyncCheckpoint, bool]:
        _, winner_changed = original_transition(**kwargs)
        assert winner_changed is True
        return original_transition(**kwargs)

    monkeypatch.setattr(
        checkpoint_store,
        "begin_live_suffix_after_empty_window",
        lose_transition,
    )
    client.messages[CHAT_ID] = [
        raw_message(1, timestamp="2026-08-30T09:20:37Z"),
    ]

    result = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].code == WechatCheckpointStateConflictError.code
    assert result.messages_processed == 0
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (0, 1)


def test_latest_regression_without_latest_fingerprint_fails_closed(
    checkpoint_store: WechatSyncCheckpointStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    monkeypatch.setattr(
        polling_service_module,
        "build_wechat_checkpoint_fingerprint",
        lambda _message: None,
    )
    caplog.set_level(logging.WARNING, logger=polling_service_module.__name__)
    sink = RecordingSink()

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: [raw_message(10), raw_message(11)]}),
        checkpoint_store,
        sink,
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].code == WechatCheckpointContinuityError.code
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 2
    assert sink.attempts == []
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (15, 0)
    failed_closed = next(
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint regression failed closed"
    )
    assert failed_closed.__dict__["fields"]["recovery_action"] == (
        "stop_chat_latest_fingerprint_unavailable"
    )
    assert failed_closed.__dict__["fields"]["cas_result"] is None


def test_latest_regression_generation_overflow_fails_closed(
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
) -> None:
    stored, _ = checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    stored.regression_generation = 2**63 - 1
    session.commit()
    sink = RecordingSink()

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: [raw_message(10), raw_message(11)]}),
        checkpoint_store,
        sink,
    ).poll_once()

    persisted = checkpoint(checkpoint_store)
    assert result.chats_failed == 1
    assert result.failures[0].code == WechatCheckpointGenerationError.code
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 2
    assert sink.attempts == []
    assert persisted is not None
    assert (persisted.last_local_id, persisted.regression_generation) == (
        15,
        2**63 - 1,
    )


def test_stale_generation_cannot_advance_after_rewind(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    anchor = checkpoint_fingerprint(raw_message(15))
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=anchor,
    )
    checkpoint_store.rewind(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        expected_last_local_id=15,
        expected_generation=0,
        expected_message_fingerprint=anchor,
        last_local_id=9,
    )

    stale, changed = checkpoint_store.advance_cas(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        expected_last_local_id=15,
        expected_generation=0,
        expected_message_fingerprint=anchor,
        last_local_id=16,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(16)),
    )

    assert changed is False
    assert (stale.last_local_id, stale.regression_generation) == (9, 1)


def test_backfill_self_message_during_recovery_advances_without_entering_sink(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(
        messages={
            CHAT_ID: [
                raw_message(10, isSelf=True),
                raw_message(11, isSelf=False),
            ]
        }
    )
    sink = RecordingSink()

    result = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.messages_processed == 1
    assert source_ids(sink.handled) == ["11"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (11, 1)


def test_backfill_checkpoint_failure_during_recovery_redelivers_in_same_generation(
    session: Session,
) -> None:
    store = TrackingCheckpointStore(session, fail_advance_once=True)
    store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(12), raw_message(10), raw_message(11)]}
    )
    sink = RecordingSink()
    service = WechatPollingService(client, store, sink, bootstrap_mode="backfill")

    first = service.poll_once()
    after_failure = checkpoint(store)
    assert after_failure is not None
    after_failure_state = (
        after_failure.last_local_id,
        after_failure.regression_generation,
    )
    second = service.poll_once()

    stored = checkpoint(store)
    assert first.chats_failed == 1
    assert first.failures[0].stage is PollFailureStage.CHECKPOINT
    assert after_failure_state == (9, 1)
    assert second.messages_processed == 3
    assert source_ids(sink.attempts) == ["10", "10", "11", "12"]
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (12, 1)


def test_checkpoint_recovery_logs_are_structured_and_redacted(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_account = "wxid-sensitive-account"
    sensitive_chat = "wxid-sensitive-conversation"
    sensitive_body = "private message body must not be logged"
    sensitive_token = "secret-token-must-not-be-logged"
    sensitive_sender = "wxid-sensitive-sender"
    sensitive_server_id = "sensitive-server-id"
    checkpoint_store.initialize(
        source_account_id=sensitive_account,
        conversation_id=sensitive_chat,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15, chat_id=sensitive_chat)),
    )
    caplog.set_level(logging.INFO, logger=polling_service_module.__name__)

    WechatPollingService(
        FakeWechatClient(
            account_id=sensitive_account,
            chats=[{"id": sensitive_chat}],
            messages={
                sensitive_chat: [
                    raw_message(
                        10,
                        chat_id=sensitive_chat,
                        server_id=sensitive_server_id,
                        content=f"{sensitive_body} {sensitive_token}",
                        sender=sensitive_sender,
                    )
                ]
            },
        ),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    events = [
        record
        for record in caplog.records
        if record.name == polling_service_module.__name__
        and record.getMessage().startswith("checkpoint regression")
    ]
    assert [record.getMessage() for record in events] == [
        "checkpoint regression detected",
        "checkpoint regression rebased",
    ]
    assert [record.levelno for record in events] == [logging.WARNING, logging.WARNING]
    serialized_records = repr([record.__dict__ for record in events])
    assert sensitive_account not in serialized_records
    assert sensitive_chat not in serialized_records
    assert sensitive_body not in serialized_records
    assert sensitive_token not in serialized_records
    assert sensitive_sender not in serialized_records
    assert sensitive_server_id not in serialized_records
    fields = events[-1].__dict__["fields"]
    assert set(fields) == {
        "anchor_match",
        "cas_result",
        "conversation_id_ref",
        "messages_skipped",
        "new_generation",
        "old_checkpoint",
        "old_generation",
        "recovery_action",
        "remote_first_local_id",
        "remote_latest_local_id",
        "source_account_id_ref",
    }
    assert fields["source_account_id_ref"].startswith("source_account:sha256:")
    assert fields["conversation_id_ref"].startswith("conversation:sha256:")
    assert fields["cas_result"] is True
    assert fields["old_generation"] == 0
    assert fields["new_generation"] == 1
    assert fields["messages_skipped"] == 1
    assert fields["recovery_action"] == "rebase_latest_visible_window"


def test_idle_chat_summary_is_debug_and_contains_only_redacted_references(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=0,
    )
    caplog.set_level(logging.DEBUG, logger=polling_service_module.__name__)

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: []}),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    summaries = [
        record for record in caplog.records if record.getMessage() == "poll chat completed"
    ]
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.DEBUG
    assert result.chat_results[0].succeeded is True
    assert result.messages_seen == 0
    assert result.messages_processed == 0
    serialized = repr(summaries[0].__dict__)
    assert ACCOUNT_ID not in serialized
    assert CHAT_ID not in serialized
    assert (
        summaries[0]
        .fields["source_account_id_ref"]
        .startswith(  # type: ignore[attr-defined]
            "source_account:sha256:"
        )
    )
    assert (
        summaries[0]
        .fields["conversation_id_ref"]
        .startswith(  # type: ignore[attr-defined]
            "conversation:sha256:"
        )
    )


def test_stable_production_history_shape_does_not_repeat_chat_or_cycle_info(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stable_counts = (9, 14, 20, 50, 1, 1)
    chat_ids = [f"wxid-production-{index:02d}" for index in range(21)]
    messages: dict[str, list[dict[str, Any]]] = {}
    for index, chat_id in enumerate(chat_ids):
        count = stable_counts[index] if index < len(stable_counts) else 0
        visible = [raw_message(local_id, chat_id=chat_id) for local_id in range(1, count + 1)]
        messages[chat_id] = visible
        checkpoint_store.initialize(
            source_account_id=ACCOUNT_ID,
            conversation_id=chat_id,
            last_local_id=count,
            last_message_fingerprint=(
                checkpoint_fingerprint(raw_message(count, chat_id=chat_id)) if count else None
            ),
        )

    client = FakeWechatClient(
        chats=[{"id": chat_id} for chat_id in chat_ids],
        messages=messages,
    )
    service = WechatPollingService(client, checkpoint_store, RecordingSink())
    settings = Settings(runtime=RuntimeSettings(polling_interval_seconds=3))
    results: list[PollResult] = []

    class StopAfterCycles(Event):
        def __init__(self, cycles: int) -> None:
            super().__init__()
            self._remaining = cycles

        def wait(self, timeout: float | None = None) -> bool:
            del timeout
            self._remaining -= 1
            return self._remaining == 0

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        result = service.poll_once()
        results.append(result)
        return result

    caplog.set_level(logging.INFO, logger=polling_service_module.__name__)
    caplog.set_level(logging.INFO, logger=runtime_worker_module.logger.name)
    runtime_worker_module.run_worker(
        settings,
        stop_event=StopAfterCycles(4),
        poll_once=poll_once,
    )

    assert len(results) == 4
    for result in results:
        assert result.chats_seen == 21
        assert result.messages_seen == 95
        assert result.messages_skipped_by_checkpoint == 95
        assert result.messages_processed == 0
        assert result.messages_new == 0
        assert result.messages_duplicate == 0
        assert result.messages_failed == 0

    chat_info = [
        record
        for record in caplog.records
        if record.name == polling_service_module.__name__
        and record.getMessage() == "poll chat completed"
        and record.levelno == logging.INFO
    ]
    cycle_info = [
        record
        for record in caplog.records
        if record.name == runtime_worker_module.logger.name
        and record.getMessage() == "poll cycle completed"
        and record.levelno == logging.INFO
    ]
    assert len(chat_info) == 6
    assert sorted(record.fields["messages_seen"] for record in chat_info) == [  # type: ignore[attr-defined]
        1,
        1,
        9,
        14,
        20,
        50,
    ]
    assert len(cycle_info) == 1
    assert cycle_info[0].fields["chats_seen"] == 21  # type: ignore[attr-defined]
    assert cycle_info[0].fields["messages_seen"] == 95  # type: ignore[attr-defined]


def test_checkpoint_history_window_shape_change_reenables_one_info_summary(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(10)),
    )
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(8), raw_message(9), raw_message(10)]})
    service = WechatPollingService(client, checkpoint_store, RecordingSink())
    caplog.set_level(logging.DEBUG, logger=polling_service_module.__name__)

    service.poll_once()
    service.poll_once()
    client.messages[CHAT_ID] = [raw_message(7), raw_message(9), raw_message(10)]
    service.poll_once()

    summaries = [
        record for record in caplog.records if record.getMessage() == "poll chat completed"
    ]
    assert [record.levelno for record in summaries] == [
        logging.INFO,
        logging.DEBUG,
        logging.INFO,
    ]


def test_persistent_empty_window_continuity_is_deduplicated_across_worker_cycles(
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat_ids = [f"wxid-empty-{index}" for index in range(5)]
    for index, chat_id in enumerate(chat_ids, start=1):
        checkpoint_store.initialize(
            source_account_id=ACCOUNT_ID,
            conversation_id=chat_id,
            last_local_id=index,
            last_message_fingerprint=checkpoint_fingerprint(raw_message(index, chat_id=chat_id)),
        )

    client = FakeWechatClient(
        chats=[{"id": chat_id} for chat_id in chat_ids],
        messages={chat_id: [] for chat_id in chat_ids},
    )
    lifecycle_state = WechatPollingLifecycleState(clock=lambda: EMPTY_WINDOW_OBSERVED_AT)
    settings = Settings(runtime=RuntimeSettings(polling_interval_seconds=3))
    stop_event = Event()
    results: list[PollResult] = []
    snapshots: list[tuple[int, int, int]] = []
    calls = 0

    def log_counts() -> tuple[int, int, int]:
        warnings = sum(
            record.getMessage() == "checkpoint continuity unverified"
            and record.levelno == logging.WARNING
            for record in caplog.records
        )
        chat_info = sum(
            record.name == polling_service_module.__name__
            and record.getMessage() == "poll chat completed"
            and record.levelno == logging.INFO
            for record in caplog.records
        )
        cycle_info = sum(
            record.name == runtime_worker_module.logger.name
            and record.getMessage() == "poll cycle completed"
            and record.levelno == logging.INFO
            for record in caplog.records
        )
        return warnings, chat_info, cycle_info

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal calls
        assert candidate is settings
        if calls:
            snapshots.append(log_counts())
        calls += 1
        if calls == 5:
            changed = checkpoint(
                checkpoint_store,
                conversation_id=chat_ids[0],
            )
            assert changed is not None
            changed.last_local_id += 1
            changed.regression_generation += 1
            changed.last_message_fingerprint = "f" * 64
            session.commit()

        result = WechatPollingService(
            client,
            checkpoint_store,
            RecordingSink(),
            lifecycle_state=lifecycle_state,
        ).poll_once()
        results.append(result)
        if calls == 5:
            stop_event.set()
        return result

    caplog.set_level(logging.INFO, logger=polling_service_module.__name__)
    caplog.set_level(logging.INFO, logger=runtime_worker_module.logger.name)
    runtime_worker_module.run_worker(
        settings,
        stop_event=stop_event,
        poll_once=poll_once,
    )
    snapshots.append(log_counts())

    assert len(results) == 5
    assert all(result.chats_failed == 5 for result in results)
    assert all(result.messages_seen == 0 for result in results)
    assert all(
        not any(key.startswith("continuity_") for key in chat_result.model_dump())
        for result in results
        for chat_result in result.chat_results
    )
    assert snapshots == [
        (5, 5, 1),
        (5, 5, 1),
        (5, 5, 1),
        (5, 5, 1),
        (6, 6, 2),
    ]
    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint continuity unverified"
    ]
    assert [
        record.fields["recovery_action"]  # type: ignore[attr-defined]
        for record in warnings
    ] == [
        *(["stop_chat_visible_window_empty"] * 5),
        "stop_chat_empty_window_marker_unavailable",
    ]
    serialized = repr([record.__dict__ for record in warnings])
    assert ACCOUNT_ID not in serialized
    assert all(chat_id not in serialized for chat_id in chat_ids)


@pytest.mark.parametrize(
    "scenario",
    [
        "fingerprint_none",
        "fingerprint_empty",
        "fingerprint_malformed",
        "clock_raises",
        "clock_unusable",
        "clock_backwards",
    ],
)
def test_marker_unavailable_continuity_is_deduplicated_across_worker_cycles(
    scenario: str,
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    valid_fingerprint = checkpoint_fingerprint(raw_message(15))
    stored, _ = checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=(None if scenario == "fingerprint_none" else valid_fingerprint),
    )
    real_get = checkpoint_store.get
    if scenario in {"fingerprint_empty", "fingerprint_malformed"}:
        marker_checkpoint = SimpleNamespace(
            last_local_id=stored.last_local_id,
            regression_generation=stored.regression_generation,
            last_message_fingerprint=("" if scenario == "fingerprint_empty" else "malformed"),
        )
        monkeypatch.setattr(
            checkpoint_store,
            "get",
            lambda **kwargs: marker_checkpoint,
        )

    current_time = [EMPTY_WINDOW_OBSERVED_AT]

    def marker_clock() -> object:
        if scenario == "clock_raises":
            raise RuntimeError("controlled marker clock failure")
        if scenario == "clock_unusable":
            return datetime(2026, 8, 30, 9, 20)
        return current_time[0]

    lifecycle_state = WechatPollingLifecycleState(clock=marker_clock)
    if scenario == "clock_backwards":
        lifecycle_state.observe_account(ACCOUNT_ID)
        assert lifecycle_state.record_empty_window(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            checkpoint=stored,
        )
        current_time[0] = EMPTY_WINDOW_OBSERVED_AT.replace(second=0) - timedelta(seconds=1)

    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    initial_checkpoint = (
        stored.last_local_id,
        stored.regression_generation,
        stored.last_message_fingerprint,
    )
    initial_pipeline = pipeline_counts(session)

    results, snapshots = run_shared_lifecycle_worker_cycles(
        checkpoint_store=checkpoint_store,
        client=client,
        sink=sink,
        lifecycle_state=lifecycle_state,
        caplog=caplog,
        cycles=4,
    )

    assert snapshots == [(1, 1, 1)] * 4
    assert all(result.chats_failed == 1 for result in results)
    assert all(result.messages_processed == 0 for result in results)
    assert all(result.messages_new == 0 for result in results)
    assert all(result.messages_duplicate == 0 for result in results)
    assert all(result.chat_results[0].continuity_only for result in results)
    assert sink.attempts == []
    assert pipeline_counts(session) == initial_pipeline
    persisted = real_get(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
    )
    assert persisted is not None
    assert (
        persisted.last_local_id,
        persisted.regression_generation,
        persisted.last_message_fingerprint,
    ) == initial_checkpoint
    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint continuity unverified"
    ]
    assert len(warnings) == 1
    assert warnings[0].fields["recovery_action"] == (  # type: ignore[attr-defined]
        "stop_chat_empty_window_marker_unavailable"
    )
    assert not [
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint regression live suffix started"
    ]
    counts = lifecycle_state.observation_counts()
    assert counts["empty_markers"] == 0
    assert counts["pending_windows"] == 0
    assert counts["history"] == 0
    assert counts["continuity"] == 1
    assert counts["marker_clock_watermarks"] == (1 if scenario == "clock_backwards" else 0)


def test_marker_mismatch_rejects_marker_without_resetting_continuity(
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stored, _ = checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    stale_marker_checkpoint = SimpleNamespace(
        last_local_id=stored.last_local_id,
        regression_generation=stored.regression_generation,
        last_message_fingerprint="f" * 64,
    )
    lifecycle_state = WechatPollingLifecycleState(clock=lambda: EMPTY_WINDOW_OBSERVED_AT)
    lifecycle_state.observe_account(ACCOUNT_ID)
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    initial_pipeline = pipeline_counts(session)

    def seed_mismatched_marker(cycle: int) -> None:
        del cycle
        assert lifecycle_state.record_empty_window(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            checkpoint=stale_marker_checkpoint,
        )

    results, snapshots = run_shared_lifecycle_worker_cycles(
        checkpoint_store=checkpoint_store,
        client=client,
        sink=sink,
        lifecycle_state=lifecycle_state,
        caplog=caplog,
        cycles=4,
        before_cycle=seed_mismatched_marker,
    )

    assert snapshots == [(1, 1, 1)] * 4
    assert all(result.messages_processed == 0 for result in results)
    assert all(result.messages_new == 0 for result in results)
    assert sink.attempts == []
    assert pipeline_counts(session) == initial_pipeline
    persisted = checkpoint(checkpoint_store)
    assert persisted is not None
    assert (
        persisted.last_local_id,
        persisted.regression_generation,
        persisted.last_message_fingerprint,
    ) == (
        stored.last_local_id,
        stored.regression_generation,
        stored.last_message_fingerprint,
    )
    assert lifecycle_state.observation_counts() == {
        "chats": 1,
        "empty_markers": 0,
        "marker_clock_watermarks": 1,
        "pending_windows": 0,
        "history": 0,
        "continuity": 1,
    }
    warning = next(
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint continuity unverified"
    )
    assert warning.fields["recovery_action"] == (  # type: ignore[attr-defined]
        "stop_chat_empty_window_marker_unavailable"
    )
    assert not [
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint regression live suffix started"
    ]


def test_marker_unavailable_checkpoint_change_reemits_once(
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stored, _ = checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
    )
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    lifecycle_state = WechatPollingLifecycleState(clock=lambda: EMPTY_WINDOW_OBSERVED_AT)
    initial_pipeline = pipeline_counts(session)

    def mutate_checkpoint(cycle: int) -> None:
        if cycle != 5:
            return
        persisted = checkpoint(checkpoint_store)
        assert persisted is not None
        assert (persisted.last_local_id, persisted.regression_generation) == (15, 0)
        assert pipeline_counts(session) == initial_pipeline
        persisted.last_local_id = 16
        persisted.regression_generation = 1
        session.commit()

    results, snapshots = run_shared_lifecycle_worker_cycles(
        checkpoint_store=checkpoint_store,
        client=client,
        sink=sink,
        lifecycle_state=lifecycle_state,
        caplog=caplog,
        cycles=5,
        before_cycle=mutate_checkpoint,
    )

    assert snapshots == [(1, 1, 1)] * 4 + [(2, 2, 2)]
    assert all(result.messages_processed == 0 for result in results)
    assert all(result.messages_new == 0 for result in results)
    assert sink.attempts == []
    assert pipeline_counts(session) == initial_pipeline
    persisted = checkpoint(checkpoint_store)
    assert persisted is not None
    assert (
        persisted.last_local_id,
        persisted.regression_generation,
        persisted.last_message_fingerprint,
    ) == (16, 1, None)
    assert not [
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint regression live suffix started"
    ]


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("checkpoint_local_id", 11),
        ("checkpoint_generation", 3),
        ("checkpoint_fingerprint", "b" * 64),
        ("remote_first_local_id", 1),
        ("remote_latest_local_id", 2),
        ("recovery_action", "stop_chat_anchor_ambiguous"),
        ("failure_code", "changed_continuity_failure"),
    ],
)
def test_continuity_signature_change_is_detected(
    field: str,
    changed_value: object,
) -> None:
    lifecycle_state = WechatPollingLifecycleState()
    lifecycle_state.observe_account(ACCOUNT_ID)
    signature: dict[str, object] = {
        "source_account_id": ACCOUNT_ID,
        "conversation_id": CHAT_ID,
        "checkpoint_local_id": 10,
        "checkpoint_generation": 2,
        "checkpoint_fingerprint": "a" * 64,
        "remote_first_local_id": 0,
        "remote_latest_local_id": 0,
        "recovery_action": "stop_chat_visible_window_empty",
        "failure_code": WechatCheckpointContinuityError.code,
    }

    first_changed, _ = lifecycle_state.observe_continuity_failure(**signature)  # type: ignore[arg-type]
    repeated_changed, _ = lifecycle_state.observe_continuity_failure(**signature)  # type: ignore[arg-type]
    signature[field] = changed_value
    changed, _ = lifecycle_state.observe_continuity_failure(**signature)  # type: ignore[arg-type]

    assert first_changed is True
    assert repeated_changed is False
    assert changed is True


def test_continuity_observation_resets_for_account_and_new_lifecycle() -> None:
    def observe(
        state: WechatPollingLifecycleState,
        account_id: str,
    ) -> tuple[bool, str]:
        state.observe_account(account_id)
        return state.observe_continuity_failure(
            source_account_id=account_id,
            conversation_id=CHAT_ID,
            checkpoint_local_id=10,
            checkpoint_generation=2,
            checkpoint_fingerprint="a" * 64,
            remote_first_local_id=0,
            remote_latest_local_id=0,
            recovery_action="stop_chat_visible_window_empty",
            failure_code=WechatCheckpointContinuityError.code,
        )

    lifecycle_state = WechatPollingLifecycleState()
    first_changed, first_ref = observe(lifecycle_state, ACCOUNT_ID)
    repeated_changed, repeated_ref = observe(lifecycle_state, ACCOUNT_ID)
    account_changed, account_ref = observe(lifecycle_state, "wxid-other-account")
    restarted_changed, restarted_ref = observe(
        WechatPollingLifecycleState(),
        ACCOUNT_ID,
    )

    assert first_changed is True
    assert repeated_changed is False
    assert repeated_ref == first_ref
    assert account_changed is True
    assert account_ref != first_ref
    assert restarted_changed is True
    assert restarted_ref == first_ref


def test_chat_removed_from_list_chats_is_pruned_from_lifecycle_state(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    chat_ids = ("wxid-empty-a", "wxid-empty-b")
    for index, chat_id in enumerate(chat_ids, start=1):
        checkpoint_store.initialize(
            source_account_id=ACCOUNT_ID,
            conversation_id=chat_id,
            last_local_id=index,
            last_message_fingerprint=checkpoint_fingerprint(raw_message(index, chat_id=chat_id)),
        )
    client = FakeWechatClient(
        chats=[{"id": chat_id} for chat_id in chat_ids],
        messages={chat_id: [] for chat_id in chat_ids},
    )
    lifecycle_state = WechatPollingLifecycleState(clock=lambda: EMPTY_WINDOW_OBSERVED_AT)

    first = WechatPollingService(
        client,
        checkpoint_store,
        RecordingSink(),
        lifecycle_state=lifecycle_state,
    ).poll_once()
    client.chats = [{"id": chat_ids[0]}]
    second = WechatPollingService(
        client,
        checkpoint_store,
        RecordingSink(),
        lifecycle_state=lifecycle_state,
    ).poll_once()

    assert first.chats_failed == 2
    assert second.chats_failed == 1
    assert lifecycle_state.observation_counts() == {
        "chats": 1,
        "empty_markers": 1,
        "marker_clock_watermarks": 1,
        "pending_windows": 0,
        "history": 0,
        "continuity": 1,
    }


def test_prune_chats_removes_all_process_lifetime_state_kinds() -> None:
    lifecycle_state = WechatPollingLifecycleState(clock=lambda: EMPTY_WINDOW_OBSERVED_AT)
    lifecycle_state.observe_account(ACCOUNT_ID)
    marker = SimpleNamespace(
        last_local_id=1,
        regression_generation=0,
        last_message_fingerprint="a" * 64,
    )
    lifecycle_state.record_empty_window(
        source_account_id=ACCOUNT_ID,
        conversation_id="empty",
        checkpoint=marker,
    )
    lifecycle_state.record_visible_window(
        source_account_id=ACCOUNT_ID,
        conversation_id="pending",
        local_ids=(1,),
    )
    lifecycle_state.record_visible_window(
        source_account_id=ACCOUNT_ID,
        conversation_id="history",
        local_ids=(1,),
    )
    lifecycle_state.chat_result_log_level(
        source_account_id=ACCOUNT_ID,
        result=polling_service_module.ChatPollResult(
            conversation_id="history",
            succeeded=True,
            messages_seen=1,
            messages_skipped_by_checkpoint=1,
        ),
    )
    lifecycle_state.observe_continuity_failure(
        source_account_id=ACCOUNT_ID,
        conversation_id="continuity",
        checkpoint_local_id=1,
        checkpoint_generation=0,
        checkpoint_fingerprint="b" * 64,
        remote_first_local_id=0,
        remote_latest_local_id=0,
        recovery_action="stop_chat_visible_window_empty",
        failure_code=WechatCheckpointContinuityError.code,
    )

    assert lifecycle_state.observation_counts() == {
        "chats": 4,
        "empty_markers": 1,
        "marker_clock_watermarks": 1,
        "pending_windows": 1,
        "history": 1,
        "continuity": 1,
    }
    lifecycle_state.prune_chats(
        source_account_id=ACCOUNT_ID,
        conversation_ids=("history",),
    )
    assert lifecycle_state.observation_counts() == {
        "chats": 1,
        "empty_markers": 0,
        "marker_clock_watermarks": 0,
        "pending_windows": 0,
        "history": 1,
        "continuity": 0,
    }
    lifecycle_state.prune_chats(
        source_account_id=ACCOUNT_ID,
        conversation_ids=(),
    )
    assert lifecycle_state.observation_counts() == {
        "chats": 0,
        "empty_markers": 0,
        "marker_clock_watermarks": 0,
        "pending_windows": 0,
        "history": 0,
        "continuity": 0,
    }


def test_continuity_chat_churn_is_bounded() -> None:
    lifecycle_state = WechatPollingLifecycleState()
    lifecycle_state.observe_account(ACCOUNT_ID)
    limit = polling_service_module._MAX_LIFECYCLE_CHAT_STATES

    for index in range(limit + 200):
        lifecycle_state.observe_continuity_failure(
            source_account_id=ACCOUNT_ID,
            conversation_id=f"wxid-temporary-{index}",
            checkpoint_local_id=index + 1,
            checkpoint_generation=0,
            checkpoint_fingerprint="c" * 64,
            remote_first_local_id=0,
            remote_latest_local_id=0,
            recovery_action="stop_chat_visible_window_empty",
            failure_code=WechatCheckpointContinuityError.code,
        )

    assert lifecycle_state.cached_chat_count == limit
    assert lifecycle_state.observation_counts() == {
        "chats": limit,
        "empty_markers": 0,
        "marker_clock_watermarks": 0,
        "pending_windows": 0,
        "history": 0,
        "continuity": limit,
    }


def test_124_checkpoint_skips_emit_one_initial_info_summary_and_exact_aggregate(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=124,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(124)),
    )
    caplog.set_level(logging.INFO, logger=polling_service_module.__name__)

    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: [raw_message(local_id) for local_id in range(1, 125)]}),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    info_records = [
        record
        for record in caplog.records
        if record.name == polling_service_module.__name__ and record.levelno == logging.INFO
    ]
    assert [record.getMessage() for record in info_records] == ["poll chat completed"]
    assert result.messages_seen == 124
    assert result.messages_processed == 0
    assert result.messages_new == 0
    assert result.messages_duplicate == 0
    assert result.messages_failed == 0
    assert result.messages_skipped_by_checkpoint == 124
    assert info_records[0].fields["messages_seen"] == 124
    assert info_records[0].fields["messages_skipped_checkpoint"] == 124


def test_failed_chat_summary_remains_info_without_exception_detail(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_detail = "private upstream response body"

    class FailingListClient(FakeWechatClient):
        def list_messages(self, chat_id: str) -> list[RawWechatMessage | Mapping[str, Any]]:
            del chat_id
            raise RuntimeError(sensitive_detail)

    caplog.set_level(logging.INFO, logger=polling_service_module.__name__)

    result = WechatPollingService(
        FailingListClient(),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    summaries = [
        record for record in caplog.records if record.getMessage() == "poll chat completed"
    ]
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.INFO
    assert summaries[0].fields["succeeded"] is False  # type: ignore[attr-defined]
    assert summaries[0].fields["failure_count"] == 1  # type: ignore[attr-defined]
    assert result.chats_failed == 1
    assert sensitive_detail not in repr(summaries[0].__dict__)


def test_message_skip_logs_are_debug_and_chat_summary_has_exact_counts(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=1,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(1)),
    )
    caplog.set_level(logging.DEBUG, logger=polling_service_module.__name__)

    result = WechatPollingService(
        FakeWechatClient(
            messages={
                CHAT_ID: [
                    raw_message(1),
                    raw_message(2, isSelf=True),
                    raw_message(3),
                ]
            }
        ),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    skip_records = [
        record
        for record in caplog.records
        if record.getMessage() in {"message skipped by checkpoint", "message skipped as self"}
    ]
    assert [(record.getMessage(), record.levelno) for record in skip_records] == [
        ("message skipped by checkpoint", logging.DEBUG),
        ("message skipped as self", logging.DEBUG),
    ]
    summary_records = [
        record for record in caplog.records if record.getMessage() == "poll chat completed"
    ]
    assert len(summary_records) == 1
    fields = dict(summary_records[0].__dict__["fields"])
    source_ref = fields.pop("source_account_id_ref")
    conversation_ref = fields.pop("conversation_id_ref")
    assert source_ref.startswith("source_account:sha256:")
    assert conversation_ref.startswith("conversation:sha256:")
    assert fields == {
        "succeeded": True,
        "failure_count": 0,
        "messages_seen": 3,
        "messages_processed": 1,
        "messages_new": 1,
        "messages_duplicate": 0,
        "messages_skipped_checkpoint": 1,
        "messages_skipped_self": 1,
        "messages_failed": 0,
        "messages_without_server_id": 0,
        "bootstrapped": False,
    }
    assert result.messages_skipped_as_self == 1


def test_no_server_messages_use_one_redacted_chat_summary_without_extra_warning(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_account = "wxid-sensitive-account"
    sensitive_chat = "wxid-sensitive-chat"
    sensitive_body = "private body must never appear in telemetry"
    sensitive_nickname = "private nickname must never appear in telemetry"
    caplog.set_level(logging.INFO, logger=polling_service_module.__name__)

    result = WechatPollingService(
        FakeWechatClient(
            account_id=sensitive_account,
            chats=[{"id": sensitive_chat}],
            messages={
                sensitive_chat: [
                    raw_message(
                        1,
                        chat_id=sensitive_chat,
                        serverId=None,
                        content=sensitive_body,
                        senderName=sensitive_nickname,
                    ),
                    raw_message(
                        2,
                        chat_id=sensitive_chat,
                        serverId=None,
                        content=sensitive_body,
                        senderName=sensitive_nickname,
                    ),
                ]
            },
        ),
        checkpoint_store,
        RecordingSink(),
    ).poll_once()

    assert not [
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint fallback anchor in use"
    ]
    summary_records = [
        record for record in caplog.records if record.getMessage() == "poll chat completed"
    ]
    assert len(summary_records) == 1
    fields = summary_records[0].__dict__["fields"]
    assert fields["messages_without_server_id"] == 2
    assert fields["source_account_id_ref"].startswith("source_account:sha256:")
    assert fields["conversation_id_ref"].startswith("conversation:sha256:")
    serialized = repr(summary_records[0].__dict__)
    assert sensitive_account not in serialized
    assert sensitive_chat not in serialized
    assert sensitive_body not in serialized
    assert sensitive_nickname not in serialized
    assert result.messages_without_server_id == 2


def test_repeated_unverified_continuity_logs_one_warning_until_state_changes(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(10)),
    )
    client = FakeWechatClient(
        messages={
            CHAT_ID: [
                raw_message(10),
                raw_message(10, serverId="different-anchor"),
                raw_message(11),
            ]
        }
    )
    service = WechatPollingService(client, checkpoint_store, RecordingSink())
    caplog.set_level(logging.WARNING, logger=polling_service_module.__name__)

    first = service.poll_once()
    second = service.poll_once()

    assert first.chats_failed == 1
    assert second.chats_failed == 1
    continuity_warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "checkpoint continuity unverified"
    ]
    assert len(continuity_warnings) == 1
    assert continuity_warnings[0].fields["recovery_action"] == ("stop_chat_anchor_ambiguous")


def test_latest_is_default_and_does_not_replay_visible_history(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), raw_message(1), raw_message(2)]})
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    stored = checkpoint(checkpoint_store)
    assert sink.attempts == []
    assert stored is not None and stored.last_local_id == 3
    assert result.messages_seen == 3
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 3
    assert result.bootstrapped_chats == 1
    assert result.chat_results[0].bootstrapped is True


def test_latest_only_delivers_messages_above_atomic_bootstrap_watermark(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(2), raw_message(1)]})
    sink = RecordingSink()
    service = WechatPollingService(client, checkpoint_store, sink)

    first = service.poll_once()
    client.messages[CHAT_ID] = [raw_message(3), raw_message(2), raw_message(1)]
    second = service.poll_once()

    assert first.messages_skipped_by_checkpoint == 2
    assert second.messages_processed == 1
    assert second.messages_skipped_by_checkpoint == 2
    assert source_ids(sink.handled) == ["3"]
    assert checkpoint(checkpoint_store).last_local_id == 3  # type: ignore[union-attr]


def test_latest_bootstrap_does_not_normalize_visible_history(
    checkpoint_store: WechatSyncCheckpointStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("latest bootstrap must not normalize visible history")

    monkeypatch.setattr(polling_service_module, "normalize_wechat_message", fail_if_called)
    invalid = raw_message(2)
    invalid.pop("sender")
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), invalid, raw_message(1)]})
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    stored = checkpoint(checkpoint_store)
    assert result.chats_succeeded == 1
    assert result.messages_skipped_by_checkpoint == 3
    assert stored is not None and stored.last_local_id == 3
    assert sink.attempts == []


def test_latest_bootstrap_initializes_once_at_max_without_advancing(
    session: Session,
) -> None:
    store = TrackingCheckpointStore(session)
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message("10"), raw_message(2), raw_message(3)]}
    )
    sink = RecordingSink()

    result = WechatPollingService(client, store, sink).poll_once()

    assert result.chats_succeeded == 1
    assert result.messages_skipped_by_checkpoint == 3
    assert store.initialize_calls == [10]
    assert store.advance_calls == []
    assert checkpoint(store).last_local_id == 10  # type: ignore[union-attr]
    assert sink.attempts == []


def test_latest_bootstrap_with_missing_sender_skips_history(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    history = raw_message(7)
    history.pop("sender")
    client = FakeWechatClient(messages={CHAT_ID: [history]})
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    assert result.chats_succeeded == 1
    assert result.messages_skipped_by_checkpoint == 1
    assert checkpoint(checkpoint_store).last_local_id == 7  # type: ignore[union-attr]
    assert sink.attempts == []


def test_latest_initialize_race_uses_existing_checkpoint_as_authority(session: Session) -> None:
    seed_store = WechatSyncCheckpointStore(session)
    existing, _ = seed_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=2,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(2)),
    )
    store = InitializeRaceCheckpointStore(session)
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), raw_message(2), raw_message(1)]})
    sink = RecordingSink()

    result = WechatPollingService(client, store, sink).poll_once()

    assert store.initialize_calls == [3]
    assert result.bootstrapped_chats == 0
    assert result.messages_processed == 1
    assert result.messages_skipped_by_checkpoint == 2
    assert source_ids(sink.handled) == ["3"]
    assert existing.last_local_id == 3


def test_backfill_replays_visible_history_in_order(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), raw_message(1), raw_message(2)]})
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert source_ids(sink.handled) == ["1", "2", "3"]
    assert result.messages_processed == 3
    assert result.bootstrapped_chats == 1
    assert checkpoint(checkpoint_store).last_local_id == 3  # type: ignore[union-attr]


def test_sink_failure_leaves_checkpoint_at_previous_message(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), raw_message(2), raw_message(1)]})
    sink = RecordingSink(fail_counts={"2": 1})

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert source_ids(sink.attempts) == ["1", "2"]
    assert source_ids(sink.handled) == ["1"]
    assert stored is not None and stored.last_local_id == 1
    assert result.chats_failed == 1
    assert result.messages_processed == 1
    assert result.failures[0].stage is PollFailureStage.SINK
    assert result.failures[0].local_id == 2


def test_failed_message_is_retried_on_next_poll(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), raw_message(2), raw_message(1)]})
    sink = RecordingSink(fail_counts={"2": 1})
    service = WechatPollingService(client, checkpoint_store, sink, bootstrap_mode="backfill")

    first = service.poll_once()
    second = service.poll_once()

    assert first.chats_failed == 1
    assert second.chats_succeeded == 1
    assert source_ids(sink.attempts) == ["1", "2", "2", "3"]
    assert source_ids(sink.handled) == ["1", "2", "3"]
    assert checkpoint(checkpoint_store).last_local_id == 3  # type: ignore[union-attr]


def test_sink_failure_stops_later_messages_in_same_conversation(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(1), raw_message(2), raw_message(3)]})
    sink = RecordingSink(fail_counts={"2": 2})

    WechatPollingService(client, checkpoint_store, sink, bootstrap_mode="backfill").poll_once()

    assert source_ids(sink.attempts) == ["1", "2"]
    assert "3" not in source_ids(sink.attempts)


def test_failed_conversation_does_not_block_other_conversations(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    other_chat = "wxid_bob"
    client = FakeWechatClient(
        chats=[{"id": CHAT_ID}, {"id": other_chat}],
        messages={
            CHAT_ID: [raw_message(2), raw_message(1)],
            other_chat: [raw_message(4, chat_id=other_chat)],
        },
    )
    sink = RecordingSink(fail_counts={"2": 1})

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert source_ids(sink.attempts) == ["1", "2", "4"]
    assert result.chats_seen == 2
    assert result.chats_succeeded == 1
    assert result.chats_failed == 1
    assert result.messages_seen == 3
    assert result.messages_processed == 2
    assert checkpoint(checkpoint_store).last_local_id == 1  # type: ignore[union-attr]
    other_checkpoint = checkpoint(checkpoint_store, conversation_id=other_chat)
    assert other_checkpoint is not None and other_checkpoint.last_local_id == 4


def test_duplicate_chat_entry_cannot_retry_a_failed_conversation_in_same_cycle(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    other_chat = "wxid_bob"
    client = FakeWechatClient(
        chats=[{"id": CHAT_ID}, {"id": CHAT_ID}, {"id": other_chat}],
        messages={
            CHAT_ID: [raw_message(3), raw_message(2), raw_message(1)],
            other_chat: [raw_message(4, chat_id=other_chat)],
        },
    )
    sink = RecordingSink(fail_counts={"2": 1})

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert client.list_message_calls == [CHAT_ID, other_chat]
    assert source_ids(sink.attempts) == ["1", "2", "4"]
    assert result.chats_seen == 3
    assert result.chats_succeeded == 1
    assert result.chats_failed == 2
    assert result.failures[1].stage is PollFailureStage.POLL_CHAT


@pytest.mark.parametrize("as_model", [False, True], ids=["mapping", "raw-model"])
def test_self_message_is_ignored_and_advances_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
    as_model: bool,
) -> None:
    message: RawWechatMessage | Mapping[str, Any] = raw_message(1, isSelf=True)
    if as_model:
        message = RawWechatMessage.model_validate(message)
    client = FakeWechatClient(messages={CHAT_ID: [message]})
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert sink.attempts == []
    assert result.chats_succeeded == 1
    assert result.chats_failed == 0
    assert result.messages_seen == 1
    assert result.messages_processed == 0
    assert stored is not None and stored.last_local_id == 1


def test_user_message_after_self_message_is_processed(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(
        messages={
            CHAT_ID: [
                raw_message(1, isSelf=True),
                raw_message(2, isSelf=False),
            ]
        }
    )
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert source_ids(sink.handled) == ["2"]
    assert result.chats_succeeded == 1
    assert result.messages_processed == 1
    assert stored is not None and stored.last_local_id == 2


def test_consecutive_self_messages_are_not_reprocessed(session: Session) -> None:
    store = TrackingCheckpointStore(session)
    messages: list[RawWechatMessage | Mapping[str, Any]] = [
        raw_message(1, isSelf=True),
        RawWechatMessage.model_validate(raw_message(2, isSelf=True)),
    ]
    client = FakeWechatClient(messages={CHAT_ID: messages})
    sink = RecordingSink()
    service = WechatPollingService(client, store, sink, bootstrap_mode="backfill")

    first = service.poll_once()
    second = service.poll_once()

    stored = checkpoint(store)
    assert sink.attempts == []
    assert first.chats_succeeded == 1
    assert first.messages_processed == 0
    assert second.chats_succeeded == 1
    assert second.messages_processed == 0
    assert second.messages_skipped_by_checkpoint == 2
    assert store.advance_calls == [1, 2]
    assert stored is not None and stored.last_local_id == 2


def test_system_message_is_forwarded_to_sink(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    message = raw_message(1, type=10000, content="system event")
    message.pop("sender")
    message.pop("senderName")
    client = FakeWechatClient(messages={CHAT_ID: [message]})
    sink = RecordingSink()

    WechatPollingService(client, checkpoint_store, sink, bootstrap_mode="backfill").poll_once()

    assert len(sink.handled) == 1
    assert sink.handled[0].message_type is WechatMessageType.SYSTEM
    assert sink.handled[0].sender_type is WechatSenderType.SYSTEM


def test_unmentioned_group_message_is_forwarded_to_sink(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    group_id = "engineering@chatroom"
    client = FakeWechatClient(
        chats=[{"id": group_id}],
        messages={group_id: [raw_message(1, chat_id=group_id, isMentioned=False)]},
    )
    sink = RecordingSink()

    WechatPollingService(client, checkpoint_store, sink, bootstrap_mode="backfill").poll_once()

    assert len(sink.handled) == 1
    assert sink.handled[0].is_mentioned is False


def test_arbitrary_sender_and_unknown_type_are_not_access_filtered(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(1, sender="not-on-any-list", type=987654)]}
    )
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert result.messages_processed == 1
    assert sink.handled[0].sender_id == "not-on-any-list"
    assert sink.handled[0].message_type is WechatMessageType.UNKNOWN


def test_missing_chat_id_is_a_controlled_failure(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(chats=[{"name": "Display name only"}])
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    assert result.chats_seen == 1
    assert result.chats_failed == 1
    assert result.messages_seen == 0
    assert result.failures[0].stage is PollFailureStage.PARSE_CHAT
    assert result.failures[0].code == "wechat_chat_identity_error"
    assert client.list_message_calls == []
    assert sink.attempts == []


@pytest.mark.parametrize(
    "invalid_local_id",
    [None, "not-a-number", True, 1.5, 0, -1, 2**63],
)
def test_invalid_local_id_does_not_advance_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
    invalid_local_id: object,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=1,
    )
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(invalid_local_id)]})
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    stored = checkpoint(checkpoint_store)
    assert stored is not None and stored.last_local_id == 1
    assert sink.attempts == []
    assert result.chats_failed == 1
    assert result.failures[0].stage is PollFailureStage.VALIDATE_MESSAGE


def test_corrected_local_id_preserves_latest_bootstrap_after_validation_failure(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message("not-a-number")]})
    sink = RecordingSink()
    service = WechatPollingService(client, checkpoint_store, sink)

    first = service.poll_once()
    stored = checkpoint(checkpoint_store)
    assert first.chats_failed == 1
    assert stored is None
    assert sink.attempts == []

    client.messages[CHAT_ID] = [raw_message(2), raw_message(1)]
    second = service.poll_once()

    assert second.messages_processed == 0
    assert second.messages_skipped_by_checkpoint == 2
    assert checkpoint(checkpoint_store).last_local_id == 2  # type: ignore[union-attr]
    assert sink.attempts == []


def test_latest_chat_id_mismatch_does_not_create_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(1, chat_id="wxid_other")]})
    sink = RecordingSink()

    result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    assert result.chats_failed == 1
    assert result.failures[0].stage is PollFailureStage.VALIDATE_MESSAGE
    assert result.failures[0].code == "wechat_conversation_mismatch"
    assert checkpoint(checkpoint_store) is None
    assert sink.attempts == []


def test_latest_initialize_failure_leaves_no_partial_checkpoint(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TrackingCheckpointStore(session)
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), raw_message(2), raw_message(1)]})
    sink = RecordingSink()
    original_commit = session.commit
    commit_attempts = 0

    def fail_commit() -> None:
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise RuntimeError("controlled checkpoint initialize failure")
        original_commit()

    monkeypatch.setattr(session, "commit", fail_commit)

    result = WechatPollingService(client, store, sink).poll_once()

    assert result.chats_failed == 1
    assert result.failures[0].stage is PollFailureStage.CHECKPOINT
    assert store.initialize_calls == [3]
    assert store.advance_calls == []
    assert checkpoint(store) is None
    assert sink.attempts == []

    retry = WechatPollingService(client, store, sink).poll_once()

    assert retry.chats_succeeded == 1
    assert retry.messages_skipped_by_checkpoint == 3
    assert store.initialize_calls == [3, 3]
    assert checkpoint(store).last_local_id == 3  # type: ignore[union-attr]
    assert sink.attempts == []


def test_checkpoint_store_never_moves_backward(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
    )

    returned = checkpoint_store.advance(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=7,
    )

    assert returned.last_local_id == 10
    assert checkpoint(checkpoint_store).last_local_id == 10  # type: ignore[union-attr]


@pytest.mark.parametrize("invalid_local_id", [-1, 2**63])
@pytest.mark.parametrize("operation", ["initialize", "advance"])
def test_checkpoint_store_rejects_values_outside_big_integer_range(
    checkpoint_store: WechatSyncCheckpointStore,
    operation: str,
    invalid_local_id: int,
) -> None:
    method = getattr(checkpoint_store, operation)

    with pytest.raises(WechatCheckpointValueError):
        method(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=invalid_local_id,
        )

    assert checkpoint(checkpoint_store) is None


@pytest.mark.parametrize(
    "fingerprint",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, 7],
)
def test_checkpoint_store_rejects_invalid_fingerprint(
    checkpoint_store: WechatSyncCheckpointStore,
    fingerprint: object,
) -> None:
    with pytest.raises(WechatCheckpointFingerprintError):
        checkpoint_store.initialize(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=1,
            last_message_fingerprint=fingerprint,  # type: ignore[arg-type]
        )

    assert checkpoint(checkpoint_store) is None


def test_zero_checkpoint_cannot_claim_a_message_anchor(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    with pytest.raises(WechatCheckpointFingerprintError):
        checkpoint_store.initialize(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=0,
            last_message_fingerprint="a" * 64,
        )


@pytest.mark.parametrize("generation", [-1, 2**63, True])
def test_checkpoint_store_rejects_invalid_expected_generation(
    checkpoint_store: WechatSyncCheckpointStore,
    generation: object,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=1,
    )

    with pytest.raises(WechatCheckpointGenerationError):
        checkpoint_store.advance_cas(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            expected_last_local_id=1,
            expected_generation=generation,  # type: ignore[arg-type]
            expected_message_fingerprint=None,
            last_local_id=2,
            last_message_fingerprint=checkpoint_fingerprint(raw_message(2)),
        )

    stored = checkpoint(checkpoint_store)
    assert stored is not None
    assert (stored.last_local_id, stored.regression_generation) == (1, 0)


def test_checkpoint_generation_cannot_overflow_during_rewind(
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
) -> None:
    stored, _ = checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint=checkpoint_fingerprint(raw_message(15)),
    )
    stored.regression_generation = 2**63 - 1
    session.commit()

    with pytest.raises(WechatCheckpointGenerationError):
        checkpoint_store.rewind(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            expected_last_local_id=15,
            expected_generation=2**63 - 1,
            expected_message_fingerprint=stored.last_message_fingerprint,
            last_local_id=9,
        )

    persisted = checkpoint(checkpoint_store)
    assert persisted is not None
    assert (persisted.last_local_id, persisted.regression_generation) == (15, 2**63 - 1)


def test_checkpoint_failure_after_sink_success_allows_redelivery(session: Session) -> None:
    store = TrackingCheckpointStore(session, fail_advance_once=True)
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(2), raw_message(1)]})
    sink = RecordingSink()
    service = WechatPollingService(client, store, sink, bootstrap_mode="backfill")

    first = service.poll_once()
    after_failure = checkpoint(store)

    assert first.chats_failed == 1
    assert first.messages_processed == 0
    assert first.failures[0].stage is PollFailureStage.CHECKPOINT
    assert first.failures[0].local_id == 1
    assert after_failure is not None and after_failure.last_local_id == 0

    second = service.poll_once()

    assert second.chats_succeeded == 1
    assert source_ids(sink.attempts) == ["1", "1", "2"]
    assert source_ids(sink.handled) == ["1", "1", "2"]
    assert checkpoint(store).last_local_id == 2  # type: ignore[union-attr]


def test_processed_checkpoint_is_visible_in_a_new_database_session(engine: Engine) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(1)]})
    sink = RecordingSink()

    with Session(engine, expire_on_commit=False) as first_session:
        result = WechatPollingService(
            client,
            WechatSyncCheckpointStore(first_session),
            sink,
            bootstrap_mode="backfill",
        ).poll_once()

    with Session(engine, expire_on_commit=False) as second_session:
        stored = checkpoint(WechatSyncCheckpointStore(second_session))

    assert result.messages_processed == 1
    assert stored is not None and stored.last_local_id == 1


def test_repeated_poll_is_idempotent(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(2), raw_message(1)]})
    sink = RecordingSink()
    service = WechatPollingService(client, checkpoint_store, sink, bootstrap_mode="backfill")

    first = service.poll_once()
    second = service.poll_once()

    assert first.messages_processed == 2
    assert second.messages_processed == 0
    assert second.messages_skipped_by_checkpoint == 2
    assert source_ids(sink.handled) == ["1", "2"]
    assert checkpoint(checkpoint_store).last_local_id == 2  # type: ignore[union-attr]


def test_normalization_failure_stops_chat_without_advancing(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=1,
    )
    invalid = raw_message(2)
    invalid.pop("sender")
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(3), invalid]})
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert result.chats_failed == 1
    assert result.failures[0].stage is PollFailureStage.NORMALIZE
    assert result.failures[0].local_id == 2
    assert sink.attempts == []
    assert checkpoint(checkpoint_store).last_local_id == 1  # type: ignore[union-attr]


def test_chat_id_precedes_username_and_name_is_display_only(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(
        chats=[{"id": CHAT_ID, "username": "wrong-fallback", "name": "Alice Display"}],
        messages={CHAT_ID: [raw_message(1)]},
    )
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert client.list_message_calls == [CHAT_ID]
    assert sink.handled[0].conversation_id == CHAT_ID
    assert sink.handled[0].conversation_name == "Alice Display"
    assert result.chat_results[0].conversation_name == "Alice Display"


def test_username_is_used_when_chat_id_is_missing(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(
        chats=[{"username": CHAT_ID, "name": "Alice"}],
        messages={CHAT_ID: [raw_message(1)]},
    )
    sink = RecordingSink()

    result = WechatPollingService(
        client, checkpoint_store, sink, bootstrap_mode="backfill"
    ).poll_once()

    assert result.chats_succeeded == 1
    assert client.list_message_calls == [CHAT_ID]
    assert len(sink.handled) == 1


@pytest.mark.parametrize("bootstrap_mode", ["latest", "backfill"])
def test_empty_first_poll_persists_discovery_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
    bootstrap_mode: str,
) -> None:
    client = FakeWechatClient(messages={CHAT_ID: []})
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        bootstrap_mode=bootstrap_mode,
    )

    first = service.poll_once()
    client.messages[CHAT_ID] = [raw_message(1)]
    second = service.poll_once()

    stored = checkpoint(checkpoint_store)
    assert first.bootstrapped_chats == 1
    assert stored is not None and stored.last_local_id == 1
    assert second.messages_processed == 1
    assert source_ids(sink.handled) == ["1"]


def test_failure_result_does_not_expose_exception_or_message_data(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    sensitive_token = "secret-token-that-must-not-leak"
    sensitive_body = "complete-sensitive-message-body"
    sensitive_base64 = "c2Vuc2l0aXZlLWZpbGUtY29udGVudA=="

    class SensitiveSinkError(RuntimeError):
        code = f"{sensitive_token}:{sensitive_body}:{sensitive_base64}"

    class SensitiveFailureSink:
        def handle(self, message: NormalizedWechatMessage) -> None:
            del message
            raise SensitiveSinkError(f"{sensitive_token}:{sensitive_body}:{sensitive_base64}")

    client = FakeWechatClient(messages={CHAT_ID: [raw_message(1, content=sensitive_body)]})

    result = WechatPollingService(
        client,
        checkpoint_store,
        SensitiveFailureSink(),
        bootstrap_mode="backfill",
    ).poll_once()
    serialized_result = result.model_dump_json()

    assert sensitive_token not in serialized_result
    assert sensitive_body not in serialized_result
    assert sensitive_base64 not in serialized_result
    assert result.failures[0].stage is PollFailureStage.SINK


def test_checkpoint_schema_uses_big_integer_and_account_chat_unique_key() -> None:
    assert isinstance(WechatSyncCheckpoint.__table__.c.last_local_id.type, BigInteger)
    assert isinstance(WechatSyncCheckpoint.__table__.c.regression_generation.type, BigInteger)
    fingerprint_type = WechatSyncCheckpoint.__table__.c.last_message_fingerprint.type
    assert isinstance(fingerprint_type, String)
    assert fingerprint_type.length == 64
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in WechatSyncCheckpoint.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("source_account_id", "conversation_id") in unique_column_sets
    check_constraints = {
        str(constraint.sqltext)
        for constraint in WechatSyncCheckpoint.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "last_local_id >= 0" in check_constraints
    assert "regression_generation >= 0" in check_constraints
    assert (
        "last_message_fingerprint IS NULL OR length(last_message_fingerprint) = 64"
        in check_constraints
    )


def test_database_constraint_rejects_negative_checkpoint(session: Session) -> None:
    session.add(
        WechatSyncCheckpoint(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=-1,
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    assert checkpoint(WechatSyncCheckpointStore(session)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"regression_generation": -1},
        {"last_message_fingerprint": "a" * 63},
    ],
)
def test_database_constraints_reject_invalid_checkpoint_recovery_state(
    session: Session,
    overrides: dict[str, object],
) -> None:
    session.add(
        WechatSyncCheckpoint(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=1,
            **overrides,
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    assert checkpoint(WechatSyncCheckpointStore(session)) is None


def test_invalid_bootstrap_mode_is_rejected_without_polling(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient()

    with pytest.raises(InvalidBootstrapModeError):
        WechatPollingService(client, checkpoint_store, RecordingSink(), bootstrap_mode="surprise")

    assert client.auth_calls == 0
