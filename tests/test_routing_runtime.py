from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.adapters.wechat import (
    NormalizedWechatMessage,
    WechatConversationType,
    WechatMessageType,
    WechatSenderType,
)
from cf_agent_gateway.agent_profile import (
    AgentProfile,
    AgentProfileStore,
    PrivateConversationProfileNotConfiguredError,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import (
    HermesChatResult,
    HermesDispatchService,
    HermesResponseHandler,
    HermesResponseRelay,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread, ThreadPolicy, ThreadSourceBinding
from cf_agent_gateway.workspace.store import WorkspaceStore

SOURCE = "wechat"
SOURCE_ACCOUNT_ID = "wxid-gateway"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as database_session:
            yield database_session
    finally:
        engine.dispose()


def provision_sender(session: Session, sender_id: str) -> EnterpriseIdentity:
    identity_service = IdentityService(session)
    identity = identity_service.create_identity(employee_id=f"employee-{sender_id}")
    identity_service.create_mapping(
        platform=SOURCE,
        account_id=SOURCE_ACCOUNT_ID,
        sender_id=sender_id,
        enterprise_identity_id=identity.id,
    )
    AccessPolicyService(session).upsert_user_policy(
        enterprise_identity_id=identity.id,
        enabled=True,
    )
    return identity


def allow_gateway(session: Session) -> None:
    AccessPolicyService(session).upsert_gateway_policy(
        enabled=True,
        allowed_risk_levels={RiskLevel.NORMAL},
    )


def create_conversation(
    session: Session,
    conversation_id: str,
    *,
    conversation_type: str,
) -> Conversation:
    conversation = Conversation(
        source=SOURCE,
        source_account_id=SOURCE_ACCOUNT_ID,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        conversation_name="A name that routing must ignore",
    )
    session.add(conversation)
    session.commit()
    return conversation


def create_profile(
    session: Session,
    *,
    revision: int = 1,
    profile_key: str = "runtime-assistant",
) -> AgentProfile:
    profile, _ = AgentProfileStore(session).create_agent_profile(
        profile_key=profile_key,
        revision=revision,
        provider="hermes",
        external_profile_ref=f"profiles/{profile_key}/{revision}",
        model="hermes-agent",
    )
    return profile


def bind_private_profile(
    session: Session,
    conversation: Conversation,
    profile: AgentProfile,
) -> None:
    AgentProfileStore(session).bind_conversation_agent_profile(
        conversation_record_id=conversation.id,
        agent_profile_id=profile.id,
    )


def bind_group_type(
    session: Session,
    conversation: Conversation,
    profile: AgentProfile,
    policy: ThreadPolicy,
) -> None:
    store = AgentProfileStore(session)
    group_type, _ = store.create_group_type(
        type_key="runtime-group",
        display_name="Runtime group",
        agent_profile_id=profile.id,
        thread_policy=policy,
    )
    store.bind_conversation_group_type(
        conversation_record_id=conversation.id,
        group_type_id=group_type.id,
    )


def message(
    *,
    sequence: int,
    sender_id: str,
    conversation_id: str,
    conversation_type: WechatConversationType,
) -> NormalizedWechatMessage:
    is_group = conversation_type is WechatConversationType.GROUP
    return NormalizedWechatMessage(
        source_account_id=SOURCE_ACCOUNT_ID,
        source_message_id=f"server-{sequence}",
        source_local_id=f"local-{sequence}",
        source_server_id=f"server-{sequence}",
        source_message_id_is_fallback=False,
        event_id=f"wechat:event-{sequence}",
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        conversation_name="Body and names never select a profile",
        sender_type=WechatSenderType.HUMAN,
        sender_id=sender_id,
        sender_name=sender_id,
        message_type=WechatMessageType.TEXT,
        raw_type=1,
        content="This body must not influence routing",
        timestamp=datetime(2026, 8, 7, 10, sequence, tzinfo=UTC),
        is_mentioned=True if is_group else None,
        is_self=False,
    )


def v2_service(session: Session) -> MessageAdmissionService:
    return MessageAdmissionService(session, v2_routing_enabled=True)


class RecordingV2HermesClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def chat(
        self,
        content: str,
        *,
        hermes_thread_id: str | None = None,
        profile_reference: str | None = None,
        profile_revision: int | None = None,
        thread_id: str | None = None,
        session_metadata: dict[str, object] | None = None,
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
            }
        )
        return HermesChatResult(
            assistant_content="V2 response",
            hermes_thread_id=hermes_thread_id,
        )


class RecordingWechatSender:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.messages: list[tuple[str, str]] = []

    def send_text(self, conversation_id: str, text: str) -> None:
        self.messages.append((conversation_id, text))


def test_private_employees_resolve_distinct_v2_threads(session: Session) -> None:
    allow_gateway(session)
    identity_a = provision_sender(session, "employee-a")
    identity_b = provision_sender(session, "employee-b")
    conversation = create_conversation(session, "private-shared", conversation_type="private")
    profile = create_profile(session)
    bind_private_profile(session, conversation, profile)
    service = v2_service(session)

    outcome_a = service.process(
        message(
            sequence=1,
            sender_id="employee-a",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.PRIVATE,
        )
    )
    outcome_b = service.process(
        message(
            sequence=2,
            sender_id="employee-b",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.PRIVATE,
        )
    )

    assert outcome_a.admission.enterprise_identity_id == identity_a.id
    assert outcome_b.admission.enterprise_identity_id == identity_b.id
    assert outcome_a.ai_thread_id != outcome_b.ai_thread_id
    threads = list(session.scalars(select(AIThread).order_by(AIThread.id)))
    assert len(threads) == 2
    assert all(thread.thread_key.startswith("v2:") for thread in threads)
    assert all(thread.agent_profile_id == profile.id for thread in threads)
    assert all(thread.thread_policy is ThreadPolicy.PRIVATE_SENDER for thread in threads)


def test_group_shared_members_reuse_one_v2_thread(session: Session) -> None:
    allow_gateway(session)
    provision_sender(session, "member-a")
    provision_sender(session, "member-b")
    conversation = create_conversation(session, "shared@chatroom", conversation_type="group")
    profile = create_profile(session)
    bind_group_type(session, conversation, profile, ThreadPolicy.GROUP_SHARED)
    service = v2_service(session)

    first = service.process(
        message(
            sequence=3,
            sender_id="member-a",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.GROUP,
        )
    )
    second = service.process(
        message(
            sequence=4,
            sender_id="member-b",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.GROUP,
        )
    )

    assert second.ai_thread_id == first.ai_thread_id
    thread = session.get(AIThread, first.ai_thread_id)
    assert thread is not None
    assert thread.thread_policy is ThreadPolicy.GROUP_SHARED
    assert session.scalar(select(func.count()).select_from(AIThread)) == 1


def test_group_sender_members_resolve_distinct_v2_threads(session: Session) -> None:
    allow_gateway(session)
    provision_sender(session, "member-a")
    provision_sender(session, "member-b")
    conversation = create_conversation(session, "isolated@chatroom", conversation_type="group")
    profile = create_profile(session)
    bind_group_type(session, conversation, profile, ThreadPolicy.GROUP_SENDER)
    service = v2_service(session)

    first = service.process(
        message(
            sequence=5,
            sender_id="member-a",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.GROUP,
        )
    )
    second = service.process(
        message(
            sequence=6,
            sender_id="member-b",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.GROUP,
        )
    )

    assert second.ai_thread_id != first.ai_thread_id
    assert session.scalar(select(func.count()).select_from(AIThread)) == 2


def test_profile_revision_change_creates_new_thread_generation(session: Session) -> None:
    allow_gateway(session)
    provision_sender(session, "member-a")
    conversation = create_conversation(session, "revision@chatroom", conversation_type="group")
    first_profile = create_profile(session, revision=1)
    bind_group_type(session, conversation, first_profile, ThreadPolicy.GROUP_SHARED)
    service = v2_service(session)

    first = service.process(
        message(
            sequence=7,
            sender_id="member-a",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.GROUP,
        )
    )
    second_profile = create_profile(session, revision=2)
    AgentProfileStore(session).upsert_group_type(
        type_key="runtime-group",
        display_name="Runtime group",
        agent_profile_id=second_profile.id,
        thread_policy=ThreadPolicy.GROUP_SHARED,
    )
    second = service.process(
        message(
            sequence=8,
            sender_id="member-a",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.GROUP,
        )
    )

    assert second.ai_thread_id != first.ai_thread_id
    profile_ids = set(session.scalars(select(AIThread.agent_profile_id)))
    assert profile_ids == {first_profile.id, second_profile.id}
    assert session.scalar(select(func.count()).select_from(AIThread)) == 2


def test_disabled_v2_flag_keeps_v1_thread_routing(session: Session) -> None:
    allow_gateway(session)
    provision_sender(session, "employee-a")

    outcome = MessageAdmissionService(session).process(
        message(
            sequence=9,
            sender_id="employee-a",
            conversation_id="v1-private",
            conversation_type=WechatConversationType.PRIVATE,
        )
    )

    thread = session.get(AIThread, outcome.ai_thread_id)
    assert thread is not None
    assert thread.thread_key.startswith("v1:")
    assert thread.agent_profile_id is None
    assert thread.thread_policy is None
    assert session.scalar(select(func.count()).select_from(ThreadSourceBinding)) == 1


def test_v2_route_snapshot_rejects_thread_policy_type_mismatch(session: Session) -> None:
    allow_gateway(session)
    provision_sender(session, "employee-a")
    profile = create_profile(session)
    outcome = MessageAdmissionService(session).process(
        message(
            sequence=12,
            sender_id="employee-a",
            conversation_id="v1-policy-private",
            conversation_type=WechatConversationType.PRIVATE,
        )
    )
    thread = session.get(AIThread, outcome.ai_thread_id)
    assert thread is not None

    with pytest.raises(
        ValueError,
        match="group_shared requires a group thread",
    ):
        WorkspaceStore(session).bind_v2_route_snapshot(
            thread,
            agent_profile_id=profile.id,
            thread_policy=ThreadPolicy.GROUP_SHARED,
        )

    session.refresh(thread)
    assert thread.agent_profile_id is None
    assert thread.thread_policy is None


def test_duplicate_v2_message_does_not_create_duplicate_thread_or_dispatch(
    session: Session,
) -> None:
    allow_gateway(session)
    provision_sender(session, "employee-a")
    conversation = create_conversation(session, "dedupe-private", conversation_type="private")
    profile = create_profile(session)
    bind_private_profile(session, conversation, profile)
    service = v2_service(session)
    incoming = message(
        sequence=10,
        sender_id="employee-a",
        conversation_id=conversation.conversation_id,
        conversation_type=WechatConversationType.PRIVATE,
    )

    first = service.process(incoming)
    next_profile = create_profile(session, revision=2)
    bind_private_profile(session, conversation, next_profile)
    duplicate = service.process(incoming)

    assert first.message_created is True
    assert duplicate.message_created is False
    assert duplicate.message_id == first.message_id
    assert duplicate.ai_thread_id == first.ai_thread_id
    persisted_thread = session.get(AIThread, first.ai_thread_id)
    assert persisted_thread is not None
    assert persisted_thread.agent_profile_id == profile.id
    assert session.scalar(select(func.count()).select_from(Message)) == 1
    assert session.scalar(select(func.count()).select_from(AIThread)) == 1
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_duplicate_archived_v2_message_without_outbox_recovers_after_binding(
    session: Session,
) -> None:
    allow_gateway(session)
    provision_sender(session, "employee-a")
    conversation = create_conversation(
        session,
        "recover-private",
        conversation_type="private",
    )
    incoming = message(
        sequence=13,
        sender_id="employee-a",
        conversation_id=conversation.conversation_id,
        conversation_type=WechatConversationType.PRIVATE,
    )
    service = v2_service(session)

    with pytest.raises(PrivateConversationProfileNotConfiguredError):
        service.process(incoming)
    assert session.scalar(select(func.count()).select_from(Message)) == 1
    assert session.scalar(select(func.count()).select_from(AIThread)) == 0
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0

    profile = create_profile(session)
    bind_private_profile(session, conversation, profile)
    recovered = service.process(incoming)

    assert recovered.message_created is False
    assert recovered.dispatch_record_id is not None
    assert recovered.ai_thread_id is not None
    assert session.scalar(select(func.count()).select_from(Message)) == 1
    assert session.scalar(select(func.count()).select_from(AIThread)) == 1
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_v2_dispatch_pipeline_invokes_hermes_with_profile_and_session_metadata(
    session: Session,
) -> None:
    allow_gateway(session)
    identity = provision_sender(session, "employee-a")
    conversation = create_conversation(session, "dispatch-private", conversation_type="private")
    profile = create_profile(session, revision=3)
    bind_private_profile(session, conversation, profile)
    client = RecordingV2HermesClient()
    sender = RecordingWechatSender(SOURCE_ACCOUNT_ID)
    dispatcher = HermesResponseRelay(
        HermesDispatchService(session, client),
        HermesResponseHandler(session, sender),
    )
    service = MessageAdmissionService(
        session,
        v2_routing_enabled=True,
        hermes_dispatcher=dispatcher,
    )

    outcome = service.process(
        message(
            sequence=11,
            sender_id="employee-a",
            conversation_id=conversation.conversation_id,
            conversation_type=WechatConversationType.PRIVATE,
        )
    )

    assert outcome.hermes_dispatch is not None
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["profile_reference"] == profile.external_profile_ref
    assert call["profile_revision"] == profile.revision
    assert call["thread_id"] == outcome.ai_thread_id
    assert call["session_metadata"] == {
        "message_id": outcome.message_id,
        "source": SOURCE,
        "source_account_id": SOURCE_ACCOUNT_ID,
        "conversation_id": conversation.conversation_id,
        "conversation_type": "private",
        "enterprise_identity_id": identity.id,
        "sender_identity_id": identity.id,
        "sender_id": "employee-a",
        "thread_policy": ThreadPolicy.PRIVATE_SENDER.value,
    }
    record = session.scalar(select(HermesDispatchRecord))
    assert record is not None
    assert record.status is HermesDispatchStatus.SUCCESS
    assert sender.messages == [(conversation.conversation_id, "V2 response")]
