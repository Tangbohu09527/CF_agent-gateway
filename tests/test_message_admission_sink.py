from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.access import AccessPolicyService, ReasonCode, RequestFacts, RiskLevel
from cf_agent_gateway.adapters.wechat import (
    NormalizedWechatMessage,
    WechatConversationType,
    WechatMessageType,
    WechatSenderType,
)
from cf_agent_gateway.admission import (
    AdmissionCandidate,
    AdmissionOutcome,
    AdmissionReason,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity, SourceIdentityMapping
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import (
    DefaultAdmissionRequestResolver,
    MessageAdmissionService,
    MessageIngestionOutcome,
    MessageStoreAdmissionSink,
    PersistedMessageNotFoundError,
    PersistedMessageSnapshot,
    SessionFactoryMessageStoreAdmissionSink,
)
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace

SOURCE_ACCOUNT_ID = "wxid-gateway"
PRIVATE_CONVERSATION_ID = "wxid-alice"
GROUP_CONVERSATION_ID = "engineering@chatroom"
SENDER_ID = "wxid-alice"


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


def normalized_message(**overrides: object) -> NormalizedWechatMessage:
    values: dict[str, object] = {
        "source_account_id": SOURCE_ACCOUNT_ID,
        "source_message_id": "server-001",
        "source_local_id": "local-001",
        "source_server_id": "server-001",
        "source_message_id_is_fallback": False,
        "event_id": "wechat:event-001",
        "conversation_id": PRIVATE_CONVERSATION_ID,
        "conversation_type": WechatConversationType.PRIVATE,
        "conversation_name": "Alice",
        "sender_type": WechatSenderType.HUMAN,
        "sender_id": SENDER_ID,
        "sender_name": "Alice",
        "message_type": WechatMessageType.TEXT,
        "raw_type": 1,
        "content": "please summarize the release notes",
        "timestamp": datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
        "is_mentioned": None,
        "is_self": False,
        "reply": None,
    }
    values.update(overrides)
    return NormalizedWechatMessage.model_validate(values)


def group_message(**overrides: object) -> NormalizedWechatMessage:
    values: dict[str, object] = {
        "conversation_id": GROUP_CONVERSATION_ID,
        "conversation_type": WechatConversationType.GROUP,
        "conversation_name": "Engineering",
        "is_mentioned": True,
    }
    values.update(overrides)
    return normalized_message(**values)


def provision_sender(
    session: Session,
    *,
    sender_id: str = SENDER_ID,
    employee_id: str | None = None,
    create_user_policy: bool = True,
) -> EnterpriseIdentity:
    identity_service = IdentityService(session)
    identity = identity_service.create_identity(employee_id=employee_id or f"employee-{sender_id}")
    identity_service.create_mapping(
        platform="wechat",
        account_id=SOURCE_ACCOUNT_ID,
        sender_id=sender_id,
        enterprise_identity_id=identity.id,
    )
    if create_user_policy:
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


def assert_resource_counts(
    session: Session, *, messages: int, workspaces: int, threads: int
) -> None:
    assert session.scalar(select(func.count()).select_from(Message)) == messages
    assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == workspaces
    assert session.scalar(select(func.count()).select_from(AIThread)) == threads


def assert_access_denied(
    session: Session,
    outcome: MessageIngestionOutcome,
    reason_code: ReasonCode,
) -> Message:
    assert outcome.message_created is True
    assert outcome.admission.admitted is False
    assert outcome.admission.reason is AdmissionReason.ACCESS_DENIED
    assert outcome.admission.authorization is not None
    assert outcome.admission.authorization.reason_code is reason_code
    assert outcome.should_create_task is False
    assert outcome.workspace_id is None
    assert outcome.ai_thread_id is None
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)
    persisted = session.get(Message, outcome.message_id)
    assert persisted is not None
    return persisted


def test_allowlisted_private_message_is_persisted_then_admitted(session: Session) -> None:
    identity = provision_sender(session)
    allow_gateway(session)
    message = normalized_message()

    outcome = MessageAdmissionService(session).process(message)

    assert isinstance(outcome, MessageIngestionOutcome)
    assert outcome.message_created is True
    assert outcome.admission.message_id == outcome.message_id
    assert outcome.admission.admitted is True
    assert outcome.admission.reason is AdmissionReason.ALLOWED
    assert outcome.admission.enterprise_identity_id == identity.id
    assert outcome.should_create_task is True
    assert outcome.workspace_id == outcome.admission.workspace_id
    assert outcome.ai_thread_id == outcome.admission.ai_thread_id
    persisted = session.get(Message, outcome.message_id)
    assert persisted is not None
    assert persisted.content == message.content
    assert_resource_counts(session, messages=1, workspaces=1, threads=1)
    with pytest.raises(FrozenInstanceError):
        outcome.message_created = False  # type: ignore[misc]


