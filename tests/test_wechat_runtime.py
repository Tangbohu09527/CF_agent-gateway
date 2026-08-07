from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.adapters.wechat import (
    AgentWechatAuthStatus,
    RawWechatMessage,
    WechatSyncCheckpoint,
)
from cf_agent_gateway.config import (
    DatabaseSettings,
    HermesSettings,
    RuntimeSettings,
    Settings,
    WechatSettings,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import HermesDispatchResponse
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.runtime import (
    WechatClientInitializationError,
    WechatPollingExecutionError,
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
    run_wechat_poll_once,
)
from cf_agent_gateway.runtime import wechat as wechat_runtime
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace

ACCOUNT_ID = "wxid_gateway"
CHAT_ID = "wxid_alice"
TOKEN_ENV = "TEST_WECHAT_RUNTIME_TOKEN"
TOKEN = "runtime-test-token"
HERMES_API_KEY_ENV = "TEST_HERMES_RUNTIME_API_KEY"


def raw_message(local_id: int, *, content: str | None = None) -> dict[str, Any]:
    return {
        "localId": local_id,
        "serverId": local_id,
        "chatId": CHAT_ID,
        "sender": "wxid_sender",
        "senderName": "Sender",
        "type": 1,
        "content": content or f"message-{local_id}",
        "timestamp": "2026-08-01T10:15:00+08:00",
    }


class FakeWechatClient:
    def __init__(
        self,
        messages: Mapping[str, list[RawWechatMessage | Mapping[str, Any]]],
        *,
        account_id: str = ACCOUNT_ID,
        logged_in: bool = True,
    ) -> None:
        self.messages = dict(messages)
        self.account_id = account_id
        self.logged_in = logged_in
        self.auth_calls = 0
        self.close_calls = 0

    def get_auth_status(self) -> AgentWechatAuthStatus:
        self.auth_calls += 1
        return AgentWechatAuthStatus(
            status="logged_in" if self.logged_in else "logged_out",
            loggedInUser=self.account_id if self.logged_in else None,
        )

    def list_chats(self) -> list[dict[str, Any]]:
        return [{"id": CHAT_ID, "name": "Alice"}]

    def list_messages(self, chat_id: str) -> list[RawWechatMessage | Mapping[str, Any]]:
        return self.messages.get(chat_id, [])

    def close(self) -> None:
        self.close_calls += 1


class RecordingClientFactory:
    def __init__(self, client: FakeWechatClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, base_url: str, token: str) -> FakeWechatClient:
        self.calls.append((base_url, token))
        return self.client


def runtime_settings(
    database_url: str,
    *,
    bootstrap_mode: str = "backfill",
    hermes: HermesSettings | None = None,
    v2_routing_enabled: bool = False,
) -> Settings:
    return Settings(
        database=DatabaseSettings(url=database_url),
        runtime=RuntimeSettings(v2_routing_enabled=v2_routing_enabled),
        wechat=WechatSettings(
            enabled=True,
            base_url="https://agent-wechat.test:6174",
            bootstrap_mode=bootstrap_mode,  # type: ignore[arg-type]
            token_env=TOKEN_ENV,
        ),
        hermes=hermes if hermes is not None else HermesSettings(),
    )


def test_disabled_runtime_does_not_read_environment_or_create_client() -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"unexpected runtime access: {args!r} {kwargs!r}")

    with pytest.raises(WechatRuntimeDisabledError):
        run_wechat_poll_once(
            Settings(),
            client_factory=forbidden,  # type: ignore[arg-type]
            engine_factory=forbidden,
            environment_reader=forbidden,
        )


def test_missing_token_environment_fails_closed_before_resource_creation() -> None:
    engine_calls = 0

    def engine_factory(url: str) -> Engine:
        del url
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("engine must not be created")

    with pytest.raises(WechatTokenEnvironmentError) as error:
        run_wechat_poll_once(
            runtime_settings("sqlite+pysqlite:///:memory:"),
            engine_factory=engine_factory,
            environment_reader=lambda name: None,
        )

    assert error.value.environment_variable == TOKEN_ENV
    assert TOKEN_ENV in str(error.value)
    assert TOKEN not in str(error.value)
    assert engine_calls == 0


def test_polling_does_not_require_or_initialize_hermes(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    settings = runtime_settings(
        database_url,
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.test",
            api_key_env=HERMES_API_KEY_ENV,
        ),
    )
    client = FakeWechatClient({})
    environment_reads: list[str] = []

    def environment_reader(name: str) -> str | None:
        environment_reads.append(name)
        return TOKEN if name == TOKEN_ENV else None

    def forbidden_inline_resource(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"inline Hermes resource initialized: {args!r} {kwargs!r}")

    result = run_wechat_poll_once(
        settings,
        client_factory=RecordingClientFactory(client),
        hermes_client_factory=forbidden_inline_resource,  # type: ignore[arg-type]
        sender_factory=forbidden_inline_resource,  # type: ignore[arg-type]
        environment_reader=environment_reader,
    )

    assert result.logged_in is True
    assert environment_reads == [TOKEN_ENV]
    assert client.close_calls == 1


