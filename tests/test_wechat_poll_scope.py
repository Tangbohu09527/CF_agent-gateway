from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.adapters.wechat import AgentWechatAuthStatus, WechatSyncCheckpoint
from cf_agent_gateway.adapters.wechat.polling_service import (
    WechatPollingLifecycleState,
    WechatPollingService,
)
from cf_agent_gateway.adapters.wechat.polling_store import WechatSyncCheckpointStore
from cf_agent_gateway.config import WechatSettings, load_settings
from cf_agent_gateway.database import create_database_engine, initialize_database
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import MessageAdmissionService, MessageStoreAdmissionSink
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.runtime.wechat import run_wechat_poll_once
from cf_agent_gateway.task.model import HermesDispatchRecord

ACCOUNT = "wxid_scoped_account"
TARGET = "wxid_scoped_conversation"
OTHER = "wxid_other_conversation"
SCOPE = {"poll_source_account_id": ACCOUNT, "poll_conversation_id": TARGET}


def raw_message(chat_id: str, local_id: int = 1) -> dict[str, Any]:
    return {
        "localId": local_id,
        "serverId": local_id,
        "chatId": chat_id,
        "sender": "wxid_sender",
        "senderName": "Sender",
        "type": 1,
        "content": f"isolated message {local_id}",
        "timestamp": "2026-10-18T08:00:00+08:00",
    }


class Client:
    def __init__(self, chats: list[dict[str, Any]], *, account: str = ACCOUNT) -> None:
        self.account = account
        self.chats = chats
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.chats_calls = 0
        self.message_calls: list[str] = []
        self.close_calls = 0

    def get_auth_status(self) -> AgentWechatAuthStatus:
        return AgentWechatAuthStatus(status="logged_in", loggedInUser=self.account)

    def list_chats(self) -> list[dict[str, Any]]:
        self.chats_calls += 1
        return self.chats

    def list_messages(self, chat_id: str) -> list[dict[str, Any]]:
        self.message_calls.append(chat_id)
        return self.messages.get(chat_id, [raw_message(chat_id)])

    def close(self) -> None:
        self.close_calls += 1


class TrackingStore(WechatSyncCheckpointStore):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.accesses: list[tuple[str, str, str]] = []

    def get(self, *, source_account_id: str, conversation_id: str) -> Any:
        self.accesses.append(("get", source_account_id, conversation_id))
        return super().get(source_account_id=source_account_id, conversation_id=conversation_id)

    def initialize(self, **kwargs: Any) -> Any:
        self.accesses.append(("initialize", kwargs["source_account_id"], kwargs["conversation_id"]))
        return super().initialize(**kwargs)

    def advance_cas(self, **kwargs: Any) -> Any:
        self.accesses.append(
            ("advance_cas", kwargs["source_account_id"], kwargs["conversation_id"])
        )
        return super().advance_cas(**kwargs)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        engine.dispose()


def admit_sender(session: Session) -> None:
    identities = IdentityService(session)
    identity = identities.create_identity(employee_id="isolated-scope-employee")
    identities.create_mapping(
        platform="wechat",
        account_id=ACCOUNT,
        sender_id="wxid_sender",
        enterprise_identity_id=identity.id,
    )
    policies = AccessPolicyService(session)
    policies.upsert_user_policy(enterprise_identity_id=identity.id)
    policies.upsert_gateway_policy(allowed_risk_levels=(RiskLevel.NORMAL,))


def service(session: Session, client: Client, **kwargs: Any) -> tuple[Any, TrackingStore]:
    store = TrackingStore(session)
    sink = MessageStoreAdmissionSink(MessageAdmissionService(session))
    return WechatPollingService(client, store, sink, bootstrap_mode="backfill", **kwargs), store


@pytest.mark.parametrize(
    "scope",
    [
        {"poll_source_account_id": ACCOUNT},
        {"poll_conversation_id": TARGET},
        {**SCOPE, "poll_source_account_id": ""},
        {**SCOPE, "poll_source_account_id": 42},
        {**SCOPE, "poll_source_account_id": True},
        {**SCOPE, "poll_conversation_id": []},
        {**SCOPE, "poll_conversation_id": " " + TARGET},
        {**SCOPE, "poll_conversation_id": TARGET + " "},
        {**SCOPE, "poll_source_account_id": ACCOUNT + "\n"},
        {**SCOPE, "poll_conversation_id": "has space"},
        {**SCOPE, "poll_conversation_id": "has\x7fcontrol"},
        {**SCOPE, "poll_conversation_id": "has\x00control"},
        {**SCOPE, "poll_conversation_id": "x" * 256},
    ],
)
def test_invalid_scope_fails_closed_at_settings_and_service(scope: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="exact account and conversation pair"):
        WechatSettings(**scope)
    with pytest.raises(ValueError, match="exact account and conversation pair"):
        WechatPollingService(object(), object(), object(), **scope)


