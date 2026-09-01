from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RequestFacts, RiskLevel
from cf_agent_gateway.adapters.wechat import (
    NormalizedWechatMessage,
    WechatConversationType,
    WechatMessageType,
    WechatSenderType,
    wechat_message_to_event,
)
from cf_agent_gateway.admission import (
    AdmissionCandidate,
    AdmissionDecision,
    AdmissionEvidenceOrigin,
    AdmissionInvariantError,
    AdmissionOutcome,
    AdmissionOutcomeState,
    AdmissionPendingError,
    AdmissionReason,
    MessageAdmissionOutcome,
    MessageAdmissionOutcomeStore,
)
from cf_agent_gateway.agent_profile import AgentProfileStore
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import MessageAdmissionService, PersistedMessageSnapshot
from cf_agent_gateway.message.models import Conversation
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.task.model import HermesDispatchRecord

SOURCE_ACCOUNT_ID = "wxid-gateway"
CONVERSATION_ID = "wxid-alice"
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
        "conversation_id": CONVERSATION_ID,
        "conversation_type": WechatConversationType.PRIVATE,
        "conversation_name": "Alice",
        "sender_type": WechatSenderType.HUMAN,
        "sender_id": SENDER_ID,
        "sender_name": "Alice",
        "message_type": WechatMessageType.TEXT,
        "raw_type": 1,
        "content": "durable admission fixture",
        "timestamp": datetime(2026, 8, 23, 2, 0, tzinfo=UTC),
        "is_mentioned": None,
        "is_self": False,
        "reply": None,
    }
    values.update(overrides)
    return NormalizedWechatMessage.model_validate(values)


def provision_sender(
    session: Session,
    *,
    permission_scope: frozenset[str] = frozenset(),
    allowed_skills: frozenset[str] = frozenset(),
) -> EnterpriseIdentity:
    identity_service = IdentityService(session)
    identity = identity_service.create_identity(employee_id="employee-alice")
    identity_service.create_mapping(
        platform="wechat",
        account_id=SOURCE_ACCOUNT_ID,
        sender_id=SENDER_ID,
        enterprise_identity_id=identity.id,
    )
    AccessPolicyService(session).upsert_user_policy(
        enterprise_identity_id=identity.id,
        enabled=True,
        permission_scope=permission_scope,
        allowed_skills=allowed_skills,
    )
    return identity


def allow_gateway(
    session: Session,
    *,
    permission_scope: frozenset[str] = frozenset(),
    allowed_skills: frozenset[str] = frozenset(),
) -> None:
    AccessPolicyService(session).upsert_gateway_policy(
        enabled=True,
        permission_scope=permission_scope,
        allowed_skills=allowed_skills,
        allowed_risk_levels={RiskLevel.NORMAL},
    )


class _NeverResolver:
    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts:
        del message
        raise AssertionError("durable admission replay must not resolve request facts")


class _NeverAdmissionOrchestrator:
    def admit(self, candidate: AdmissionCandidate) -> AdmissionOutcome:
        del candidate
        raise AssertionError("durable admission replay must not evaluate policy or routing")


def test_checkpoint_redelivery_replays_denial_without_reevaluation_after_policy_change(
    session: Session,
) -> None:
    allow_gateway(session)
    message = normalized_message()
    first = MessageAdmissionService(session).process(message)
    assert first.admission.admitted is False

    provision_sender(session)
    replay = MessageAdmissionService(
        session,
        request_resolver=_NeverResolver(),
        admission_orchestrator=_NeverAdmissionOrchestrator(),  # type: ignore[arg-type]
    ).process(message)

    assert replay.message_created is False
    assert replay.message_id == first.message_id
    assert replay.admission.admitted is False
    assert replay.admission.reason is AdmissionReason.ACCESS_DENIED
    assert replay.dispatch_record_id is None
    assert session.scalar(select(func.count()).select_from(MessageAdmissionOutcome)) == 1
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0


