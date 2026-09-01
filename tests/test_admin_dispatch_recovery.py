from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.admin.auth import (
    ADMIN_BEARER_TOKEN_ENV,
    get_authenticated_roles,
)
from cf_agent_gateway.admin.recovery import (
    DispatchRecoveryStateConflictError,
    DispatchRecoveryStore,
)
from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.config import APISettings, HermesSettings, Settings, WechatSettings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.response.store import DeliveryTarget, ResponseStore
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchRecoveryAudit,
    HermesDispatchStatus,
)
from cf_agent_gateway.workspace.models import EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService

ADMIN_TOKEN = "admin-recovery-test-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
CUSTOM_ADMIN_TOKEN_ENV = "TEST_RECOVERY_ADMIN_VALUE"
CUSTOM_API_TOKEN_ENV = "TEST_RECOVERY_API_VALUE"
CUSTOM_WECHAT_TOKEN_ENV = "TEST_RECOVERY_WECHAT_VALUE"
CUSTOM_HERMES_KEY_ENV = "TEST_RECOVERY_HERMES_VALUE"
CUSTOM_RECOVERY_SECRETS = {
    "admin": (CUSTOM_ADMIN_TOKEN_ENV, "A9m4N7q2R5s8", "A9m4N7q2R5s8"),
    "api": (CUSTOM_API_TOKEN_ENV, "B8n3P6r1S4t7", "B8n3P6r1S4t7"),
    "wechat": (CUSTOM_WECHAT_TOKEN_ENV, "  C7p2Q5s8T3u6  ", "  C7p2Q5s8T3u6  "),
    "hermes": (CUSTOM_HERMES_KEY_ENV, "  D6q1R4t7U2v5  ", "D6q1R4t7U2v5"),
}


@dataclass(frozen=True, slots=True)
class RecoveryFixture:
    record_id: int
    message_id: int
    thread_id: str
    following_record_id: int | None


def _session_factory(client: TestClient) -> sessionmaker[Session]:
    return client.app.state.database_session_factory


def _recovery_state_snapshot(
    factory: sessionmaker[Session],
    record_id: int,
) -> tuple[tuple[tuple[str, object], ...], tuple[tuple[object, ...], ...]]:
    with factory() as session:
        record = session.get(HermesDispatchRecord, record_id)
        assert record is not None
        dispatch = tuple(
            (column.key, getattr(record, column.key))
            for column in HermesDispatchRecord.__table__.columns
        )
        audits = tuple(
            tuple(row)
            for row in session.execute(
                select(*HermesDispatchRecoveryAudit.__table__.columns).order_by(
                    HermesDispatchRecoveryAudit.id
                )
            )
        )
    return dispatch, audits