def test_authorized_contact_mentioning_bot_in_group_is_persisted_then_admitted(
    session: Session,
) -> None:
    provision_sender(session)
    allow_gateway(session)

    outcome = MessageAdmissionService(session).process(group_message())

    assert outcome.message_created is True
    assert outcome.admission.admitted is True
    assert outcome.admission.reason is AdmissionReason.ALLOWED
    assert outcome.should_create_task is True
    assert outcome.workspace_id is not None
    assert outcome.ai_thread_id is not None
    assert outcome.admission.authorization is not None
    assert outcome.admission.authorization.is_mentioned is True
    assert_resource_counts(session, messages=1, workspaces=1, threads=1)


def test_group_without_mention_is_saved_without_workspace(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)

    outcome = MessageAdmissionService(session).process(group_message(is_mentioned=False))

    persisted = assert_access_denied(session, outcome, ReasonCode.BOT_NOT_MENTIONED)
    assert persisted.is_mentioned is False


def test_non_allowlisted_message_is_saved_without_workspace(session: Session) -> None:
    provision_sender(session, create_user_policy=False)
    allow_gateway(session)

    outcome = MessageAdmissionService(session).process(normalized_message())

    assert_access_denied(session, outcome, ReasonCode.USER_NOT_ALLOWED)


def test_unmapped_human_message_is_saved_without_workspace(session: Session) -> None:
    allow_gateway(session)

    outcome = MessageAdmissionService(session).process(normalized_message())

    persisted = assert_access_denied(session, outcome, ReasonCode.IDENTITY_UNRESOLVED)
    assert persisted.sender_id == SENDER_ID
    assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 0
    assert session.scalar(select(func.count()).select_from(SourceIdentityMapping)) == 0


def test_self_message_is_saved_and_short_circuited(session: Session) -> None:
    outcome = MessageAdmissionService(session).process(normalized_message(is_self=True))

    assert outcome.message_created is True
    assert outcome.admission.admitted is False
    assert outcome.admission.reason is AdmissionReason.SELF_MESSAGE
    assert outcome.should_create_task is False
    assert outcome.workspace_id is None
    assert outcome.ai_thread_id is None
    assert outcome.admission.authorization is None
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)


def test_senderless_system_message_is_saved_and_short_circuited(session: Session) -> None:
    message = group_message(
        sender_type=WechatSenderType.SYSTEM,
        sender_id=None,
        sender_name=None,
        message_type=WechatMessageType.SYSTEM,
        raw_type=10000,
        is_mentioned=False,
    )

    outcome = MessageAdmissionService(session).process(message)

    assert outcome.message_created is True
    assert outcome.admission.admitted is False
    assert outcome.admission.reason is AdmissionReason.SYSTEM_MESSAGE
    assert outcome.should_create_task is False
    persisted = session.get(Message, outcome.message_id)
    assert persisted is not None
    assert persisted.sender_type == "system"
    assert persisted.sender_id is None
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)
    assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 0


def test_duplicate_physical_message_returns_same_message_id(session: Session) -> None:
    service = MessageAdmissionService(session)
    message = normalized_message()

    first = service.process(message)
    second = service.process(message)

    assert first.message_created is True
    assert second.message_created is False
    assert second.message_id == first.message_id
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)


def test_duplicate_allowed_message_reuses_workspace_and_ai_thread(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)
    service = MessageAdmissionService(session)
    message = normalized_message()

    first = service.process(message)
    second = service.process(message)

    assert first.admission.admitted is True
    assert second.admission.admitted is True
    assert second.message_created is False
    assert second.message_id == first.message_id
    assert second.workspace_id == first.workspace_id
    assert second.ai_thread_id == first.ai_thread_id
    assert_resource_counts(session, messages=1, workspaces=1, threads=1)


class _AdmissionFailure(RuntimeError):
    pass


class _FailingAdmissionOrchestrator:
    def admit(self, candidate: AdmissionCandidate) -> AdmissionOutcome:
        raise _AdmissionFailure(f"admission failed for message {candidate.message_id}")