def test_legacy_message_without_dispatch_replays_fail_closed(session: Session) -> None:
    message_id = _persist_message(session)
    session.add(
        MessageAdmissionOutcome(
            message_id=message_id,
            state=AdmissionOutcomeState.COMPLETED,
            decision=AdmissionDecision.UNRESOLVED,
            evidence_origin=AdmissionEvidenceOrigin.LEGACY_UNRESOLVED,
            admission_reason=AdmissionReason.LEGACY_UNRESOLVED.value,
            should_create_task=False,
            routing_mode="legacy",
            attempt_count=0,
            completed_at=datetime.now(UTC),
        )
    )
    session.commit()

    replay = MessageAdmissionService(
        session,
        request_resolver=_NeverResolver(),
        admission_orchestrator=_NeverAdmissionOrchestrator(),  # type: ignore[arg-type]
    ).process(normalized_message())

    assert replay.message_created is False
    assert replay.admission.admitted is False
    assert replay.admission.reason is AdmissionReason.LEGACY_UNRESOLVED
    assert replay.should_create_task is False
    assert replay.dispatch_record_id is None
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0


def test_completed_allowed_reuses_target_and_only_repairs_missing_dispatch(
    session: Session,
) -> None:
    identity = provision_sender(session)
    allow_gateway(session)
    message = normalized_message()
    first = MessageAdmissionService(session).process(message)
    assert first.dispatch_record_id is not None
    original_target = (first.workspace_id, first.ai_thread_id)

    session.execute(
        delete(HermesDispatchRecord).where(HermesDispatchRecord.id == first.dispatch_record_id)
    )
    session.commit()
    AccessPolicyService(session).upsert_user_policy(
        enterprise_identity_id=identity.id,
        enabled=False,
    )

    replay = MessageAdmissionService(
        session,
        request_resolver=_NeverResolver(),
        admission_orchestrator=_NeverAdmissionOrchestrator(),  # type: ignore[arg-type]
    ).process(message)

    assert replay.message_created is False
    assert replay.admission.admitted is True
    assert (replay.workspace_id, replay.ai_thread_id) == original_target
    assert replay.dispatch_record_id is not None
    assert replay.hermes_dispatch is None
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_late_legacy_dispatch_is_adopted_without_policy_reevaluation(session: Session) -> None:
    identity = provision_sender(session)
    allow_gateway(session)
    message = normalized_message()
    first = MessageAdmissionService(session).process(message)
    assert first.dispatch_record_id is not None
    original_target = (first.workspace_id, first.ai_thread_id)

    legacy_dispatch = session.get(HermesDispatchRecord, first.dispatch_record_id)
    assert legacy_dispatch is not None
    legacy_dispatch.idempotency_key = "legacy-dispatch-key"
    session.execute(delete(MessageAdmissionOutcome))
    session.commit()
    AccessPolicyService(session).upsert_user_policy(
        enterprise_identity_id=identity.id,
        enabled=False,
    )

    replay = MessageAdmissionService(
        session,
        request_resolver=_NeverResolver(),
        admission_orchestrator=_NeverAdmissionOrchestrator(),  # type: ignore[arg-type]
    ).process(message)

    assert replay.admission.admitted is True
    assert (replay.workspace_id, replay.ai_thread_id) == original_target
    assert replay.dispatch_record_id == first.dispatch_record_id
    stored = session.scalar(select(MessageAdmissionOutcome))
    assert stored is not None
    assert stored.evidence_origin is AdmissionEvidenceOrigin.LEGACY_DISPATCH
    assert stored.decision is AdmissionDecision.ALLOWED
    assert legacy_dispatch.idempotency_key == "legacy-dispatch-key"
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_stale_pending_adopts_legacy_dispatch_and_clears_request_snapshot(
    session: Session,
) -> None:
    provision_sender(session)
    allow_gateway(session)
    message = normalized_message()
    first = MessageAdmissionService(session).process(message)
    assert first.dispatch_record_id is not None

    session.execute(delete(MessageAdmissionOutcome))
    session.commit()
    MessageAdmissionOutcomeStore(session).create_pending(
        message_id=first.message_id,
        request=RequestFacts(
            requested_scope=frozenset({"stale-scope"}),
            requested_skill_ids=frozenset({"stale-skill"}),
            risk_level=RiskLevel.HIGH,
        ),
        lease_seconds=1,
        now=datetime(2020, 1, 1, tzinfo=UTC),
    )

    replay = MessageAdmissionService(
        session,
        request_resolver=_NeverResolver(),
        admission_orchestrator=_NeverAdmissionOrchestrator(),  # type: ignore[arg-type]
    ).process(message)

    assert replay.admission.admitted is True
    assert replay.dispatch_record_id == first.dispatch_record_id
    stored = session.scalar(select(MessageAdmissionOutcome))
    assert stored is not None
    assert stored.evidence_origin is AdmissionEvidenceOrigin.LEGACY_DISPATCH
    assert stored.requested_scope is None
    assert stored.requested_skill_ids is None
    assert stored.risk_level is None
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_denied_outcome_with_existing_dispatch_fails_closed(session: Session) -> None:
    provision_sender(session)
    allow_gateway(session)
    message = normalized_message()
    first = MessageAdmissionService(session).process(message)
    assert first.dispatch_record_id is not None

    session.execute(delete(MessageAdmissionOutcome))
    session.add(
        MessageAdmissionOutcome(
            message_id=first.message_id,
            state=AdmissionOutcomeState.COMPLETED,
            decision=AdmissionDecision.UNRESOLVED,
            evidence_origin=AdmissionEvidenceOrigin.LEGACY_UNRESOLVED,
            admission_reason=AdmissionReason.LEGACY_UNRESOLVED.value,
            should_create_task=False,
            routing_mode="legacy",
            attempt_count=0,
            completed_at=datetime.now(UTC),
        )
    )
    session.commit()

    with pytest.raises(AdmissionInvariantError, match="conflicts with an existing dispatch"):
        MessageAdmissionService(session).process(message)


