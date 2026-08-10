from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.adapters.wechat import (
    AgentWechatAuthStatus,
    PollResult,
    RawWechatMessage,
    WechatSyncCheckpoint,
)
from cf_agent_gateway.agent_profile import AgentProfileStore
from cf_agent_gateway.artifact import ArtifactKind, ArtifactRepository, ArtifactStatus
from cf_agent_gateway.config import (
    ArtifactSettings,
    DatabaseSettings,
    HermesSettings,
    RuntimeSettings,
    Settings,
    WechatSettings,
    WorkerSettings,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.delivery import (
    DeliveryOutboxRecord,
    DeliveryReceipt,
    DeliveryStatus,
)
from cf_agent_gateway.hermes import (
    ArtifactRefPart,
    HermesChatResult,
    ResponseEnvelope,
    TextPart,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Conversation, Message, MessageRawPayload
from cf_agent_gateway.response import (
    ResponsePartKind,
    ResponseRecord,
    ResponseStatus,
)
from cf_agent_gateway.runtime import run_wechat_poll_once
from cf_agent_gateway.runtime.delivery import drain_wechat_delivery_outbox
from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
from cf_agent_gateway.runtime.worker import run_worker
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread, ThreadPolicy, ThreadType

SOURCE_ACCOUNT_ID = "wxid_v2_runtime_gateway"
WECHAT_TOKEN_ENV = "TEST_V2_RUNTIME_WECHAT_TOKEN"
WECHAT_TOKEN = "v2-runtime-wechat-token"
HERMES_API_KEY_ENV = "TEST_V2_RUNTIME_HERMES_KEY"
HERMES_API_KEY = "v2-runtime-hermes-key"
PROFILE_ID = "00000000-0000-4000-8000-000000000007"
PROFILE_REVISION = 7
PROFILE_REFERENCE = "profiles/v2-runtime-e2e/7"


def raw_wechat_message(
    local_id: int,
    *,
    server_id: int,
    conversation_id: str,
    sender_id: str,
    content: str,
    is_mentioned: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "localId": local_id,
        "serverId": server_id,
        "chatId": conversation_id,
        "sender": sender_id,
        "senderName": f"Sender {sender_id}",
        "type": 1,
        "content": content,
        "timestamp": "2026-08-07T10:15:00+08:00",
        "archiveMarker": f"raw-{local_id}-{server_id}",
    }
    if is_mentioned is not None:
        payload["isMentioned"] = is_mentioned
    return payload


class WechatPollingMock:
    def __init__(self, messages: Mapping[str, list[Mapping[str, Any]]]) -> None:
        self._messages = dict(messages)
        self.close_calls = 0

    def get_auth_status(self) -> AgentWechatAuthStatus:
        return AgentWechatAuthStatus(status="logged_in", loggedInUser=SOURCE_ACCOUNT_ID)

    def list_chats(self) -> list[dict[str, Any]]:
        return [
            {"id": conversation_id, "name": f"Chat {conversation_id}"}
            for conversation_id in self._messages
        ]

    def list_messages(self, chat_id: str) -> list[RawWechatMessage | Mapping[str, Any]]:
        return self._messages[chat_id]

    def close(self) -> None:
        self.close_calls += 1


class RecordingHermesMock:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.scripted_responses: dict[str, ResponseEnvelope] = {}
        self.close_calls = 0

    def chat(
        self,
        content: str,
        *,
        hermes_thread_id: str | None = None,
        profile_reference: str | None = None,
        profile_revision: int | None = None,
        thread_id: str | None = None,
        session_metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> HermesChatResult:
        assert hermes_thread_id is not None
        self.calls.append(
            {
                "content": content,
                "hermes_thread_id": hermes_thread_id,
                "profile_reference": profile_reference,
                "profile_revision": profile_revision,
                "thread_id": thread_id,
                "session_metadata": session_metadata,
                "idempotency_key": idempotency_key,
            }
        )
        response = self.scripted_responses.get(content)
        if response is not None:
            return HermesChatResult.from_response(
                response,
                hermes_thread_id=hermes_thread_id,
            )
        return HermesChatResult(
            assistant_content=f"Hermes reply: {content}",
            hermes_thread_id=hermes_thread_id,
        )

    def close(self) -> None:
        self.close_calls += 1


class RecordingWechatSender:
    def __init__(self, account_id: str, factory: RecordingWechatSenderFactory) -> None:
        self.account_id = account_id
        self._factory = factory

    def send_text(self, conversation_id: str, content: str) -> dict[str, object]:
        self._factory.send_calls.append((self.account_id, conversation_id, content))
        self._factory.delivery_events.append(("text", self.account_id, conversation_id, content))
        return {
            "success": True,
            "localId": len(self._factory.delivery_events),
        }

    def send_media(
        self,
        conversation_id: str,
        media_type: str,
        data: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> dict[str, object]:
        call = (
            self.account_id,
            conversation_id,
            media_type,
            data,
            mime_type,
            filename,
        )
        self._factory.media_calls.append(call)
        self._factory.delivery_events.append(("media", *call))
        return {
            "success": True,
            "localId": len(self._factory.delivery_events),
        }

    def close(self) -> None:
        self._factory.closed_account_ids.append(self.account_id)


class RecordingWechatSenderFactory:
    def __init__(self) -> None:
        self.send_calls: list[tuple[str, str, str]] = []
        self.media_calls: list[tuple[object, ...]] = []
        self.delivery_events: list[tuple[object, ...]] = []
        self.closed_account_ids: list[str] = []

    def __call__(self, *, account_id: str) -> RecordingWechatSender:
        return RecordingWechatSender(account_id, self)


class RuntimeHarness:
    def __init__(self, tmp_path: Path) -> None:
        database_path = tmp_path / "v2-runtime-e2e.db"
        self.database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        self.artifact_root = tmp_path / "artifacts"
        self.settings = Settings(
            database=DatabaseSettings(url=self.database_url),
            artifact=ArtifactSettings(storage_root=str(self.artifact_root)),
            runtime=RuntimeSettings(
                polling_interval_seconds=0.001,
                v2_routing_enabled=True,
            ),
            worker=WorkerSettings(
                enabled=True,
                concurrency=1,
                lease_seconds=5,
                retry_limit=3,
            ),
            wechat=WechatSettings(
                enabled=True,
                base_url="https://agent-wechat.e2e.test:6174",
                bootstrap_mode="backfill",
                token_env=WECHAT_TOKEN_ENV,
            ),
            hermes=HermesSettings(
                enabled=True,
                base_url="https://hermes.e2e.test",
                api_key_env=HERMES_API_KEY_ENV,
                model="hermes-v2-runtime-e2e",
            ),
        )
        self.hermes = RecordingHermesMock()
        self.sender_factory = RecordingWechatSenderFactory()
        self.polling_clients: list[WechatPollingMock] = []
        self.hermes_factory_calls: list[tuple[str, str, str]] = []

        engine = create_database_engine(self.database_url)
        try:
            initialize_database(engine)
            with Session(engine) as session:
                AccessPolicyService(session).upsert_gateway_policy(
                    enabled=True,
                    allowed_risk_levels={RiskLevel.NORMAL},
                )
                AgentProfileStore(session).create_agent_profile(
                    profile_key="v2-runtime-e2e",
                    revision=PROFILE_REVISION,
                    provider="hermes",
                    external_profile_ref=PROFILE_REFERENCE,
                    model="hermes-v2-runtime-e2e",
                    agent_profile_id=PROFILE_ID,
                )
        finally:
            engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        engine = create_database_engine(self.database_url)
        try:
            with Session(engine) as session:
                yield session
        finally:
            engine.dispose()

    def provision_private(self, *, conversation_id: str, sender_id: str) -> str:
        with self.session() as session:
            identity_id = self._provision_sender(session, sender_id)
            self._create_conversation(
                session,
                conversation_id=conversation_id,
                conversation_type=ThreadType.PRIVATE,
            )
            return identity_id

    def bind_private_profile(self, *, conversation_id: str) -> None:
        with self.session() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.source == "wechat",
                    Conversation.source_account_id == SOURCE_ACCOUNT_ID,
                    Conversation.conversation_id == conversation_id,
                )
            )
            assert conversation is not None
            AgentProfileStore(session).bind_conversation_agent_profile(
                conversation_record_id=conversation.id,
                agent_profile_id=PROFILE_ID,
            )

    def configure_private(self, *, conversation_id: str, sender_id: str) -> str:
        identity_id = self.provision_private(
            conversation_id=conversation_id,
            sender_id=sender_id,
        )
        self.bind_private_profile(conversation_id=conversation_id)
        return identity_id

    def configure_group_sender(
        self, *, conversation_id: str, sender_ids: tuple[str, ...]
    ) -> dict[str, str]:
        with self.session() as session:
            identity_ids = {
                sender_id: self._provision_sender(session, sender_id) for sender_id in sender_ids
            }
            conversation = self._create_conversation(
                session,
                conversation_id=conversation_id,
                conversation_type=ThreadType.GROUP,
            )
            profile_store = AgentProfileStore(session)
            group_type, _ = profile_store.create_group_type(
                type_key="v2-runtime-group-sender",
                display_name="V2 runtime group sender",
                agent_profile_id=PROFILE_ID,
                thread_policy=ThreadPolicy.GROUP_SENDER,
            )
            profile_store.bind_conversation_group_type(
                conversation_record_id=conversation.id,
                group_type_id=group_type.id,
            )
            return identity_ids

    def poll(self, *messages: Mapping[str, Any]) -> PollResult:
        messages_by_chat: dict[str, list[Mapping[str, Any]]] = {}
        for message in messages:
            messages_by_chat.setdefault(str(message["chatId"]), []).append(message)
        polling_client = WechatPollingMock(messages_by_chat)
        self.polling_clients.append(polling_client)

        def create_wechat_client(*, base_url: str, token: str) -> WechatPollingMock:
            assert base_url == self.settings.wechat.base_url
            assert token == WECHAT_TOKEN
            return polling_client

        def create_hermes_client(*, base_url: str, api_key: str, model: str) -> RecordingHermesMock:
            self.hermes_factory_calls.append((base_url, api_key, model))
            return self.hermes

        result = run_wechat_poll_once(
            self.settings,
            client_factory=create_wechat_client,
            hermes_client_factory=create_hermes_client,
            sender_factory=self.sender_factory,
            environment_reader={
                WECHAT_TOKEN_ENV: WECHAT_TOKEN,
                HERMES_API_KEY_ENV: HERMES_API_KEY,
            }.get,
        )
        self._drain_runtime()
        return result

    def _drain_runtime(self) -> None:
        engine = create_database_engine(self.database_url)
        try:
            session_factory = create_database_session_factory(engine)
            worker = build_dispatch_worker(
                self.settings,
                session_factory=session_factory,
                hermes_client=self.hermes,
                sender_factory=self.sender_factory,
            )
            while worker.run_once() is not None:
                pass

            drain_wechat_delivery_outbox(
                session_factory,
                self.settings,
                sender_factory=self.sender_factory,
            )
        finally:
            engine.dispose()

    @staticmethod
    def _provision_sender(session: Session, sender_id: str) -> str:
        identity_service = IdentityService(session)
        identity = identity_service.create_identity(employee_id=f"employee-{sender_id}")
        identity_service.create_mapping(
            platform="wechat",
            account_id=SOURCE_ACCOUNT_ID,
            sender_id=sender_id,
            enterprise_identity_id=identity.id,
        )
        AccessPolicyService(session).upsert_user_policy(
            enterprise_identity_id=identity.id,
            enabled=True,
        )
        return identity.id

    @staticmethod
    def _create_conversation(
        session: Session,
        *,
        conversation_id: str,
        conversation_type: ThreadType,
    ) -> Conversation:
        conversation = Conversation(
            source="wechat",
            source_account_id=SOURCE_ACCOUNT_ID,
            conversation_id=conversation_id,
            conversation_type=conversation_type.value,
            conversation_name=f"Configured {conversation_id}",
        )
        session.add(conversation)
        session.commit()
        return conversation


def assert_v2_hermes_call(
    call: dict[str, object],
    *,
    message: Message,
    thread: AIThread,
    identity_id: str,
    idempotency_key: str,
    policy: ThreadPolicy,
) -> None:
    assert call["hermes_thread_id"] == f"v1:cf-agent-gateway:{thread.id}"
    assert call["profile_reference"] == PROFILE_REFERENCE
    assert call["profile_revision"] == PROFILE_REVISION
    assert call["thread_id"] == thread.id
    assert call["idempotency_key"] == idempotency_key
    assert call["session_metadata"] == {
        "message_id": message.id,
        "source": "wechat",
        "source_account_id": SOURCE_ACCOUNT_ID,
        "conversation_id": message.conversation_id,
        "conversation_type": message.conversation_type,
        "enterprise_identity_id": identity_id,
        "sender_identity_id": identity_id,
        "sender_id": message.sender_id,
        "thread_policy": policy.value,
    }


def assert_text_responses_delivered(
    session: Session,
    *,
    messages: list[Message],
) -> None:
    responses = list(session.scalars(select(ResponseRecord).order_by(ResponseRecord.message_id)))
    deliveries = list(
        session.scalars(select(DeliveryOutboxRecord).order_by(DeliveryOutboxRecord.id))
    )
    receipts = list(
        session.scalars(
            select(DeliveryReceipt).order_by(
                DeliveryReceipt.delivery_id,
                DeliveryReceipt.part_ordinal,
            )
        )
    )
    assert len(responses) == len(deliveries) == len(receipts) == len(messages)

    messages_by_id = {message.id: message for message in messages}
    deliveries_by_response = {delivery.response_id: delivery for delivery in deliveries}
    receipts_by_response = {receipt.response_id: receipt for receipt in receipts}
    assert (
        set(deliveries_by_response)
        == set(receipts_by_response)
        == {response.response_id for response in responses}
    )

    for response in responses:
        message = messages_by_id[response.message_id]
        assert response.status is ResponseStatus.DELIVERED
        assert response.ai_thread_id is not None
        assert response.part_count == 1
        assert [
            (part.ordinal, part.part_type, part.text, part.artifact_id) for part in response.parts
        ] == [
            (
                0,
                ResponsePartKind.TEXT,
                f"Hermes reply: {message.content}",
                None,
            )
        ]

        delivery = deliveries_by_response[response.response_id]
        assert delivery.channel == "wechat"
        assert delivery.account_id == SOURCE_ACCOUNT_ID
        assert delivery.conversation_id == message.conversation_id
        assert delivery.status is DeliveryStatus.DELIVERED
        assert delivery.next_part_ordinal == 1
        assert delivery.attempt_count == 1

        receipt = receipts_by_response[response.response_id]
        assert receipt.delivery_id == delivery.id
        assert receipt.part_ordinal == 0
        assert receipt.provider_message_id is not None
        assert receipt.receipt_payload["success"] is True


def test_private_senders_are_isolated_through_v2_runtime(tmp_path: Path) -> None:
    harness = RuntimeHarness(tmp_path)
    private_senders = {
        "wxid_private_alice": "wxid_alice",
        "wxid_private_bob": "wxid_bob",
    }
    identity_ids = {
        sender_id: harness.configure_private(
            conversation_id=conversation_id,
            sender_id=sender_id,
        )
        for conversation_id, sender_id in private_senders.items()
    }
    raw_messages = [
        raw_wechat_message(
            index,
            server_id=1000 + index,
            conversation_id=conversation_id,
            sender_id=sender_id,
            content=f"private request from {sender_id}",
        )
        for index, (conversation_id, sender_id) in enumerate(private_senders.items(), start=1)
    ]

    result = harness.poll(*raw_messages)

    assert result.logged_in is True
    assert result.messages_processed == 2
    assert result.chats_failed == 0
    assert result.failures == []
    assert harness.polling_clients[0].close_calls == 1

    with harness.session() as session:
        messages = list(session.scalars(select(Message).order_by(Message.id)))
        payloads = list(session.scalars(select(MessageRawPayload).order_by(MessageRawPayload.id)))
        threads = list(session.scalars(select(AIThread).order_by(AIThread.id)))
        records = list(
            session.scalars(select(HermesDispatchRecord).order_by(HermesDispatchRecord.message_id))
        )
        assert [payload.payload for payload in payloads] == raw_messages
        assert len(messages) == len(threads) == len(records) == 2
        assert len({thread.id for thread in threads}) == 2
        assert all(thread.thread_key.startswith("v2:sha256:private_sender:") for thread in threads)
        assert all(thread.thread_type is ThreadType.PRIVATE for thread in threads)
        assert all(thread.thread_policy is ThreadPolicy.PRIVATE_SENDER for thread in threads)
        assert all(thread.agent_profile_id == PROFILE_ID for thread in threads)

        messages_by_id = {message.id: message for message in messages}
        threads_by_id = {thread.id: thread for thread in threads}
        calls_by_content = {str(call["content"]): call for call in harness.hermes.calls}
        for record in records:
            message = messages_by_id[record.message_id]
            thread = threads_by_id[record.ai_thread_id]
            identity_id = identity_ids[str(message.sender_id)]
            assert record.enterprise_identity_id == identity_id
            assert record.status is HermesDispatchStatus.SUCCESS
            assert record.attempt_count == 1
            assert thread.hermes_thread_id == f"v1:cf-agent-gateway:{thread.id}"
            assert_v2_hermes_call(
                calls_by_content[message.content],
                message=message,
                thread=thread,
                identity_id=identity_id,
                idempotency_key=record.idempotency_key,
                policy=ThreadPolicy.PRIVATE_SENDER,
            )
        assert_text_responses_delivered(session, messages=messages)

    assert harness.sender_factory.send_calls == [
        (SOURCE_ACCOUNT_ID, raw["chatId"], f"Hermes reply: {raw['content']}")
        for raw in raw_messages
    ]


def test_group_sender_policy_isolates_members_through_v2_runtime(tmp_path: Path) -> None:
    harness = RuntimeHarness(tmp_path)
    conversation_id = "v2-isolated-group@chatroom"
    sender_ids = ("wxid_member_a", "wxid_member_b")
    identity_ids = harness.configure_group_sender(
        conversation_id=conversation_id,
        sender_ids=sender_ids,
    )
    raw_messages = [
        raw_wechat_message(
            index,
            server_id=2000 + index,
            conversation_id=conversation_id,
            sender_id=sender_id,
            content=f"group request from {sender_id}",
            is_mentioned=True,
        )
        for index, sender_id in enumerate(sender_ids, start=1)
    ]

    result = harness.poll(*raw_messages)

    assert result.messages_processed == 2
    assert result.chats_failed == 0
    assert result.failures == []

    with harness.session() as session:
        messages = list(session.scalars(select(Message).order_by(Message.id)))
        payloads = list(session.scalars(select(MessageRawPayload).order_by(MessageRawPayload.id)))
        threads = list(session.scalars(select(AIThread).order_by(AIThread.id)))
        records = list(
            session.scalars(select(HermesDispatchRecord).order_by(HermesDispatchRecord.message_id))
        )
        assert [payload.payload for payload in payloads] == raw_messages
        assert len(messages) == len(threads) == len(records) == 2
        assert len({record.ai_thread_id for record in records}) == 2
        assert all(thread.thread_key.startswith("v2:sha256:group_sender:") for thread in threads)
        assert all(thread.thread_type is ThreadType.GROUP for thread in threads)
        assert all(thread.thread_policy is ThreadPolicy.GROUP_SENDER for thread in threads)
        assert all(thread.agent_profile_id == PROFILE_ID for thread in threads)

        messages_by_id = {message.id: message for message in messages}
        threads_by_id = {thread.id: thread for thread in threads}
        calls_by_content = {str(call["content"]): call for call in harness.hermes.calls}
        for record in records:
            message = messages_by_id[record.message_id]
            thread = threads_by_id[record.ai_thread_id]
            identity_id = identity_ids[str(message.sender_id)]
            assert record.enterprise_identity_id == identity_id
            assert record.status is HermesDispatchStatus.SUCCESS
            assert record.attempt_count == 1
            assert_v2_hermes_call(
                calls_by_content[message.content],
                message=message,
                thread=thread,
                identity_id=identity_id,
                idempotency_key=record.idempotency_key,
                policy=ThreadPolicy.GROUP_SENDER,
            )
        assert_text_responses_delivered(session, messages=messages)

    assert harness.sender_factory.send_calls == [
        (SOURCE_ACCOUNT_ID, conversation_id, f"Hermes reply: {raw['content']}")
        for raw in raw_messages
    ]


def test_duplicate_source_message_is_archived_dispatched_and_delivered_once(
    tmp_path: Path,
) -> None:
    harness = RuntimeHarness(tmp_path)
    conversation_id = "wxid_private_dedupe"
    sender_id = "wxid_dedupe_sender"
    identity_id = harness.configure_private(
        conversation_id=conversation_id,
        sender_id=sender_id,
    )
    original = raw_wechat_message(
        1,
        server_id=3001,
        conversation_id=conversation_id,
        sender_id=sender_id,
        content="dispatch this exactly once",
    )
    duplicate = raw_wechat_message(
        2,
        server_id=3001,
        conversation_id=conversation_id,
        sender_id=sender_id,
        content="duplicate payload must not replace the archive",
    )

    first_result = harness.poll(original)
    duplicate_result = harness.poll(duplicate)

    assert first_result.messages_processed == 1
    assert duplicate_result.messages_processed == 1
    assert first_result.failures == duplicate_result.failures == []
    assert len(harness.hermes.calls) == 1
    assert harness.sender_factory.send_calls == [
        (SOURCE_ACCOUNT_ID, conversation_id, "Hermes reply: dispatch this exactly once")
    ]

    with harness.session() as session:
        message = session.scalar(select(Message))
        payload = session.scalar(select(MessageRawPayload))
        thread = session.scalar(select(AIThread))
        record = session.scalar(select(HermesDispatchRecord))
        checkpoint = session.scalar(select(WechatSyncCheckpoint))
        assert message is not None
        assert payload is not None and payload.payload == original
        assert thread is not None
        assert record is not None
        assert checkpoint is not None and checkpoint.last_local_id == 2
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        assert session.scalar(select(func.count()).select_from(MessageRawPayload)) == 1
        assert session.scalar(select(func.count()).select_from(AIThread)) == 1
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1
        assert thread.thread_policy is ThreadPolicy.PRIVATE_SENDER
        assert thread.agent_profile_id == PROFILE_ID
        assert record.message_id == message.id
        assert record.ai_thread_id == thread.id
        assert record.enterprise_identity_id == identity_id
        assert record.status is HermesDispatchStatus.SUCCESS
        assert record.attempt_count == 1
        assert_v2_hermes_call(
            harness.hermes.calls[0],
            message=message,
            thread=thread,
            identity_id=identity_id,
            idempotency_key=record.idempotency_key,
            policy=ThreadPolicy.PRIVATE_SENDER,
        )
        assert_text_responses_delivered(session, messages=[message])


def test_worker_recovers_archived_message_after_private_route_is_configured(
    tmp_path: Path,
) -> None:
    harness = RuntimeHarness(tmp_path)
    conversation_id = "wxid_private_worker_recovery"
    sender_id = "wxid_worker_recovery_sender"
    identity_id = harness.provision_private(
        conversation_id=conversation_id,
        sender_id=sender_id,
    )
    raw = raw_wechat_message(
        1,
        server_id=4001,
        conversation_id=conversation_id,
        sender_id=sender_id,
        content="recover this archived request",
    )
    first_stop_event = Event()
    first_results: list[PollResult] = []

    def first_poll(settings: Settings) -> PollResult:
        assert settings is harness.settings
        try:
            result = harness.poll(raw)
            first_results.append(result)
            return result
        finally:
            first_stop_event.set()

    run_worker(
        harness.settings,
        stop_event=first_stop_event,
        poll_once=first_poll,
    )

    assert len(first_results) == 1
    first_result = first_results[0]
    assert first_result.messages_processed == 0
    assert first_result.chats_failed == 1
    assert len(first_result.failures) == 1
    assert first_result.failures[0].stage.value == "sink"
    assert first_result.failures[0].code == "wechat_sink_error"

    with harness.session() as session:
        checkpoint = session.scalar(select(WechatSyncCheckpoint))
        assert checkpoint is not None and checkpoint.last_local_id == 0
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        assert session.scalar(select(func.count()).select_from(AIThread)) == 0
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0

    harness.bind_private_profile(conversation_id=conversation_id)
    restarted_stop_event = Event()
    restarted_results: list[PollResult] = []

    def restarted_poll(settings: Settings) -> PollResult:
        assert settings is harness.settings
        try:
            result = harness.poll(raw)
            restarted_results.append(result)
            return result
        finally:
            restarted_stop_event.set()

    run_worker(
        harness.settings,
        stop_event=restarted_stop_event,
        poll_once=restarted_poll,
    )

    assert len(restarted_results) == 1
    recovered_result = restarted_results[0]
    assert recovered_result.messages_processed == 1
    assert recovered_result.chats_failed == 0
    assert recovered_result.failures == []
    assert len(harness.hermes.calls) == 1
    assert harness.hermes.close_calls == 0
    assert harness.hermes_factory_calls == []
    assert all(client.close_calls == 1 for client in harness.polling_clients)
    assert harness.sender_factory.send_calls == [
        (
            SOURCE_ACCOUNT_ID,
            conversation_id,
            "Hermes reply: recover this archived request",
        )
    ]

    with harness.session() as session:
        message = session.scalar(select(Message))
        payload = session.scalar(select(MessageRawPayload))
        thread = session.scalar(select(AIThread))
        record = session.scalar(select(HermesDispatchRecord))
        checkpoint = session.scalar(select(WechatSyncCheckpoint))
        assert message is not None
        assert payload is not None and payload.payload == raw
        assert thread is not None
        assert record is not None
        assert checkpoint is not None and checkpoint.last_local_id == 1
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        assert session.scalar(select(func.count()).select_from(MessageRawPayload)) == 1
        assert session.scalar(select(func.count()).select_from(AIThread)) == 1
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1
        assert record.status is HermesDispatchStatus.SUCCESS
        assert record.attempt_count == 1
        assert_v2_hermes_call(
            harness.hermes.calls[0],
            message=message,
            thread=thread,
            identity_id=identity_id,
            idempotency_key=record.idempotency_key,
            policy=ThreadPolicy.PRIVATE_SENDER,
        )
        assert_text_responses_delivered(session, messages=[message])


def test_artifact_only_file_response_is_persisted_and_delivered(
    tmp_path: Path,
) -> None:
    harness = RuntimeHarness(tmp_path)
    conversation_id = "wxid_private_artifact_reply"
    sender_id = "wxid_artifact_reply_sender"
    identity_id = harness.configure_private(
        conversation_id=conversation_id,
        sender_id=sender_id,
    )
    response_id = "v2-runtime-file-response"
    file_content = b"%PDF-1.7\nV2 runtime artifact reply\n"
    with harness.session() as session:
        artifact = ArtifactRepository(session, harness.artifact_root).create(
            response_id=response_id,
            kind=ArtifactKind.FILE,
            filename="runtime-report.pdf",
            mime_type="application/pdf",
            content=file_content,
        )
        artifact_id = artifact.artifact_id
        assert artifact.status is ArtifactStatus.READY

    request_content = "return the generated report"
    harness.hermes.scripted_responses[request_content] = ResponseEnvelope(
        response_id=response_id,
        parts=(ArtifactRefPart(artifact_id=artifact_id),),
    )
    raw = raw_wechat_message(
        1,
        server_id=5001,
        conversation_id=conversation_id,
        sender_id=sender_id,
        content=request_content,
    )

    result = harness.poll(raw)

    assert result.messages_processed == 1
    assert result.chats_failed == 0
    assert result.failures == []
    expected_media_call = (
        SOURCE_ACCOUNT_ID,
        conversation_id,
        "file",
        file_content,
        "application/pdf",
        "runtime-report.pdf",
    )
    assert harness.sender_factory.send_calls == []
    assert harness.sender_factory.media_calls == [expected_media_call]
    assert harness.sender_factory.delivery_events == [("media", *expected_media_call)]

    with harness.session() as session:
        message = session.scalar(select(Message))
        payload = session.scalar(select(MessageRawPayload))
        thread = session.scalar(select(AIThread))
        record = session.scalar(select(HermesDispatchRecord))
        response = session.get(ResponseRecord, response_id)
        delivery = session.scalar(select(DeliveryOutboxRecord))
        receipt = session.scalar(select(DeliveryReceipt))
        stored_artifact = ArtifactRepository(session, harness.artifact_root).get(artifact_id)

        assert message is not None
        assert payload is not None and payload.payload == raw
        assert thread is not None
        assert record is not None
        assert response is not None
        assert delivery is not None
        assert receipt is not None
        assert stored_artifact is not None
        assert stored_artifact.status is ArtifactStatus.READY
        assert record.message_id == message.id
        assert record.ai_thread_id == thread.id
        assert record.enterprise_identity_id == identity_id
        assert record.status is HermesDispatchStatus.SUCCESS
        assert record.attempt_count == 1
        assert response.message_id == message.id
        assert response.ai_thread_id == thread.id
        assert response.status is ResponseStatus.DELIVERED
        assert response.part_count == 1
        assert [
            (part.ordinal, part.part_type, part.text, part.artifact_id) for part in response.parts
        ] == [(0, ResponsePartKind.ARTIFACT_REF, None, artifact_id)]
        assert delivery.response_id == response_id
        assert delivery.channel == "wechat"
        assert delivery.account_id == SOURCE_ACCOUNT_ID
        assert delivery.conversation_id == conversation_id
        assert delivery.status is DeliveryStatus.DELIVERED
        assert delivery.next_part_ordinal == 1
        assert delivery.attempt_count == 1
        assert receipt.delivery_id == delivery.id
        assert receipt.response_id == response_id
        assert receipt.part_ordinal == 0
        assert receipt.provider_message_id == "1"
        assert receipt.receipt_payload == {"success": True, "localId": 1}
        assert_v2_hermes_call(
            harness.hermes.calls[0],
            message=message,
            thread=thread,
            identity_id=identity_id,
            idempotency_key=record.idempotency_key,
            policy=ThreadPolicy.PRIVATE_SENDER,
        )


def test_text_and_image_response_preserves_delivery_order(
    tmp_path: Path,
) -> None:
    harness = RuntimeHarness(tmp_path)
    conversation_id = "wxid_private_media_reply"
    sender_id = "wxid_media_reply_sender"
    identity_id = harness.configure_private(
        conversation_id=conversation_id,
        sender_id=sender_id,
    )
    response_id = "v2-runtime-image-response"
    image_content = b"\x89PNG\r\n\x1a\nV2 runtime image reply"
    with harness.session() as session:
        artifact = ArtifactRepository(session, harness.artifact_root).create(
            response_id=response_id,
            kind=ArtifactKind.IMAGE,
            filename="runtime-chart.png",
            mime_type="image/png",
            content=image_content,
        )
        artifact_id = artifact.artifact_id
        assert artifact.status is ArtifactStatus.READY

    request_content = "show the generated chart"
    response_text = "The chart is ready."
    harness.hermes.scripted_responses[request_content] = ResponseEnvelope(
        response_id=response_id,
        parts=(
            TextPart(text=response_text),
            ArtifactRefPart(artifact_id=artifact_id),
        ),
    )
    raw = raw_wechat_message(
        1,
        server_id=6001,
        conversation_id=conversation_id,
        sender_id=sender_id,
        content=request_content,
    )

    result = harness.poll(raw)

    assert result.messages_processed == 1
    assert result.chats_failed == 0
    assert result.failures == []
    expected_text_call = (SOURCE_ACCOUNT_ID, conversation_id, response_text)
    expected_media_call = (
        SOURCE_ACCOUNT_ID,
        conversation_id,
        "image",
        image_content,
        "image/png",
        None,
    )
    assert harness.sender_factory.send_calls == [expected_text_call]
    assert harness.sender_factory.media_calls == [expected_media_call]
    assert harness.sender_factory.delivery_events == [
        ("text", *expected_text_call),
        ("media", *expected_media_call),
    ]

    with harness.session() as session:
        message = session.scalar(select(Message))
        payload = session.scalar(select(MessageRawPayload))
        thread = session.scalar(select(AIThread))
        record = session.scalar(select(HermesDispatchRecord))
        response = session.get(ResponseRecord, response_id)
        delivery = session.scalar(select(DeliveryOutboxRecord))
        receipts = list(
            session.scalars(select(DeliveryReceipt).order_by(DeliveryReceipt.part_ordinal))
        )
        stored_artifact = ArtifactRepository(session, harness.artifact_root).get(artifact_id)

        assert message is not None
        assert payload is not None and payload.payload == raw
        assert thread is not None
        assert record is not None
        assert response is not None
        assert delivery is not None
        assert stored_artifact is not None
        assert stored_artifact.status is ArtifactStatus.READY
        assert record.message_id == message.id
        assert record.ai_thread_id == thread.id
        assert record.enterprise_identity_id == identity_id
        assert record.status is HermesDispatchStatus.SUCCESS
        assert record.attempt_count == 1
        assert response.message_id == message.id
        assert response.ai_thread_id == thread.id
        assert response.status is ResponseStatus.DELIVERED
        assert response.part_count == 2
        assert [
            (part.ordinal, part.part_type, part.text, part.artifact_id) for part in response.parts
        ] == [
            (0, ResponsePartKind.TEXT, response_text, None),
            (1, ResponsePartKind.ARTIFACT_REF, None, artifact_id),
        ]
        assert delivery.response_id == response_id
        assert delivery.channel == "wechat"
        assert delivery.account_id == SOURCE_ACCOUNT_ID
        assert delivery.conversation_id == conversation_id
        assert delivery.status is DeliveryStatus.DELIVERED
        assert delivery.next_part_ordinal == 2
        assert delivery.attempt_count == 1
        assert [receipt.delivery_id for receipt in receipts] == [
            delivery.id,
            delivery.id,
        ]
        assert [receipt.response_id for receipt in receipts] == [
            response_id,
            response_id,
        ]
        assert [receipt.part_ordinal for receipt in receipts] == [0, 1]
        assert [receipt.provider_message_id for receipt in receipts] == ["1", "2"]
        assert [receipt.receipt_payload for receipt in receipts] == [
            {"success": True, "localId": 1},
            {"success": True, "localId": 2},
        ]
        assert_v2_hermes_call(
            harness.hermes.calls[0],
            message=message,
            thread=thread,
            identity_id=identity_id,
            idempotency_key=record.idempotency_key,
            policy=ThreadPolicy.PRIVATE_SENDER,
        )