def _configure_custom_recovery_secrets(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    settings = Settings(
        database=client.app.state.settings.database,
        api=APISettings(
            token_env=CUSTOM_API_TOKEN_ENV,
            admin_token_env=CUSTOM_ADMIN_TOKEN_ENV,
        ),
        wechat=WechatSettings(token_env=CUSTOM_WECHAT_TOKEN_ENV),
        hermes=HermesSettings(api_key_env=CUSTOM_HERMES_KEY_ENV),
    )
    client.app.state.settings = settings
    for environment_name, raw_value, _ in CUSTOM_RECOVERY_SECRETS.values():
        monkeypatch.setenv(environment_name, raw_value)
    return settings


def _recovery_body(reference: str = "INC-2026-001") -> dict[str, str]:
    return {
        "operator": "on-call@example.test",
        "reference": reference,
        "reason": "Hermes execution was checked by the on-call operator",
    }


def _create_uncertain(
    factory: sessionmaker[Session],
    suffix: str,
    *,
    with_following: bool = False,
) -> RecoveryFixture:
    with factory() as session:
        identity = IdentityService(session).create_identity(employee_id=f"employee-{suffix}")
        thread = WorkspaceService(session).ensure_thread_for_authorized_request(
            enterprise_identity_id=identity.id,
            platform="wechat",
            account_id=f"account-{suffix}",
            physical_conversation_id=f"conversation-{suffix}",
            conversation_type="private",
            sender_id=f"sender-{suffix}",
        )
        workspace = session.get(EmployeeWorkspace, thread.workspace_id)
        assert workspace is not None
        message = _create_message(session, suffix, ordinal=1)
        store = HermesDispatchRecordStore(session)
        record, created = store.enqueue(
            _admission(message.id, identity.id, workspace.id, thread.id)
        )
        assert created is True
        claimed = store.claim(record.id, claim_token=f"claim-{suffix}")
        uncertain = store.mark_uncertain(
            claimed.id,
            claim_token=f"claim-{suffix}",
            error_code="hermes_timeout",
        )
        assert uncertain.status is HermesDispatchStatus.UNCERTAIN

        following_record_id = None
        if with_following:
            following_message = _create_message(session, suffix, ordinal=2)
            following, following_created = store.enqueue(
                _admission(
                    following_message.id,
                    identity.id,
                    workspace.id,
                    thread.id,
                )
            )
            assert following_created is True
            following_record_id = following.id
        return RecoveryFixture(
            record_id=record.id,
            message_id=message.id,
            thread_id=thread.id,
            following_record_id=following_record_id,
        )


def _create_message(session: Session, suffix: str, *, ordinal: int) -> Message:
    message, created = MessageStore(session).create(
        MessageEvent(
            event_id=f"event-{suffix}-{ordinal}",
            source="wechat",
            source_account_id=f"account-{suffix}",
            source_message_id=f"source-{suffix}-{ordinal}",
            conversation_id=f"conversation-{suffix}",
            conversation_type="private",
            is_mentioned=None,
            is_self=False,
            sender_type="human",
            sender_id=f"sender-{suffix}",
            sender_name="Recovery operator test",
            message_type="text",
            content=f"message {ordinal}",
            timestamp=datetime(2026, 8, 23, 1, ordinal, tzinfo=UTC),
        )
    )
    assert created is True
    return message


def _admission(
    message_id: int,
    identity_id: str,
    workspace_id: str,
    thread_id: str,
) -> AdmissionOutcome:
    return AdmissionOutcome(
        message_id=message_id,
        admitted=True,
        should_create_task=True,
        reason=AdmissionReason.ALLOWED,
        enterprise_identity_id=identity_id,
        workspace_id=workspace_id,
        ai_thread_id=thread_id,
    )


def test_admin_bearer_is_fail_closed_and_inspect_is_redacted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_uncertain(_session_factory(client), "auth", with_following=True)
    path = f"/admin/dispatches/{fixture.record_id}"
    monkeypatch.delenv(ADMIN_BEARER_TOKEN_ENV, raising=False)

    assert client.get(path).status_code == 401
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401

    response = client.get(path, headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "dispatch_record_id": fixture.record_id,
        "status": "uncertain",
        "message_id": fixture.message_id,
        "ai_thread_id": fixture.thread_id,
        "attempt_count": 1,
        "last_error_code": "hermes_timeout",
        "has_dispatch_response": False,
        "has_hermes_response": False,
        "has_delivery": False,
        "blocks_following_dispatch": True,
        "created_at": response.json()["created_at"],
        "updated_at": response.json()["updated_at"],
        "claimed_at": response.json()["claimed_at"],
        "completed_at": response.json()["completed_at"],
        "lease_expires_at": None,
    }
    assert "content" not in response.text


def test_admin_bearer_rejects_non_ascii_bytes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    monkeypatch.setenv(settings.api.admin_token_env, ADMIN_TOKEN)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer \xff")],
            "app": app,
        }
    )

    with pytest.raises(HTTPException) as raised:
        get_authenticated_roles(request)

    assert raised.value.status_code == 401