class _FixedRequestResolver:
    def resolve(self, message: PersistedMessageSnapshot) -> RequestFacts:
        del message
        return RequestFacts(
            requested_scope=frozenset({"messages:read"}),
            requested_skill_ids=frozenset({"summarize"}),
            risk_level=RiskLevel.NORMAL,
        )


def test_v2_outcome_persists_request_policy_profile_and_route_references(
    session: Session,
) -> None:
    scope = frozenset({"messages:read", "messages:write"})
    skills = frozenset({"search", "summarize"})
    identity = provision_sender(session, permission_scope=scope, allowed_skills=skills)
    allow_gateway(session, permission_scope=scope, allowed_skills=skills)
    conversation = Conversation(
        source="wechat",
        source_account_id=SOURCE_ACCOUNT_ID,
        conversation_id=CONVERSATION_ID,
        conversation_type="private",
    )
    session.add(conversation)
    session.commit()
    profile, created = AgentProfileStore(session).create_agent_profile(
        profile_key="durable-runtime",
        revision=7,
        provider="hermes",
        external_profile_ref="profiles/durable-runtime/7",
        model="hermes-agent",
    )
    assert created is True
    AgentProfileStore(session).bind_conversation_agent_profile(
        conversation_record_id=conversation.id,
        agent_profile_id=profile.id,
    )

    outcome = MessageAdmissionService(
        session,
        request_resolver=_FixedRequestResolver(),
        v2_routing_enabled=True,
    ).process(normalized_message())

    stored = session.scalar(select(MessageAdmissionOutcome))
    assert stored is not None
    assert stored.message_id == outcome.message_id
    assert stored.state is AdmissionOutcomeState.COMPLETED
    assert stored.decision is AdmissionDecision.ALLOWED
    assert stored.evidence_origin is AdmissionEvidenceOrigin.RUNTIME
    assert stored.requested_scope == frozenset({"messages:read"})
    assert stored.requested_skill_ids == frozenset({"summarize"})
    assert stored.risk_level == RiskLevel.NORMAL.value
    assert stored.authorization_reason_code == "allowed"
    assert stored.authorization_snapshot is not None
    assert stored.authorization_snapshot["enterprise_identity_id"] == identity.id
    assert stored.authorization_snapshot["permission_scope"] == ["messages:read"]
    assert stored.authorization_snapshot["allowed_skills"] == ["summarize"]
    assert stored.policy_snapshot is not None
    assert stored.policy_snapshot["gateway"]["permission_scope"] == sorted(scope)
    assert stored.policy_snapshot["gateway"]["allowed_skills"] == sorted(skills)
    assert stored.policy_snapshot["user"]["permission_scope"] == sorted(scope)
    assert stored.policy_snapshot["user"]["allowed_skills"] == sorted(skills)
    assert stored.gateway_policy_id is not None
    assert stored.gateway_policy_updated_at is not None
    assert stored.user_policy_id is not None
    assert stored.user_policy_updated_at is not None
    assert stored.routing_mode == "v2"
    assert stored.route_conversation_record_id == conversation.id
    assert stored.agent_profile_id == profile.id
    assert stored.agent_profile_key == profile.profile_key
    assert stored.agent_profile_reference == profile.external_profile_ref
    assert stored.agent_profile_revision == profile.revision
    assert stored.thread_policy == "private_sender"


