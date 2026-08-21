from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from sqlalchemy import BigInteger, CheckConstraint, UniqueConstraint, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat import polling_service as polling_service_module
from cf_agent_gateway.adapters.wechat.normalized_models import (
    NormalizedWechatMessage,
    WechatMessageType,
    WechatSenderType,
)
from cf_agent_gateway.adapters.wechat.polling_errors import (
    InvalidBootstrapModeError,
    WechatCheckpointConflictError,
    WechatCheckpointValueError,
)
from cf_agent_gateway.adapters.wechat.polling_models import (
    PollFailureStage,
    WechatSyncCheckpoint,
)
from cf_agent_gateway.adapters.wechat.polling_service import WechatPollingService
from cf_agent_gateway.adapters.wechat.polling_store import WechatSyncCheckpointStore
from cf_agent_gateway.adapters.wechat.raw_models import (
    AgentWechatAuthStatus,
    RawWechatMessage,
)
from cf_agent_gateway.database import create_database_engine, initialize_database

ACCOUNT_ID = "wxid_gateway"
CHAT_ID = "wxid_alice"
TIMESTAMP = "2026-08-01T10:15:00+08:00"


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

    def advance(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        last_local_id: int,
        expected_last_local_id: int,
        expected_regression_generation: int,
        last_message_fingerprint: str | None = None,
    ) -> WechatSyncCheckpoint:
        self.advance_calls.append(last_local_id)
        if self.fail_advance_once:
            self.fail_advance_once = False
            raise RuntimeError("controlled checkpoint advance failure")
        return super().advance(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            last_local_id=last_local_id,
            last_message_fingerprint=last_message_fingerprint,
            expected_last_local_id=expected_last_local_id,
            expected_regression_generation=expected_regression_generation,
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


def test_lease_guard_stops_before_sink_and_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=0,
    )
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(1)]})
    sink = RecordingSink()
    guard_calls = 0

    def lease_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise RuntimeError("worker lease lost")

    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
        lease_guard=lease_guard,
    )

    with pytest.raises(RuntimeError, match="worker lease lost"):
        service.poll_once()

    assert sink.attempts == []
    assert checkpoint(checkpoint_store).last_local_id == 0  # type: ignore[union-attr]


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=2,
    )
    client = FakeWechatClient(messages={CHAT_ID: [raw_message(2), raw_message(1)]})
    sink = RecordingSink()

    with caplog.at_level(
        logging.INFO,
        logger="cf_agent_gateway.adapters.wechat.polling_service",
    ):
        result = WechatPollingService(
            client, checkpoint_store, sink, bootstrap_mode="backfill"
        ).poll_once()

    assert sink.attempts == []
    assert result.messages_processed == 0
    assert result.messages_skipped_by_checkpoint == 2
    assert result.chats_succeeded == 1
    skipped_fields = [
        record.fields
        for record in caplog.records
        if record.name == "cf_agent_gateway.adapters.wechat.polling_service"
        and record.getMessage() == "message skipped"
    ]
    assert skipped_fields == [
        {
            "reason": "checkpoint",
            "conversation_id": CHAT_ID,
            "local_id": 1,
            "checkpoint": 2,
        },
        {
            "reason": "checkpoint",
            "conversation_id": CHAT_ID,
            "local_id": 2,
            "checkpoint": 2,
        },
    ]


def test_checkpoint_regression_replays_visible_messages(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
    )
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(12), raw_message(11), raw_message(10)]}
    )
    sink = RecordingSink()

    with caplog.at_level(
        logging.WARNING,
        logger="cf_agent_gateway.adapters.wechat.polling_service",
    ):
        result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    assert source_ids(sink.handled) == ["10", "11", "12"]
    assert result.messages_processed == 3
    assert result.messages_skipped_by_checkpoint == 0
    assert checkpoint(checkpoint_store).last_local_id == 12  # type: ignore[union-attr]
    regression = next(
        record
        for record in caplog.records
        if record.getMessage() == "wechat checkpoint regression detected"
    )
    assert regression.fields == {
        "source_account_id": ACCOUNT_ID,
        "conversation_id": CHAT_ID,
        "old_checkpoint": 15,
        "remote_latest_local_id": 12,
        "recovery_action": "rewind_to_visible_window",
    }
    recovered = next(
        record for record in caplog.records if record.getMessage() == "wechat checkpoint recovered"
    )
    assert recovered.fields["old_checkpoint"] == 15
    assert recovered.fields["new_checkpoint"] == 9
    assert recovered.fields["regression_generation"] == 1