def test_sink_propagates_admission_failure_after_message_commit(session: Session) -> None:
    service = MessageAdmissionService(
        session,
        admission_orchestrator=_FailingAdmissionOrchestrator(),  # type: ignore[arg-type]
    )
    sink = MessageStoreAdmissionSink(service)

    with pytest.raises(_AdmissionFailure, match="admission failed"):
        sink.handle(normalized_message())

    session.rollback()
    persisted = session.scalar(select(Message))
    assert persisted is not None
    assert persisted.event_id == "wechat:event-001"
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)


def test_retry_of_message_after_admission_failure_can_succeed(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)
    message = normalized_message()
    failing_service = MessageAdmissionService(
        session,
        admission_orchestrator=_FailingAdmissionOrchestrator(),  # type: ignore[arg-type]
    )
    with pytest.raises(_AdmissionFailure):
        failing_service.process(message)

    outcome = MessageAdmissionService(session).process(message)

    assert outcome.message_created is False
    assert outcome.admission.admitted is True
    assert outcome.workspace_id is not None
    assert outcome.ai_thread_id is not None
    assert_resource_counts(session, messages=1, workspaces=1, threads=1)


class _TrackingSession(Session):
    created: list[_TrackingSession] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.rollback_calls = 0
        self.close_calls = 0
        self.created.append(self)

    def rollback(self) -> None:
        self.rollback_calls += 1
        super().rollback()

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class _FailOnceResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts:
        del message
        self.calls += 1
        if self.calls == 1:
            raise _AdmissionFailure("controlled resolver failure")
        return RequestFacts(
            requested_scope=frozenset(),
            requested_skill_ids=frozenset(),
            risk_level=RiskLevel.NORMAL,
        )


class _CleanupFailingSession(_TrackingSession):
    def rollback(self) -> None:
        super().rollback()
        raise RuntimeError("controlled rollback cleanup failure")

    def close(self) -> None:
        super().close()
        raise RuntimeError("controlled close cleanup failure")


def test_session_factory_sink_uses_and_closes_a_fresh_session_per_message() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    _TrackingSession.created = []
    factory = sessionmaker(
        bind=engine,
        class_=_TrackingSession,
        autoflush=False,
        expire_on_commit=False,
    )
    sink = SessionFactoryMessageStoreAdmissionSink(factory)
    try:
        sink.handle(normalized_message())
        sink.handle(
            normalized_message(
                event_id="wechat:event-002",
                source_message_id="server-002",
                source_local_id="local-002",
                source_server_id="server-002",
            )
        )

        assert len(_TrackingSession.created) == 2
        assert _TrackingSession.created[0] is not _TrackingSession.created[1]
        assert [item.close_calls for item in _TrackingSession.created] == [1, 1]
        assert [item.rollback_calls for item in _TrackingSession.created] == [0, 0]
    finally:
        engine.dispose()


def test_session_factory_sink_rolls_back_closes_and_retries_committed_message() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    _TrackingSession.created = []
    factory = sessionmaker(
        bind=engine,
        class_=_TrackingSession,
        autoflush=False,
        expire_on_commit=False,
    )
    resolver = _FailOnceResolver()
    sink = SessionFactoryMessageStoreAdmissionSink(factory, resolver)
    message = normalized_message()
    try:
        with pytest.raises(_AdmissionFailure, match="controlled resolver failure"):
            sink.handle(message)

        failed_session = _TrackingSession.created[0]
        assert failed_session.rollback_calls == 1
        assert failed_session.close_calls == 1
        with Session(engine) as verification_session:
            persisted = verification_session.scalar(select(Message))
            assert persisted is not None
            persisted_message_id = persisted.id

        retry = sink.process(message)

        assert len(_TrackingSession.created) == 2
        assert _TrackingSession.created[1] is not failed_session
        assert _TrackingSession.created[1].close_calls == 1
        assert retry.message_created is False
        assert retry.message_id == persisted_message_id
        with Session(engine) as verification_session:
            assert verification_session.scalar(select(func.count()).select_from(Message)) == 1
    finally:
        engine.dispose()


def test_session_factory_sink_preserves_processing_error_when_cleanup_fails() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    _TrackingSession.created = []
    factory = sessionmaker(
        bind=engine,
        class_=_CleanupFailingSession,
        autoflush=False,
        expire_on_commit=False,
    )
    sink = SessionFactoryMessageStoreAdmissionSink(factory, _FailOnceResolver())
    try:
        with pytest.raises(_AdmissionFailure, match="controlled resolver failure"):
            sink.handle(normalized_message())

        failed_session = _TrackingSession.created[0]
        assert failed_session.rollback_calls == 1
        assert failed_session.close_calls == 1
    finally:
        engine.dispose()


