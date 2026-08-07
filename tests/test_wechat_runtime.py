from __future__ import annotations

from collections.abc import Callable, Mapping
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
from cf_agent_gateway.hermes import HermesChatResult
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.runtime import (
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
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
HERMES_API_KEY = "runtime-hermes-api-key"


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


class FakeHermesClient:
    def __init__(
        self,
        *,
        assistant_content: str = "Hermes accepted the message",
        hermes_thread_id: str = "hermes-runtime-thread",
        close_error: Exception | None = None,
    ) -> None:
        self.chat_calls: list[tuple[str, str | None]] = []
        self.assistant_content = assistant_content
        self.hermes_thread_id = hermes_thread_id
        self.close_calls = 0
        self.close_error = close_error

    def chat(self, content: str, *, hermes_thread_id: str | None = None) -> HermesChatResult:
        self.chat_calls.append((content, hermes_thread_id))
        return HermesChatResult(
            assistant_content=self.assistant_content,
            hermes_thread_id=self.hermes_thread_id,
        )

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class RecordingHermesClientFactory:
    def __init__(self, client: FakeHermesClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, *, base_url: str, api_key: str, model: str) -> FakeHermesClient:
        self.calls.append((base_url, api_key, model))
        return self.client


class RecordingWechatSender:
    def __init__(self, account_id: str, factory: RecordingWechatSenderFactory) -> None:
        self.account_id = account_id
        self._factory = factory

    def send_text(self, conversation_id: str, content: str) -> None:
        self._factory.send_calls.append((self.account_id, conversation_id, content))

    def close(self) -> None:
        self._factory.closed_account_ids.append(self.account_id)


class RecordingWechatSenderFactory:
    def __init__(self) -> None:
        self.account_ids: list[str] = []
        self.send_calls: list[tuple[str, str, str]] = []
        self.closed_account_ids: list[str] = []

    def __call__(self, *, account_id: str) -> RecordingWechatSender:
        self.account_ids.append(account_id)
        return RecordingWechatSender(account_id, self)


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


def test_missing_hermes_api_key_fails_before_resource_creation() -> None:
    engine_calls = 0

    def engine_factory(url: str) -> Engine:
        del url
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("engine must not be created")

    settings = runtime_settings(
        "sqlite+pysqlite:///:memory:",
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.test",
            api_key_env=HERMES_API_KEY_ENV,
        ),
    )
    with pytest.raises(HermesAPIKeyEnvironmentError) as error:
        run_wechat_poll_once(
            settings,
            engine_factory=engine_factory,
            environment_reader=lambda name: TOKEN if name == TOKEN_ENV else None,
        )

    assert error.value.environment_variable == HERMES_API_KEY_ENV
    assert engine_calls == 0


def test_hermes_client_initialization_error_does_not_expose_api_key() -> None:
    sensitive_api_key = "hermes-key-that-must-not-appear"
    settings = runtime_settings(
        "sqlite+pysqlite:///:memory:",
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.test",
            api_key_env=HERMES_API_KEY_ENV,
        ),
    )

    def failing_factory(*, base_url: str, api_key: str, model: str) -> FakeHermesClient:
        del base_url, model
        raise RuntimeError(f"Hermes client rejected {api_key}")

    with pytest.raises(HermesClientInitializationError) as error:
        run_wechat_poll_once(
            settings,
            hermes_client_factory=failing_factory,
            environment_reader={
                TOKEN_ENV: TOKEN,
                HERMES_API_KEY_ENV: sensitive_api_key,
            }.get,
        )

    assert sensitive_api_key not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


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