def test_scope_values_are_not_in_settings_repr() -> None:
    settings = WechatSettings(**SCOPE)
    assert settings.poll_source_account_id == ACCOUNT
    assert settings.poll_conversation_id == TARGET
    assert ACCOUNT not in repr(settings)
    assert TARGET not in repr(settings)


def test_22_chats_only_target_reads_messages_persists_and_advances(session: Session) -> None:
    admit_sender(session)
    seed = WechatSyncCheckpointStore(session)
    seed.initialize(source_account_id=ACCOUNT, conversation_id=OTHER, last_local_id=73)
    seed.initialize(
        source_account_id="wxid_other_account", conversation_id=TARGET, last_local_id=91
    )
    before = {
        row.id: (
            row.last_local_id,
            row.regression_generation,
            row.last_message_fingerprint,
            row.updated_at,
        )
        for row in session.scalars(select(WechatSyncCheckpoint))
    }
    chats = [{"id": OTHER}] + [{"id": f"wxid_other_{i}"} for i in range(20)]
    chats.insert(11, {"id": 12345, "userName": TARGET})
    client = Client(chats)
    poller, store = service(session, client, **SCOPE)

    result = poller.poll_once()

    assert result.chats_seen == 22
    assert result.chats_succeeded == 1
    assert result.failures == []
    assert result.messages_new == 1
    assert client.message_calls == [TARGET]
    assert store.accesses
    assert {(account, chat) for _, account, chat in store.accesses} == {(ACCOUNT, TARGET)}
    messages = list(session.scalars(select(Message)))
    assert len(messages) == 1
    assert messages[0].source_account_id == ACCOUNT
    assert messages[0].conversation_id == TARGET
    dispatches = list(session.scalars(select(HermesDispatchRecord)))
    assert len(dispatches) == 1 and dispatches[0].message_id == messages[0].id
    rows = list(session.scalars(select(WechatSyncCheckpoint)))
    assert len(rows) == 3
    assert {
        row.id: (
            row.last_local_id,
            row.regression_generation,
            row.last_message_fingerprint,
            row.updated_at,
        )
        for row in rows
        if row.id in before
    } == before
    assert next(row.last_local_id for row in rows if row.id not in before) == 1


def test_wrong_account_has_no_chat_message_checkpoint_or_ingestion_access(session: Session) -> None:
    client = Client([{"id": TARGET}], account="wxid_wrong_account")
    poller, store = service(session, client, **SCOPE)
    result = poller.poll_once()
    assert [item.code for item in result.failures] == ["wechat_poll_scope_account_mismatch"]
    assert result.source_account_id is None
    assert client.chats_calls == 0
    assert client.message_calls == []
    assert store.accesses == []
    assert list(session.scalars(select(Message))) == []
    assert list(session.scalars(select(WechatSyncCheckpoint))) == []


def test_target_absent_does_no_message_or_checkpoint_work(session: Session) -> None:
    client = Client([{"id": OTHER}])
    poller, store = service(session, client, **SCOPE)
    result = poller.poll_once()
    assert result.chats_seen == 1 and result.chats_succeeded == 0
    assert result.failures == []
    assert client.message_calls == store.accesses == []
    assert list(session.scalars(select(Message))) == []


@pytest.mark.parametrize(
    "chat",
    [
        {"id": TARGET, "chatId": OTHER},
        {"id": 42},
        {},
        {"id": None, "name": TARGET},
    ],
)
def test_ambiguous_or_missing_chat_identity_never_reads_messages(
    session: Session,
    chat: dict[str, Any],
) -> None:
    client = Client([chat])
    poller, store = service(session, client, **SCOPE)
    result = poller.poll_once()
    assert result.chats_failed == 1
    assert len(result.failures) == 1
    assert result.failures[0].stage.value == "parse_chat"
    assert client.message_calls == store.accesses == []