def test_retry_approved_is_idempotent_and_consumed_once_past_retry_budget(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "retry")
    path = f"/admin/dispatches/{fixture.record_id}/retry-approved"
    body = _recovery_body()

    first = client.post(path, headers=ADMIN_HEADERS, json=body)
    replay = client.post(path, headers=ADMIN_HEADERS, json=body)

    assert first.status_code == 200
    assert first.json()["status"] == "failed"
    assert first.json()["idempotent"] is False
    assert replay.status_code == 200
    assert replay.json()["audit_id"] == first.json()["audit_id"]
    assert replay.json()["idempotent"] is True
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        assert record is not None
        assert record.attempt_count == 1
        assert record.manual_retry_approved is True
        claimed = HermesDispatchRecordStore(session).claim(
            record.id,
            claim_token="approved-retry",
            retry_limit=0,
        )
        assert claimed.attempt_count == 2
        assert claimed.manual_retry_approved is False
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 1


def test_unauthenticated_recovery_never_mutates_dispatch(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "unauthenticated-write")
    path = f"/admin/dispatches/{fixture.record_id}/mark-dead"
    monkeypatch.delenv(ADMIN_BEARER_TOKEN_ENV, raising=False)

    missing = client.post(path, json=_recovery_body("INC-NO-AUTH"))
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    invalid = client.post(
        path,
        headers={"Authorization": "Bearer invalid"},
        json=_recovery_body("INC-BAD-AUTH"),
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        assert record is not None and record.status is HermesDispatchStatus.UNCERTAIN
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 0


def test_same_reference_with_changed_metadata_is_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "reference-conflict")
    path = f"/admin/dispatches/{fixture.record_id}/mark-dead"
    body = _recovery_body("INC-REFERENCE-CONFLICT")
    assert client.post(path, headers=ADMIN_HEADERS, json=body).status_code == 200
    body["reason"] = "a different operator rationale"

    conflict = client.post(path, headers=ADMIN_HEADERS, json=body)

    assert conflict.status_code == 409
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 1