class _RecordingResolver:
    def __init__(self) -> None:
        self.messages: list[PersistedMessageSnapshot] = []

    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts:
        self.messages.append(message)
        return RequestFacts(
            requested_scope=frozenset({"persisted-scope"}),
            requested_skill_ids=frozenset({"persisted-skill"}),
            risk_level=RiskLevel.HIGH,
        )


class _RecordingAdmissionOrchestrator:
    def __init__(self) -> None:
        self.candidates: list[AdmissionCandidate] = []

    def admit(self, candidate: AdmissionCandidate) -> AdmissionOutcome:
        self.candidates.append(candidate)
        return AdmissionOutcome(
            message_id=candidate.message_id,
            admitted=False,
            should_create_task=False,
            reason=AdmissionReason.ACCESS_DENIED,
        )


def test_candidate_and_resolver_use_authoritative_persisted_message(session: Session) -> None:
    resolver = _RecordingResolver()
    orchestrator = _RecordingAdmissionOrchestrator()
    service = MessageAdmissionService(
        session,
        request_resolver=resolver,
        admission_orchestrator=orchestrator,  # type: ignore[arg-type]
    )
    original = normalized_message(
        content="authoritative body",
        sender_name="Persisted Alice",
    )
    first = service.process(original)
    duplicate_with_conflicting_facts = normalized_message(
        event_id=original.event_id,
        source_account_id=original.source_account_id,
        source_message_id=original.source_message_id,
        conversation_id=original.conversation_id,
        conversation_type=WechatConversationType.GROUP,
        sender_type=WechatSenderType.SYSTEM,
        sender_id=None,
        sender_name="Untrusted Override",
        message_type=WechatMessageType.SYSTEM,
        raw_type=10000,
        content="untrusted duplicate body",
        is_self=True,
        is_mentioned=True,
    )

    second = service.process(duplicate_with_conflicting_facts)

    assert first.message_created is True
    assert second.message_created is False
    assert second.message_id == first.message_id
    request_message = resolver.messages[-1]
    assert isinstance(request_message, PersistedMessageSnapshot)
    assert not isinstance(request_message, Message)
    assert request_message.message_id == first.message_id
    assert request_message.content == "authoritative body"
    assert request_message.sender_name == "Persisted Alice"
    candidate = orchestrator.candidates[-1]
    assert candidate.message_id == first.message_id
    assert candidate.source == "wechat"
    assert candidate.source_account_id == SOURCE_ACCOUNT_ID
    assert candidate.conversation_id == PRIVATE_CONVERSATION_ID
    assert candidate.conversation_type.value == "private"
    assert candidate.sender_type.value == "human"
    assert candidate.sender_id == SENDER_ID
    assert candidate.is_self is False
    assert candidate.is_mentioned is None
    assert candidate.message_type == "text"
    assert candidate.requested_scope == frozenset({"persisted-scope"})
    assert candidate.requested_skill_ids == frozenset({"persisted-skill"})
    assert candidate.risk_level is RiskLevel.HIGH
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)


def test_default_resolver_does_not_infer_requests_from_message_text_or_names(
    session: Session,
) -> None:
    orchestrator = _RecordingAdmissionOrchestrator()
    service = MessageAdmissionService(
        session,
        request_resolver=DefaultAdmissionRequestResolver(),
        admission_orchestrator=orchestrator,  # type: ignore[arg-type]
    )
    message = normalized_message(
        sender_name="Administrator @root",
        conversation_name="critical-admin-skills",
        content="@gateway enable admin skill for secrets.txt at CRITICAL risk",
    )

    service.process(message)

    candidate = orchestrator.candidates[-1]
    assert candidate.requested_scope == frozenset()
    assert candidate.requested_skill_ids == frozenset()
    assert candidate.risk_level is RiskLevel.NORMAL


class _MutationAttemptResolver:
    def __init__(self, **mutations: object) -> None:
        self._mutations = mutations
        self.rejected_fields: set[str] = set()

    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts:
        for field_name, value in self._mutations.items():
            try:
                setattr(message, field_name, value)
            except FrozenInstanceError:
                self.rejected_fields.add(field_name)
        return RequestFacts(
            requested_scope=frozenset(),
            requested_skill_ids=frozenset(),
            risk_level=RiskLevel.NORMAL,
        )