def test_default_scope_keeps_all_chats_and_alias_behavior(session: Session) -> None:
    client = Client([{"id": TARGET, "chatId": TARGET}, {"username": OTHER}])
    poller, store = service(session, client)
    result = poller.poll_once()
    assert WechatSettings().poll_source_account_id is None
    assert WechatSettings().poll_conversation_id is None
    assert result.chats_succeeded == result.messages_new == 2
    assert client.message_calls == [TARGET, OTHER]
    assert {chat for _, _, chat in store.accesses} == {TARGET, OTHER}


@pytest.mark.parametrize("shared_lifecycle", [False, True])
def test_config_reload_and_new_runtime_preserve_scope_and_natural_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_lifecycle: bool,
) -> None:
    monkeypatch.delenv("CF_AGENT_GATEWAY_DATABASE_URL", raising=False)
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'scope.db').as_posix()}"
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database": {"url": database_url},
                "wechat": {"enabled": True, "bootstrap_mode": "backfill", **SCOPE},
            }
        ),
        encoding="utf-8",
    )
    engine = create_database_engine(database_url)
    initialize_database(engine)
    with Session(engine) as session:
        admit_sender(session)
    engine.dispose()

    for final_id in (1, 2):
        # A new settings object, client, engine and poller represent runtime reconstruction.
        settings = load_settings(path)
        assert settings.wechat.poll_source_account_id == ACCOUNT
        assert settings.wechat.poll_conversation_id == TARGET
        client = Client([{"id": OTHER}, {"chat_id": TARGET}])
        client.messages[TARGET] = [raw_message(TARGET, i) for i in range(1, final_id + 1)]
        result = run_wechat_poll_once(
            settings,
            client_factory=lambda client=client, **kwargs: client,
            environment_reader=lambda name, token_env=settings.wechat.token_env: (
                "isolated-test-token" if name == token_env else None
            ),
            lifecycle_state=WechatPollingLifecycleState() if shared_lifecycle else None,
        )
        assert result.messages_new == 1
        assert result.failures == []
        assert client.message_calls == [TARGET]
        assert client.close_calls == 1

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            messages = list(session.scalars(select(Message)))
            assert len(messages) == 2
            assert {message.conversation_id for message in messages} == {TARGET}
            assert len(list(session.scalars(select(HermesDispatchRecord)))) == 2
            rows = list(session.scalars(select(WechatSyncCheckpoint)))
            assert len(rows) == 1
            assert (rows[0].source_account_id, rows[0].conversation_id, rows[0].last_local_id) == (
                ACCOUNT,
                TARGET,
                2,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "scope",
    [
        {"poll_source_account_id": ACCOUNT},
        {**SCOPE, "poll_conversation_id": TARGET + " "},
    ],
)
def test_yaml_scope_validation_does_not_silently_disable_or_normalize(
    tmp_path: Path,
    scope: dict[str, Any],
) -> None:
    path = tmp_path / "invalid-scope.yaml"
    path.write_text(yaml.safe_dump({"wechat": scope}), encoding="utf-8")
    with pytest.raises(ValueError, match="exact account and conversation pair") as error:
        load_settings(path)
    assert ACCOUNT not in str(error.value)
    assert TARGET not in str(error.value)


def test_scope_prunes_foreign_lifecycle_and_account_drift_invalidates_all(session: Session) -> None:
    lifecycle = WechatPollingLifecycleState()
    lifecycle.observe_account(ACCOUNT)
    for chat_id in (TARGET, OTHER):
        lifecycle.record_visible_window(
            source_account_id=ACCOUNT,
            conversation_id=chat_id,
            local_ids=(1,),
        )
    assert lifecycle.cached_chat_count == 2
    client = Client([{"id": OTHER}, {"id": TARGET}])
    poller, store = service(session, client, lifecycle_state=lifecycle, **SCOPE)
    assert poller.poll_once().failures == []
    # Normal target advancement clears its temporary marker; pruning drops the foreign one.
    assert lifecycle.cached_chat_count == 0
    assert client.message_calls == [TARGET]
    lifecycle.record_visible_window(
        source_account_id=ACCOUNT,
        conversation_id=TARGET,
        local_ids=(1,),
    )
    assert lifecycle.cached_chat_count == 1
    before = list(store.accesses)
    client.account = "wxid_changed_account"
    result = poller.poll_once()
    assert [item.code for item in result.failures] == ["wechat_poll_scope_account_mismatch"]
    assert lifecycle.cached_chat_count == 0
    assert all(count == 0 for count in lifecycle.observation_counts().values())
    assert client.chats_calls == 1
    assert client.message_calls == [TARGET]
    assert store.accesses == before