def test_client_initialization_error_does_not_expose_token() -> None:
    sensitive_token = "token-that-must-not-appear"

    def failing_client_factory(*, base_url: str, token: str) -> FakeWechatClient:
        del base_url
        raise RuntimeError(f"client rejected {token}")

    with pytest.raises(WechatClientInitializationError) as error:
        run_wechat_poll_once(
            runtime_settings("sqlite+pysqlite:///:memory:"),
            client_factory=failing_client_factory,
            environment_reader=lambda name: sensitive_token,
        )

    assert sensitive_token not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize("poll_fails", [False, True])
@pytest.mark.parametrize("v2_routing_enabled", [False, True])
def test_runtime_assembly_and_cleanup_order(
    monkeypatch: pytest.MonkeyPatch,
    poll_fails: bool,
    v2_routing_enabled: bool,
) -> None:
    events: list[str] = []
    expected_routing_flag = v2_routing_enabled
    checkpoint_store_marker = object()
    sink_marker = object()

    class TrackingEngine:
        def dispose(self) -> None:
            events.append("engine.dispose")

    class TrackingSession:
        def close(self) -> None:
            events.append("checkpoint_session.close")

    class TrackingClient:
        def close(self) -> None:
            events.append("client.close")

    engine = TrackingEngine()
    checkpoint_session = TrackingSession()
    client = TrackingClient()

    def engine_factory(url: str) -> TrackingEngine:
        assert url == "sqlite+pysqlite:///:memory:"
        events.append("engine")
        return engine

    def initialize_database(candidate: object) -> None:
        assert candidate is engine
        events.append("initialize_database")

    def session_factory_builder(candidate: object) -> Any:
        assert candidate is engine
        events.append("session_factory")

        def create_checkpoint_session() -> TrackingSession:
            events.append("checkpoint_session")
            return checkpoint_session

        return create_checkpoint_session

    def checkpoint_store_factory(session: object) -> object:
        assert session is checkpoint_session
        events.append("checkpoint_store")
        return checkpoint_store_marker

    def sink_factory(factory: object, *, v2_routing_enabled: bool) -> object:
        assert callable(factory)
        assert v2_routing_enabled is expected_routing_flag
        events.append("sink")
        return sink_marker

    def client_factory(*, base_url: str, token: str) -> TrackingClient:
        assert base_url == "https://agent-wechat.test:6174"
        assert token == TOKEN
        events.append("client")
        return client

    class TrackingPollingService:
        def __init__(
            self,
            runtime_client: object,
            checkpoint_store: object,
            sink: object,
            *,
            bootstrap_mode: str,
        ) -> None:
            assert runtime_client is client
            assert checkpoint_store is checkpoint_store_marker
            assert sink is sink_marker
            assert bootstrap_mode == "backfill"
            events.append("polling_service")

        def poll_once(self) -> Any:
            events.append("poll_once")
            if poll_fails:
                raise RuntimeError("sensitive unexpected poll failure")
            return wechat_runtime.PollResult(logged_in=True)

    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setattr(wechat_runtime, "initialize_database", initialize_database)
    monkeypatch.setattr(
        wechat_runtime,
        "create_database_session_factory",
        session_factory_builder,
    )
    monkeypatch.setattr(wechat_runtime, "WechatSyncCheckpointStore", checkpoint_store_factory)
    monkeypatch.setattr(
        wechat_runtime,
        "SessionFactoryMessageStoreAdmissionSink",
        sink_factory,
    )
    monkeypatch.setattr(wechat_runtime, "WechatPollingService", TrackingPollingService)

    settings = runtime_settings(
        "sqlite+pysqlite:///:memory:",
        v2_routing_enabled=v2_routing_enabled,
    )
    if poll_fails:
        with pytest.raises(WechatPollingExecutionError) as error:
            run_wechat_poll_once(
                settings,
                client_factory=client_factory,  # type: ignore[arg-type]
                engine_factory=engine_factory,  # type: ignore[arg-type]
            )
        assert error.value.__cause__ is None
        assert error.value.__context__ is None
    else:
        result = run_wechat_poll_once(
            settings,
            client_factory=client_factory,  # type: ignore[arg-type]
            engine_factory=engine_factory,  # type: ignore[arg-type]
        )
        assert result.logged_in is True

    assert events == [
        "engine",
        "initialize_database",
        "session_factory",
        "checkpoint_session",
        "checkpoint_store",
        "sink",
        "client",
        "polling_service",
        "poll_once",
        "client.close",
        "checkpoint_session.close",
        "engine.dispose",
    ]