def test_runtime_relays_hermes_response_to_source_wechat_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    hermes_client = FakeHermesClient()
    hermes_factory = RecordingHermesClientFactory(hermes_client)
    settings = runtime_settings(
        database_url,
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.test",
            api_key_env=HERMES_API_KEY_ENV,
            model="hermes-runtime-test",
        ),
    )
    sender_factory = RecordingWechatSenderFactory()
    outbound_factory_calls: list[tuple[str, str, str]] = []

    def recording_http_sender(
        account_id: str,
        base_url: str,
        token_env: str,
        *,
        environment_reader: Callable[[str], str | None],
    ) -> RecordingWechatSender:
        assert environment_reader(token_env) == TOKEN
        outbound_factory_calls.append((account_id, base_url, token_env))
        return sender_factory(account_id=account_id)

    monkeypatch.setattr(wechat_runtime, "WechatHttpMessageSender", recording_http_sender)

    result = run_wechat_poll_once(
        settings,
        client_factory=RecordingClientFactory(wechat_client),
        hermes_client_factory=hermes_factory,
        environment_reader={
            TOKEN_ENV: TOKEN,
            HERMES_API_KEY_ENV: HERMES_API_KEY,
        }.get,
    )

    assert result.logged_in is True
    assert result.messages_processed == 1
    assert result.chats_failed == 0
    assert result.failures == []
    assert hermes_factory.calls == [("https://hermes.test", HERMES_API_KEY, "hermes-runtime-test")]
    assert hermes_client.chat_calls[0][0] == "send this to Hermes"
    assert outbound_factory_calls == [
        (ACCOUNT_ID, "https://agent-wechat.test:6174", TOKEN_ENV),
    ]
    assert sender_factory.account_ids == [ACCOUNT_ID]
    assert sender_factory.send_calls == [
        (ACCOUNT_ID, CHAT_ID, "Hermes accepted the message"),
    ]
    assert sender_factory.closed_account_ids == [ACCOUNT_ID]
    assert wechat_client.auth_calls == 1
    assert hermes_client.close_calls == 1
    assert wechat_client.close_calls == 1

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            thread = session.scalar(select(AIThread))
            dispatch_record = session.scalar(select(HermesDispatchRecord))
            assert thread is not None
            assert dispatch_record is not None
            assert hermes_client.chat_calls[0][1] == f"v1:cf-agent-gateway:{thread.id}"
            assert thread.hermes_thread_id == "hermes-runtime-thread"
            assert dispatch_record.status is HermesDispatchStatus.SUCCESS
            assert dispatch_record.message_id == session.scalar(select(Message.id))
            assert dispatch_record.ai_thread_id == thread.id
            assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 1
    finally:
        engine.dispose()


def test_runtime_isolates_hermes_responses_between_wechat_accounts(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    account_ids = ("wxid_gateway_a", "wxid_gateway_b")
    responses = ("response for account A", "response for account B")

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

    settings = runtime_settings(
        database_url,
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.test",
            api_key_env=HERMES_API_KEY_ENV,
        ),
    )
    sender_factory = RecordingWechatSenderFactory()
    results = []
    wechat_clients = []

    for index, (account_id, assistant_content) in enumerate(
        zip(account_ids, responses, strict=True)
    ):
        wechat_client = FakeWechatClient(
            {CHAT_ID: [raw_message(1, content=f"message from {account_id}")]},
            account_id=account_id,
        )
        hermes_client = FakeHermesClient(
            assistant_content=assistant_content,
            hermes_thread_id=f"hermes-runtime-thread-{index}",
        )
        results.append(
            run_wechat_poll_once(
                settings,
                client_factory=RecordingClientFactory(wechat_client),
                hermes_client_factory=RecordingHermesClientFactory(hermes_client),
                sender_factory=sender_factory,
                environment_reader={
                    TOKEN_ENV: TOKEN,
                    HERMES_API_KEY_ENV: HERMES_API_KEY,
                }.get,
            )
        )
        wechat_clients.append(wechat_client)

    assert [result.source_account_id for result in results] == list(account_ids)
    assert [result.messages_processed for result in results] == [1, 1]
    assert [result.chats_failed for result in results] == [0, 0]
    assert [result.failures for result in results] == [[], []]
    assert sender_factory.account_ids == list(account_ids)
    assert sender_factory.send_calls == [
        (account_ids[0], CHAT_ID, responses[0]),
        (account_ids[1], CHAT_ID, responses[1]),
    ]
    assert sender_factory.closed_account_ids == list(account_ids)
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
            assert stored_account_ids == list(account_ids)
            assert checkpoint_account_ids == list(account_ids)
    finally:
        engine.dispose()


def test_hermes_cleanup_error_does_not_replace_successful_poll_result(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    wechat_client = FakeWechatClient({})
    close_error = RuntimeError(f"cleanup leaked {HERMES_API_KEY}")
    hermes_client = FakeHermesClient(close_error=close_error)

    result = run_wechat_poll_once(
        runtime_settings(
            database_url,
            hermes=HermesSettings(
                enabled=True,
                base_url="https://hermes.test",
                api_key_env=HERMES_API_KEY_ENV,
            ),
        ),
        client_factory=RecordingClientFactory(wechat_client),
        hermes_client_factory=RecordingHermesClientFactory(hermes_client),
        environment_reader={
            TOKEN_ENV: TOKEN,
            HERMES_API_KEY_ENV: HERMES_API_KEY,
        }.get,
    )

    assert result.logged_in is True
    assert hermes_client.close_calls == 1
    assert wechat_client.close_calls == 1


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