@pytest.mark.parametrize("uses_server_id", [True, False])
def test_checkpoint_anchor_recovers_regression_after_new_session_overtakes_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
    uses_server_id: bool,
) -> None:
    old_message = raw_message(
        15,
        server_id="old-server-15" if uses_server_id else 0,
        content="old-session-anchor",
    )
    old_sink = RecordingSink()
    WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: [old_message]}),
        checkpoint_store,
        old_sink,
        bootstrap_mode="backfill",
    ).poll_once()
    stored_before_reset = checkpoint(checkpoint_store)
    assert stored_before_reset is not None
    assert stored_before_reset.last_local_id == 15
    assert stored_before_reset.last_message_fingerprint is not None

    new_messages = [
        raw_message(
            local_id,
            server_id=f"new-server-{local_id}" if uses_server_id else 0,
            content=f"new-session-{local_id}",
        )
        for local_id in range(1, 21)
    ]
    new_sink = RecordingSink()
    result = WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: new_messages}),
        checkpoint_store,
        new_sink,
        bootstrap_mode="backfill",
    ).poll_once()

    assert result.messages_processed == 20
    assert [message.content for message in new_sink.handled] == [
        f"new-session-{local_id}" for local_id in range(1, 21)
    ]
    stored_after_reset = checkpoint(checkpoint_store)
    assert stored_after_reset is not None
    assert stored_after_reset.last_local_id == 20
    assert stored_after_reset.regression_generation == 1
    assert stored_after_reset.last_message_fingerprint is not None


def test_checkpoint_anchor_does_not_recover_normal_overlapping_window(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    initial_messages = [
        raw_message(local_id, server_id=f"server-{local_id}") for local_id in range(10, 16)
    ]
    WechatPollingService(
        FakeWechatClient(messages={CHAT_ID: initial_messages}),
        checkpoint_store,
        RecordingSink(),
        bootstrap_mode="backfill",
    ).poll_once()
    initial_checkpoint = checkpoint(checkpoint_store)
    assert initial_checkpoint is not None
    assert initial_checkpoint.last_local_id == 15
    assert initial_checkpoint.last_message_fingerprint is not None

    overlapping_messages = [
        raw_message(local_id, server_id=f"server-{local_id}") for local_id in range(10, 21)
    ]
    sink = RecordingSink()
    with caplog.at_level(
        logging.WARNING,
        logger="cf_agent_gateway.adapters.wechat.polling_service",
    ):
        result = WechatPollingService(
            FakeWechatClient(messages={CHAT_ID: overlapping_messages}),
            checkpoint_store,
            sink,
            bootstrap_mode="backfill",
        ).poll_once()

    assert result.messages_processed == 5
    assert [message.source_local_id for message in sink.handled] == [
        str(local_id) for local_id in range(16, 21)
    ]
    current = checkpoint(checkpoint_store)
    assert current is not None
    assert current.last_local_id == 20
    assert current.regression_generation == 0
    assert all(
        record.getMessage() != "wechat checkpoint regression detected" for record in caplog.records
    )


def test_regression_compare_and_swap_loss_does_not_process_with_stale_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint="a" * 64,
    )

    def lose_recovery_race(**kwargs: object) -> tuple[WechatSyncCheckpoint, bool]:
        del kwargs
        session.execute(
            update(WechatSyncCheckpoint)
            .where(
                WechatSyncCheckpoint.source_account_id == ACCOUNT_ID,
                WechatSyncCheckpoint.conversation_id == CHAT_ID,
            )
            .values(
                last_local_id=16,
                last_message_fingerprint="b" * 64,
            )
        )
        session.commit()
        current = checkpoint(checkpoint_store)
        assert current is not None
        return current, False

    monkeypatch.setattr(checkpoint_store, "recover_regression", lose_recovery_race)
    sink = RecordingSink()
    result = WechatPollingService(
        FakeWechatClient(
            messages={
                CHAT_ID: [
                    raw_message(local_id, server_id=f"new-server-{local_id}")
                    for local_id in range(1, 21)
                ]
            }
        ),
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
    ).poll_once()

    assert result.chats_failed == 1
    assert result.failures[0].code == "wechat_checkpoint_regression_conflict"
    assert sink.attempts == []