def test_production_poll_uses_read_only_database_check_before_external_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class TrackingEngine:
        def dispose(self) -> None:
            events.append("engine.dispose")

    engine = TrackingEngine()

    def engine_factory(url: str) -> TrackingEngine:
        assert url == "sqlite+pysqlite:///:memory:"
        events.append("engine")
        return engine

    def check_database(candidate: object) -> None:
        assert candidate is engine
        events.append("check_database")
        raise RuntimeError("database migration is stale")

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"unexpected runtime access: {args!r} {kwargs!r}")

    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setattr(wechat_runtime, "database_startup_check_enabled", lambda: True)
    monkeypatch.setattr(wechat_runtime, "check_database_migrations", check_database)
    monkeypatch.setattr(wechat_runtime, "initialize_database", forbidden)

    with pytest.raises(RuntimeError, match="database migration is stale"):
        run_wechat_poll_once(
            runtime_settings("sqlite+pysqlite:///:memory:"),
            client_factory=forbidden,  # type: ignore[arg-type]
            engine_factory=engine_factory,  # type: ignore[arg-type]
        )

    assert events == ["engine", "check_database", "engine.dispose"]


def test_runtime_wires_client_checkpoint_store_and_admission_sink(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    client = FakeWechatClient({CHAT_ID: [raw_message(1)]})
    client_factory = RecordingClientFactory(client)

    result = run_wechat_poll_once(
        runtime_settings(database_url),
        client_factory=client_factory,
        environment_reader=lambda name: TOKEN if name == TOKEN_ENV else None,
    )

    assert result.logged_in is True
    assert result.messages_processed == 1
    assert result.chats_failed == 0
    assert client_factory.calls == [("https://agent-wechat.test:6174", TOKEN)]
    assert client.close_calls == 1

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            message = session.scalar(select(Message))
            checkpoint = session.scalar(select(WechatSyncCheckpoint))
            assert message is not None and message.source_local_id == "1"
            assert checkpoint is not None and checkpoint.last_local_id == 1
    finally:
        engine.dispose()


def test_runtime_archives_admits_and_enqueues_without_inline_hermes(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    engine = create_database_engine(database_url)
    initialize_database(engine)
    session_factory = create_database_session_factory(engine)
    try:
        with session_factory() as session:
            identity_service = IdentityService(session)
            identity = identity_service.create_identity(employee_id="employee-runtime")
            identity_service.create_mapping(
                platform="wechat",
                account_id=ACCOUNT_ID,
                sender_id="wxid_sender",
                enterprise_identity_id=identity.id,
            )
            policy_service = AccessPolicyService(session)
            policy_service.upsert_user_policy(enterprise_identity_id=identity.id)
            policy_service.upsert_gateway_policy(
                allowed_risk_levels=(RiskLevel.NORMAL,),
            )
    finally:
        engine.dispose()

    wechat_client = FakeWechatClient({CHAT_ID: [raw_message(1, content="send this to Hermes")]})
    settings = runtime_settings(
        database_url,
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.test",
            api_key_env=HERMES_API_KEY_ENV,
            model="hermes-runtime-test",
        ),
    )
    environment_reads: list[str] = []

    def environment_reader(name: str) -> str | None:
        environment_reads.append(name)
        return TOKEN if name == TOKEN_ENV else None

    def forbidden_inline_resource(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"inline Hermes resource initialized: {args!r} {kwargs!r}")

    result = run_wechat_poll_once(
        settings,
        client_factory=RecordingClientFactory(wechat_client),
        hermes_client_factory=forbidden_inline_resource,  # type: ignore[arg-type]
        sender_factory=forbidden_inline_resource,  # type: ignore[arg-type]
        environment_reader=environment_reader,
    )

    assert result.logged_in is True
    assert result.messages_processed == 1
    assert result.chats_failed == 0
    assert result.failures == []
    assert environment_reads == [TOKEN_ENV]
    assert wechat_client.auth_calls == 1
    assert wechat_client.close_calls == 1

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            message = session.scalar(select(Message))
            thread = session.scalar(select(AIThread))
            dispatch_record = session.scalar(select(HermesDispatchRecord))
            assert message is not None
            assert message.content == "send this to Hermes"
            assert thread is not None
            assert dispatch_record is not None
            assert thread.hermes_thread_id is None
            assert dispatch_record.status is HermesDispatchStatus.QUEUED
            assert dispatch_record.attempt_count == 0
            assert dispatch_record.claim_token is None
            assert dispatch_record.claimed_at is None
            assert dispatch_record.lease_expires_at is None
            assert dispatch_record.completed_at is None
            assert dispatch_record.message_id == message.id
            assert dispatch_record.ai_thread_id == thread.id
            assert session.scalar(select(func.count()).select_from(HermesDispatchResponse)) == 0
            assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 1
    finally:
        engine.dispose()


def test_runtime_queues_dispatches_independently_between_wechat_accounts(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    account_ids = ("wxid_gateway_a", "wxid_gateway_b")

    engine = create_database_engine(database_url)
    initialize_database(engine)
    session_factory = create_database_session_factory(engine)
    try:
        with session_factory() as session:
            identity_service = IdentityService(session)
            policy_service = AccessPolicyService(session)
            for index, account_id in enumerate(account_ids):
                identity = identity_service.create_identity(employee_id=f"employee-runtime-{index}")
                identity_service.create_mapping(
                    platform="wechat",
                    account_id=account_id,
                    sender_id="wxid_sender",
                    enterprise_identity_id=identity.id,
                )
                policy_service.upsert_user_policy(enterprise_identity_id=identity.id)
            policy_service.upsert_gateway_policy(
                allowed_risk_levels=(RiskLevel.NORMAL,),
            )
    finally:
        engine.dispose()

    settings = runtime_settings(database_url)
    results = []
    wechat_clients = []

    def forbidden_inline_resource(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"inline Hermes resource initialized: {args!r} {kwargs!r}")

    for account_id in account_ids:
        wechat_client = FakeWechatClient(
            {CHAT_ID: [raw_message(1, content=f"message from {account_id}")]},
            account_id=account_id,
        )
        results.append(
            run_wechat_poll_once(
                settings,
                client_factory=RecordingClientFactory(wechat_client),
                hermes_client_factory=forbidden_inline_resource,  # type: ignore[arg-type]
                sender_factory=forbidden_inline_resource,  # type: ignore[arg-type]
                environment_reader=lambda name: TOKEN if name == TOKEN_ENV else None,
            )
        )
        wechat_clients.append(wechat_client)

    assert [result.source_account_id for result in results] == list(account_ids)
    assert [result.messages_processed for result in results] == [1, 1]
    assert [result.chats_failed for result in results] == [0, 0]
    assert [result.failures for result in results] == [[], []]
    assert [client.auth_calls for client in wechat_clients] == [1, 1]
    assert [client.close_calls for client in wechat_clients] == [1, 1]

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            stored_account_ids = session.scalars(
                select(Message.source_account_id).order_by(Message.source_account_id)
            ).all()
            checkpoint_account_ids = session.scalars(
                select(WechatSyncCheckpoint.source_account_id).order_by(
                    WechatSyncCheckpoint.source_account_id
                )
            ).all()
            dispatch_rows = session.execute(
                select(
                    Message.source_account_id,
                    HermesDispatchRecord.status,
                    HermesDispatchRecord.attempt_count,
                )
                .join(HermesDispatchRecord, HermesDispatchRecord.message_id == Message.id)
                .order_by(Message.source_account_id)
            ).all()
            assert stored_account_ids == list(account_ids)
            assert checkpoint_account_ids == list(account_ids)
            assert dispatch_rows == [
                (account_ids[0], HermesDispatchStatus.QUEUED, 0),
                (account_ids[1], HermesDispatchStatus.QUEUED, 0),
            ]
            assert session.scalars(select(AIThread.hermes_thread_id)).all() == [None, None]
            assert session.scalar(select(func.count()).select_from(HermesDispatchResponse)) == 0
    finally:
        engine.dispose()


def test_latest_bootstrap_then_processes_only_new_messages(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    settings = runtime_settings(database_url, bootstrap_mode="latest")
    first_client = FakeWechatClient({CHAT_ID: [raw_message(2), raw_message(1)]})

    first = run_wechat_poll_once(
        settings,
        client_factory=RecordingClientFactory(first_client),
        environment_reader=lambda name: TOKEN,
    )

    assert first.bootstrapped_chats == 1
    assert first.messages_processed == 0
    assert first.messages_skipped_by_checkpoint == 2

    second_client = FakeWechatClient({CHAT_ID: [raw_message(3), raw_message(2), raw_message(1)]})
    second = run_wechat_poll_once(
        settings,
        client_factory=RecordingClientFactory(second_client),
        environment_reader=lambda name: TOKEN,
    )

    assert second.bootstrapped_chats == 0
    assert second.messages_processed == 1
    assert second.messages_skipped_by_checkpoint == 2

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Message)) == 1
            message = session.scalar(select(Message))
            checkpoint = session.scalar(select(WechatSyncCheckpoint))
            assert message is not None and message.source_local_id == "3"
            assert checkpoint is not None and checkpoint.last_local_id == 3
    finally:
        engine.dispose()