def test_mark_dead_releases_following_thread_dispatch(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "dead", with_following=True)
    assert fixture.following_record_id is not None

    response = client.post(
        f"/admin/dispatches/{fixture.record_id}/mark-dead",
        headers=ADMIN_HEADERS,
        json=_recovery_body("INC-2026-DEAD"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "dead"
    with factory() as session:
        claimed = HermesDispatchRecordStore(session).claim_next(claim_token="next-thread-head")
        assert claimed is not None
        assert claimed.id == fixture.following_record_id


def test_confirm_success_requires_raw_evidence_and_never_creates_delivery(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "confirm")
    path = f"/admin/dispatches/{fixture.record_id}/confirm-success"

    rejected = client.post(
        path,
        headers=ADMIN_HEADERS,
        json=_recovery_body("INC-2026-NO-EVIDENCE"),
    )
    assert rejected.status_code == 409

    with factory() as session:
        session.add(
            HermesDispatchResponse(
                dispatch_record_id=fixture.record_id,
                assistant_content="already persisted raw result",
            )
        )
        session.commit()

    confirmed = client.post(
        path,
        headers=ADMIN_HEADERS,
        json=_recovery_body("INC-2026-CONFIRM"),
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "success"
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        assert record is not None and record.status is HermesDispatchStatus.SUCCESS
        assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 0
        assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 0
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 1


def test_confirm_success_with_normalized_response_does_not_recreate_missing_delivery(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "confirm-normalized")
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        message = session.get(Message, fixture.message_id)
        assert record is not None and message is not None
        outcome = HermesDispatchOutcome(
            message_id=record.message_id,
            workspace_id=record.workspace_id,
            ai_thread_id=record.ai_thread_id,
            assistant_content="matching persisted result",
        )
        session.add(
            HermesDispatchResponse(
                dispatch_record_id=fixture.record_id,
                assistant_content=outcome.assistant_content,
            )
        )
        session.commit()
        _, delivery, _ = ResponseStore(session).save_generated(
            outcome,
            target=DeliveryTarget(
                channel=message.source,
                account_id=message.source_account_id,
                conversation_id=message.conversation_id,
            ),
        )
        session.delete(delivery)
        session.commit()

    path = f"/admin/dispatches/{fixture.record_id}/confirm-success"
    body = _recovery_body("INC-2026-CONFIRM-NORMALIZED")
    confirmed = client.post(path, headers=ADMIN_HEADERS, json=body)
    replay = client.post(path, headers=ADMIN_HEADERS, json=body)

    assert confirmed.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
        assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 0


@pytest.mark.parametrize("raw_content", ["", "different persisted raw result"])
def test_confirm_success_rejects_invalid_or_mismatched_response_evidence(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    raw_content: str,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, f"invalid-evidence-{len(raw_content)}")
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        message = session.get(Message, fixture.message_id)
        assert record is not None and message is not None
        session.add(
            HermesDispatchResponse(
                dispatch_record_id=fixture.record_id,
                assistant_content=raw_content,
            )
        )
        session.commit()
        if raw_content:
            _, delivery, _ = ResponseStore(session).save_generated(
                HermesDispatchOutcome(
                    message_id=record.message_id,
                    workspace_id=record.workspace_id,
                    ai_thread_id=record.ai_thread_id,
                    assistant_content="canonical normalized result",
                ),
                target=DeliveryTarget(
                    channel=message.source,
                    account_id=message.source_account_id,
                    conversation_id=message.conversation_id,
                ),
            )
            session.delete(delivery)
            session.commit()

    response = client.post(
        f"/admin/dispatches/{fixture.record_id}/confirm-success",
        headers=ADMIN_HEADERS,
        json=_recovery_body(f"INC-INVALID-EVIDENCE-{len(raw_content)}"),
    )

    assert response.status_code == 409
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        assert record is not None and record.status is HermesDispatchStatus.UNCERTAIN
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operator", "operator\x00injected"),
        ("reference", "INC-1\x7f"),
        ("reason", "approved\u0085metadata"),
        ("reason", "Authorization: Bearer credential"),
        ("reason", "password=credential"),
        ("reason", "x" * 1025),
    ],
)
def test_recovery_metadata_boundary_rejects_unsafe_values_without_mutation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, f"unsafe-{field}-{len(value)}")
    body = _recovery_body()
    body[field] = value

    response = client.post(
        f"/admin/dispatches/{fixture.record_id}/mark-dead",
        headers=ADMIN_HEADERS,
        json=body,
    )

    assert response.status_code == 422
    assert value not in response.text
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        assert record is not None and record.status is HermesDispatchStatus.UNCERTAIN
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 0


@pytest.mark.parametrize("field", ("operator", "reference", "reason"))
@pytest.mark.parametrize("secret_source", tuple(CUSTOM_RECOVERY_SECRETS))
def test_configured_recovery_secrets_are_rejected_without_any_database_change(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    secret_source: str,
    field: str,
) -> None:
    settings = _configure_custom_recovery_secrets(client, monkeypatch)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, f"configured-secret-{secret_source}-{field}")
    before = _recovery_state_snapshot(factory, fixture.record_id)
    _, _, embedded_value = CUSTOM_RECOVERY_SECRETS[secret_source]
    body = _recovery_body(f"INC-CONFIGURED-{secret_source}-{field}")
    body[field] = f"approved-{embedded_value}-metadata"
    admin_secret = CUSTOM_RECOVERY_SECRETS["admin"][1]

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            f"/admin/dispatches/{fixture.record_id}/mark-dead",
            headers={"Authorization": f"Bearer {admin_secret}"},
            json=body,
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "recovery metadata may not contain secret material"}
    assert _recovery_state_snapshot(factory, fixture.record_id) == before
    assert settings.api.admin_token_env == CUSTOM_ADMIN_TOKEN_ENV
    for _, raw_value, _ in CUSTOM_RECOVERY_SECRETS.values():
        for candidate in {raw_value, raw_value.strip()}:
            if candidate:
                assert candidate not in response.text
                assert candidate not in caplog.text