def _persist_message(session: Session) -> int:
    persisted, created = MessageStore(session).create(wechat_message_to_event(normalized_message()))
    assert created is True
    return persisted.id


def test_live_pending_claim_fails_closed_without_resolver_or_policy_evaluation(
    session: Session,
) -> None:
    message_id = _persist_message(session)
    request = RequestFacts(
        requested_scope=frozenset({"stored-scope"}),
        requested_skill_ids=frozenset({"stored-skill"}),
        risk_level=RiskLevel.HIGH,
    )
    claim = MessageAdmissionOutcomeStore(session).create_pending(
        message_id=message_id,
        request=request,
        lease_seconds=600,
    )
    assert claim.should_evaluate is True

    with pytest.raises(AdmissionPendingError):
        MessageAdmissionService(
            session,
            request_resolver=_NeverResolver(),
            admission_orchestrator=_NeverAdmissionOrchestrator(),  # type: ignore[arg-type]
        ).process(normalized_message())

    stored = session.get(MessageAdmissionOutcome, claim.outcome.id)
    assert stored is not None
    assert stored.state is AdmissionOutcomeState.PENDING
    assert stored.claim_token == claim.claim_token
    assert stored.attempt_count == 1


class _RecordingDeniedOrchestrator:
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


def test_stale_pending_claim_recovers_from_stored_request_snapshot(session: Session) -> None:
    message_id = _persist_message(session)
    stored_request = RequestFacts(
        requested_scope=frozenset({"stored-scope"}),
        requested_skill_ids=frozenset({"stored-skill"}),
        risk_level=RiskLevel.HIGH,
    )
    MessageAdmissionOutcomeStore(session).create_pending(
        message_id=message_id,
        request=stored_request,
        lease_seconds=1,
        now=datetime(2020, 1, 1, tzinfo=UTC),
    )
    orchestrator = _RecordingDeniedOrchestrator()

    recovered = MessageAdmissionService(
        session,
        request_resolver=_NeverResolver(),
        admission_orchestrator=orchestrator,  # type: ignore[arg-type]
    ).process(normalized_message())

    assert recovered.admission.admitted is False
    assert len(orchestrator.candidates) == 1
    candidate = orchestrator.candidates[0]
    assert candidate.requested_scope == stored_request.requested_scope
    assert candidate.requested_skill_ids == stored_request.requested_skill_ids
    assert candidate.risk_level is RiskLevel.HIGH
    stored = session.scalar(select(MessageAdmissionOutcome))
    assert stored is not None
    assert stored.state is AdmissionOutcomeState.COMPLETED
    assert stored.decision is AdmissionDecision.DENIED
    assert stored.attempt_count == 2