def test_resolver_and_identity_map_cannot_override_persisted_source_facts(
    session: Session,
) -> None:
    provision_sender(session)
    allow_gateway(session)
    message = group_message(is_mentioned=False)
    first = MessageAdmissionService(session).process(message)
    persisted = session.get(Message, first.message_id)
    assert persisted is not None
    persisted.is_mentioned = True
    resolver = _MutationAttemptResolver(is_mentioned=True)

    outcome = MessageAdmissionService(session, resolver).process(message)

    assert outcome.message_created is False
    assert outcome.admission.reason is AdmissionReason.ACCESS_DENIED
    assert outcome.admission.authorization is not None
    assert outcome.admission.authorization.reason_code is ReasonCode.BOT_NOT_MENTIONED
    assert resolver.rejected_fields == {"is_mentioned"}
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)
    session.commit()
    session.expire_all()
    reloaded = session.get(Message, first.message_id)
    assert reloaded is not None
    assert reloaded.is_mentioned is False


def test_resolver_cannot_clear_persisted_self_flag(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)
    resolver = _MutationAttemptResolver(is_self=False)

    outcome = MessageAdmissionService(session, resolver).process(normalized_message(is_self=True))

    assert outcome.admission.reason is AdmissionReason.SELF_MESSAGE
    assert outcome.admission.authorization is None
    assert resolver.rejected_fields == {"is_self"}
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)
    session.commit()
    session.expire_all()
    persisted = session.get(Message, outcome.message_id)
    assert persisted is not None
    assert persisted.is_self is True


def test_resolver_cannot_reclassify_persisted_system_sender(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)
    resolver = _MutationAttemptResolver(sender_type="human", sender_id=SENDER_ID)
    message = normalized_message(
        sender_type=WechatSenderType.SYSTEM,
        sender_id=None,
        sender_name=None,
        message_type=WechatMessageType.TEXT,
        raw_type=10000,
    )

    outcome = MessageAdmissionService(session, resolver).process(message)

    assert outcome.admission.reason is AdmissionReason.SYSTEM_MESSAGE
    assert outcome.admission.authorization is None
    assert resolver.rejected_fields == {"sender_id", "sender_type"}
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)
    session.commit()
    session.expire_all()
    persisted = session.get(Message, outcome.message_id)
    assert persisted is not None
    assert persisted.sender_type == "system"
    assert persisted.sender_id is None
    assert persisted.message_type == "text"


class _SnapshotBypassingResolver:
    def __init__(self) -> None:
        self.messages: list[PersistedMessageSnapshot] = []

    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts:
        self.messages.append(message)
        mutations: dict[str, object] = {
            "message_id": -1,
            "source": "untrusted-source",
            "source_account_id": "untrusted-account",
            "conversation_id": "untrusted@chatroom",
            "conversation_type": "group",
            "sender_type": "system",
            "sender_id": None,
            "is_self": True,
            "is_mentioned": True,
            "message_type": "system",
        }
        for field_name, value in mutations.items():
            object.__setattr__(message, field_name, value)
        return RequestFacts(
            requested_scope=frozenset({"persisted-scope"}),
            requested_skill_ids=frozenset({"persisted-skill"}),
            risk_level=RiskLevel.HIGH,
        )


def test_candidate_and_database_ignore_resolver_snapshot_mutations(session: Session) -> None:
    resolver = _SnapshotBypassingResolver()
    orchestrator = _RecordingAdmissionOrchestrator()
    outcome = MessageAdmissionService(
        session,
        request_resolver=resolver,
        admission_orchestrator=orchestrator,  # type: ignore[arg-type]
    ).process(normalized_message())

    request_message = resolver.messages[-1]
    assert request_message.source == "untrusted-source"
    candidate = orchestrator.candidates[-1]
    assert candidate.message_id == outcome.message_id
    assert candidate.source == "wechat"
    assert candidate.source_account_id == SOURCE_ACCOUNT_ID
    assert candidate.conversation_id == PRIVATE_CONVERSATION_ID
    assert candidate.conversation_type.value == "private"
    assert candidate.sender_type.value == "human"
    assert candidate.sender_id == SENDER_ID
    assert candidate.is_self is False
    assert candidate.is_mentioned is None
    assert candidate.message_type == "text"
    assert candidate.requested_scope == frozenset({"persisted-scope"})
    assert candidate.requested_skill_ids == frozenset({"persisted-skill"})
    assert candidate.risk_level is RiskLevel.HIGH
    assert not session.dirty

    session.commit()
    session.expire_all()
    persisted = session.get(Message, outcome.message_id)
    assert persisted is not None
    assert persisted.source == "wechat"
    assert persisted.source_account_id == SOURCE_ACCOUNT_ID
    assert persisted.conversation_id == PRIVATE_CONVERSATION_ID
    assert persisted.conversation_type == "private"
    assert persisted.sender_type == "human"
    assert persisted.sender_id == SENDER_ID
    assert persisted.is_self is False
    assert persisted.is_mentioned is None
    assert persisted.message_type == "text"