@pytest.mark.parametrize("action", ("retry-approved", "mark-dead", "confirm-success"))
def test_every_recovery_action_uses_configured_secret_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    action: str,
) -> None:
    configured_secret = "E5r8V3x6Y1z4"
    settings: Settings = client.app.state.settings
    monkeypatch.setenv(settings.api.admin_token_env, configured_secret)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, f"secret-action-{action}")
    before = _recovery_state_snapshot(factory, fixture.record_id)
    body = _recovery_body(f"INC-RUNTIME-ACTION-{action}")
    body["reason"] = f"upstream evidence {configured_secret} was reviewed"

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            f"/admin/dispatches/{fixture.record_id}/{action}",
            headers={"Authorization": f"Bearer {configured_secret}"},
            json=body,
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "recovery metadata may not contain secret material"}
    assert configured_secret not in response.text
    assert configured_secret not in caplog.text
    assert _recovery_state_snapshot(factory, fixture.record_id) == before


def test_seven_character_configured_values_are_not_treated_as_secrets(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short_value = "Ab3dE5f"
    settings: Settings = client.app.state.settings
    for environment_name in (
        settings.api.admin_token_env,
        settings.api.token_env,
        settings.wechat.token_env,
        settings.hermes.api_key_env,
    ):
        monkeypatch.setenv(environment_name, short_value)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "short-configured-value")
    body = _recovery_body("INC-SHORT-CONFIGURED-VALUE")
    body["reason"] = f"upstream evidence {short_value} was reviewed"

    response = client.post(
        f"/admin/dispatches/{fixture.record_id}/mark-dead",
        headers={"Authorization": f"Bearer {short_value}"},
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["status"] == HermesDispatchStatus.DEAD.value
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        assert record is not None and record.status is HermesDispatchStatus.DEAD
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 1


def test_admin_recovery_rejects_oversized_body_before_state_change(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADMIN_BEARER_TOKEN_ENV, ADMIN_TOKEN)
    factory = _session_factory(client)
    fixture = _create_uncertain(factory, "oversized")

    response = client.post(
        f"/admin/dispatches/{fixture.record_id}/mark-dead",
        headers={**ADMIN_HEADERS, "Content-Type": "application/json"},
        content=b"x" * 1_048_577,
    )

    assert response.status_code == 413
    with factory() as session:
        record = session.get(HermesDispatchRecord, fixture.record_id)
        assert record is not None and record.status is HermesDispatchStatus.UNCERTAIN


def test_concurrent_different_resolutions_have_one_cas_winner(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'recovery-cas.db'}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        fixture = _create_uncertain(factory, "concurrent")
        barrier = Barrier(2)

        def resolve(action: str) -> str:
            barrier.wait()
            with factory() as session:
                store = DispatchRecoveryStore(session)
                try:
                    if action == "dead":
                        store.mark_dead(
                            fixture.record_id,
                            operator="operator-a",
                            reference="INC-CONCURRENT-A",
                            reason="upstream result cannot be safely retried",
                        )
                    else:
                        store.retry_approved(
                            fixture.record_id,
                            operator="operator-b",
                            reference="INC-CONCURRENT-B",
                            reason="upstream execution was verified absent",
                        )
                except DispatchRecoveryStateConflictError:
                    return "conflict"
                return "success"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(resolve, ("dead", "retry")))

        assert sorted(results) == ["conflict", "success"]
        with factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(HermesDispatchRecoveryAudit)) == 1
            )
    finally:
        engine.dispose()