def test_offset_aware_lease_is_normalized_before_sqlite_stale_takeover(
    session: Session,
) -> None:
    message_id = _persist_message(session)
    request = RequestFacts(
        requested_scope=frozenset(),
        requested_skill_ids=frozenset(),
        risk_level=RiskLevel.NORMAL,
    )
    store = MessageAdmissionOutcomeStore(session)
    first = store.create_pending(
        message_id=message_id,
        request=request,
        lease_seconds=60,
        now=datetime(2026, 8, 23, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    takeover = store.claim_pending(
        first.outcome,
        lease_seconds=60,
        now=datetime(2026, 8, 23, 2, 2, tzinfo=UTC),
    )

    assert takeover.should_evaluate is True
    assert takeover.claim_token != first.claim_token
    assert takeover.outcome.attempt_count == 2


def test_invalid_pending_request_snapshot_releases_claim(session: Session) -> None:
    message_id = _persist_message(session)
    store = MessageAdmissionOutcomeStore(session)
    store.create_pending(
        message_id=message_id,
        request=RequestFacts(
            requested_scope=frozenset(),
            requested_skill_ids=frozenset(),
            risk_level=RiskLevel.NORMAL,
        ),
        lease_seconds=1,
        now=datetime(2020, 1, 1, tzinfo=UTC),
    )
    session.execute(
        text(
            "UPDATE message_admission_outcomes "
            "SET risk_level = 'future-risk' WHERE message_id = :message_id"
        ),
        {"message_id": message_id},
    )
    session.commit()

    with pytest.raises(ValueError, match="future-risk"):
        MessageAdmissionService(
            session,
            request_resolver=_NeverResolver(),
            admission_orchestrator=_NeverAdmissionOrchestrator(),  # type: ignore[arg-type]
        ).process(normalized_message())

    stored = store.get_by_message_id(message_id)
    assert stored is not None
    assert stored.state is AdmissionOutcomeState.PENDING
    assert stored.claim_token is None
    assert stored.lease_expires_at is None
    assert stored.last_error_code == "admission_evaluation_failed"
    assert stored.attempt_count == 2


class _CompletionCrash(RuntimeError):
    pass


class _FailOnceCompletionStore(MessageAdmissionOutcomeStore):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.calls = 0

    def stage_completion(
        self,
        *,
        claim_token: str,
        admission: AdmissionOutcome,
    ) -> None:
        self.calls += 1
        if self.calls == 1:
            raise _CompletionCrash("crash after staged dispatch")
        super().stage_completion(claim_token=claim_token, admission=admission)


def test_dispatch_stage_rolls_back_when_outcome_completion_crashes_and_replay_recovers(
    session: Session,
) -> None:
    provision_sender(session)
    allow_gateway(session)
    message = normalized_message()
    failing_store = _FailOnceCompletionStore(session)

    with pytest.raises(_CompletionCrash, match="staged dispatch"):
        MessageAdmissionService(
            session,
            admission_outcome_store=failing_store,
        ).process(message)

    session.expire_all()
    pending = session.scalar(select(MessageAdmissionOutcome))
    assert pending is not None
    assert pending.state is AdmissionOutcomeState.PENDING
    assert pending.claim_token is None
    assert pending.last_error_code == "admission_evaluation_failed"
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0

    recovered = MessageAdmissionService(session).process(message)

    assert recovered.message_created is False
    assert recovered.admission.admitted is True
    assert recovered.dispatch_record_id is not None
    assert session.scalar(select(func.count()).select_from(MessageAdmissionOutcome)) == 1
    assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1
    session.refresh(pending)
    assert pending.state is AdmissionOutcomeState.COMPLETED
    assert pending.attempt_count == 2


def test_concurrent_pending_creation_has_one_authoritative_claim(tmp_path) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'admission-concurrency.db').as_posix()}"
    )
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as setup_session:
            message_id = _persist_message(setup_session)
        barrier = Barrier(2)
        request = RequestFacts(
            requested_scope=frozenset({"scope"}),
            requested_skill_ids=frozenset({"skill"}),
            risk_level=RiskLevel.NORMAL,
        )

        def acquire() -> tuple[str, str | None]:
            with factory() as worker_session:
                barrier.wait(timeout=10)
                try:
                    claim = MessageAdmissionOutcomeStore(worker_session).create_pending(
                        message_id=message_id,
                        request=request,
                        lease_seconds=600,
                    )
                except AdmissionPendingError:
                    return "pending", None
                return "claimed", claim.claim_token

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: acquire(), range(2)))

        assert sorted(result[0] for result in results) == ["claimed", "pending"]
        assert len({token for _, token in results if token is not None}) == 1
        with factory() as verification_session:
            outcomes = list(verification_session.scalars(select(MessageAdmissionOutcome)))
            assert len(outcomes) == 1
            assert outcomes[0].state is AdmissionOutcomeState.PENDING
            assert outcomes[0].attempt_count == 1
    finally:
        engine.dispose()