def test_different_employees_in_same_group_reuse_ai_thread(session: Session) -> None:
    first_identity = provision_sender(
        session,
        sender_id="wxid-alice",
        employee_id="employee-alice",
    )
    second_identity = provision_sender(
        session,
        sender_id="wxid-bob",
        employee_id="employee-bob",
    )
    allow_gateway(session)
    service = MessageAdmissionService(session)

    first = service.process(group_message())
    second = service.process(
        group_message(
            event_id="wechat:event-002",
            source_message_id="server-002",
            source_local_id="local-002",
            source_server_id="server-002",
            sender_id="wxid-bob",
            sender_name="Bob",
            timestamp=datetime(2026, 8, 1, 10, 16, tzinfo=UTC),
        )
    )

    assert first.admission.enterprise_identity_id == first_identity.id
    assert second.admission.enterprise_identity_id == second_identity.id
    assert first.workspace_id != second.workspace_id
    assert first.ai_thread_id == second.ai_thread_id
    assert_resource_counts(session, messages=2, workspaces=2, threads=1)


def test_same_group_applies_contact_policy_to_each_sender(session: Session) -> None:
    authorized_identity = provision_sender(
        session,
        sender_id="wxid-alice",
        employee_id="employee-alice",
    )
    unauthorized_identity = provision_sender(
        session,
        sender_id="wxid-bob",
        employee_id="employee-bob",
        create_user_policy=False,
    )
    allow_gateway(session)
    service = MessageAdmissionService(session)

    allowed = service.process(group_message())
    denied = service.process(
        group_message(
            event_id="wechat:event-002",
            source_message_id="server-002",
            source_local_id="local-002",
            source_server_id="server-002",
            sender_id="wxid-bob",
            sender_name="Bob",
            timestamp=datetime(2026, 8, 1, 10, 16, tzinfo=UTC),
        )
    )

    assert allowed.admission.admitted is True
    assert allowed.admission.enterprise_identity_id == authorized_identity.id
    assert denied.admission.admitted is False
    assert denied.admission.authorization is not None
    assert denied.admission.authorization.enterprise_identity_id == unauthorized_identity.id
    assert denied.admission.authorization.reason_code is ReasonCode.USER_NOT_ALLOWED
    assert denied.should_create_task is False
    assert denied.workspace_id is None
    assert denied.ai_thread_id is None
    assert_resource_counts(session, messages=2, workspaces=1, threads=1)


def test_private_message_does_not_require_mention(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)

    outcome = MessageAdmissionService(session).process(normalized_message(is_mentioned=None))

    assert outcome.admission.admitted is True
    assert outcome.admission.authorization is not None
    assert outcome.admission.authorization.is_mentioned is None


def test_sink_handle_returns_none_and_process_returns_outcome(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)
    sink = MessageStoreAdmissionSink(MessageAdmissionService(session))
    message = normalized_message()

    assert sink.handle(message) is None
    outcome = sink.process(message)

    assert isinstance(outcome, MessageIngestionOutcome)
    assert outcome.message_created is False
    assert outcome.admission.admitted is True
    assert_resource_counts(session, messages=1, workspaces=1, threads=1)


class _MissingReadMessageStore:
    def __init__(self, session: Session) -> None:
        self.delegate = MessageStore(session)

    def create(self, event: object) -> tuple[Message, bool]:
        return self.delegate.create(event)  # type: ignore[arg-type]

    def get(self, message_id: int) -> None:
        return None


def test_missing_committed_message_read_raises_without_losing_history(session: Session) -> None:
    store = _MissingReadMessageStore(session)
    service = MessageAdmissionService(
        session,
        message_store=store,  # type: ignore[arg-type]
    )

    with pytest.raises(PersistedMessageNotFoundError) as error:
        service.process(normalized_message())

    persisted = session.scalar(select(Message))
    assert persisted is not None
    assert error.value.message_id == persisted.id
    assert_resource_counts(session, messages=1, workspaces=0, threads=0)