def test_regression_generation_prevents_fallback_local_id_reuse(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    client = FakeWechatClient(
        messages={CHAT_ID: [raw_message(12, server_id=0, content="before restart")]}
    )
    sink = RecordingSink()
    service = WechatPollingService(
        client,
        checkpoint_store,
        sink,
        bootstrap_mode="backfill",
    )

    first = service.poll_once()
    checkpoint_store.advance(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        expected_last_local_id=12,
        expected_regression_generation=0,
    )
    client.messages[CHAT_ID] = [raw_message(12, server_id=0, content="after restart")]
    second = service.poll_once()

    assert first.messages_processed == 1
    assert second.messages_processed == 1
    assert len(sink.handled) == 2
    before_restart, after_restart = sink.handled
    assert before_restart.source_message_id.startswith("local:v1:")
    assert after_restart.source_message_id.startswith("local:v2:")
    assert after_restart.source_message_id != before_restart.source_message_id
    stored = checkpoint(checkpoint_store)
    assert stored is not None
    assert stored.last_local_id == 12
    assert stored.regression_generation == 1


def test_normal_sync_above_checkpoint_does_not_trigger_recovery(
    checkpoint_store: WechatSyncCheckpointStore,
    caplog: pytest.LogCaptureFixture,
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

    with caplog.at_level(
        logging.WARNING,
        logger="cf_agent_gateway.adapters.wechat.polling_service",
    ):
        result = WechatPollingService(client, checkpoint_store, sink).poll_once()

    assert source_ids(sink.handled) == ["11", "12", "13"]
    assert result.messages_processed == 3
    assert checkpoint(checkpoint_store).last_local_id == 13  # type: ignore[union-attr]
    assert all(
        record.getMessage() != "wechat checkpoint regression detected" for record in caplog.records
    )


def test_message_above_checkpoint_is_delivered_and_persisted(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=2,
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


def test_checkpoint_store_rejects_backward_advance(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=10,
    )

    with pytest.raises(WechatCheckpointValueError):
        checkpoint_store.advance(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=7,
            expected_last_local_id=10,
            expected_regression_generation=0,
        )

    assert checkpoint(checkpoint_store).last_local_id == 10  # type: ignore[union-attr]


def test_checkpoint_regression_recovery_uses_compare_and_swap(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
    )

    recovered_checkpoint, recovered = checkpoint_store.recover_regression(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        old_checkpoint=15,
        old_regression_generation=0,
        new_checkpoint=9,
    )
    stale_checkpoint, stale_recovered = checkpoint_store.recover_regression(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        old_checkpoint=15,
        old_regression_generation=0,
        new_checkpoint=8,
    )

    assert recovered is True
    assert recovered_checkpoint.last_local_id == 9
    assert recovered_checkpoint.regression_generation == 1
    assert stale_recovered is False
    assert stale_checkpoint.last_local_id == 9
    assert stale_checkpoint.regression_generation == 1
    assert checkpoint(checkpoint_store).last_local_id == 9  # type: ignore[union-attr]


def test_stale_generation_cannot_advance_recovered_checkpoint(
    checkpoint_store: WechatSyncCheckpointStore,
) -> None:
    checkpoint_store.initialize(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        last_local_id=15,
        last_message_fingerprint="a" * 64,
    )
    recovered_checkpoint, recovered = checkpoint_store.recover_regression(
        source_account_id=ACCOUNT_ID,
        conversation_id=CHAT_ID,
        old_checkpoint=15,
        old_regression_generation=0,
        new_checkpoint=9,
    )
    assert recovered is True
    assert recovered_checkpoint.regression_generation == 1

    with pytest.raises(WechatCheckpointConflictError):
        checkpoint_store.advance(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=16,
            last_message_fingerprint="b" * 64,
            expected_last_local_id=15,
            expected_regression_generation=0,
        )

    current = checkpoint(checkpoint_store)
    assert current is not None
    assert current.last_local_id == 9
    assert current.regression_generation == 1
    assert current.last_message_fingerprint is None


@pytest.mark.parametrize("invalid_local_id", [-1, 2**63])
@pytest.mark.parametrize("operation", ["initialize", "advance"])
def test_checkpoint_store_rejects_values_outside_big_integer_range(
    checkpoint_store: WechatSyncCheckpointStore,
    operation: str,
    invalid_local_id: int,
) -> None:
    method = getattr(checkpoint_store, operation)
    extra_arguments = (
        {
            "expected_last_local_id": 0,
            "expected_regression_generation": 0,
        }
        if operation == "advance"
        else {}
    )

    with pytest.raises(WechatCheckpointValueError):
        method(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=invalid_local_id,
            **extra_arguments,
        )

    assert checkpoint(checkpoint_store) is None


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
    generation = WechatSyncCheckpoint.__table__.c.regression_generation
    assert isinstance(generation.type, BigInteger)
    assert generation.default is not None and generation.default.arg == 0
    assert generation.server_default is not None
    assert str(generation.server_default.arg) == "0"
    fingerprint = WechatSyncCheckpoint.__table__.c.last_message_fingerprint
    assert fingerprint.type.length == 64
    assert fingerprint.nullable is True
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


def test_database_constraint_rejects_negative_regression_generation(
    session: Session,
) -> None:
    session.add(
        WechatSyncCheckpoint(
            source_account_id=ACCOUNT_ID,
            conversation_id=CHAT_ID,
            last_local_id=0,
            regression_generation=-1,
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
