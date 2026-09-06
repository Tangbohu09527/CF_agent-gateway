from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from cf_agent_gateway import business_access as business
from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.access.policy_store import AccessPolicyStore
from cf_agent_gateway.adapters.wechat import NormalizedWechatMessage, wechat_message_to_event
from cf_agent_gateway.agent_profile import AgentProfile, AgentProfileStore
from cf_agent_gateway.database import create_database_engine, initialize_database
from cf_agent_gateway.identity.models import EnterpriseIdentity, SourceIdentityMapping
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.initial_identity import InitialIdentityConfig, InitialIdentityError
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.task.model import HermesDispatchRecord


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_database_engine(f"sqlite:///{tmp_path / 'business.db'}")
    initialize_database(database)
    try:
        yield database
    finally:
        database.dispose()


@pytest.fixture
def config() -> business.BusinessApprovalConfig:
    return business.BusinessApprovalConfig(
        version=1,
        profile={
            "profile_key": "approved-profile",
            "revision": 1,
            "provider": "hermes",
            "external_profile_ref": "profiles/approved/1",
            "model": "hermes-agent",
        },
        identity={"employee_id": "approved-employee", "display_name": "Approved employee"},
    )


def incoming(event_id: str, **changes: object) -> NormalizedWechatMessage:
    values = {
        "event_id": event_id,
        "source": "wechat",
        "source_account_id": "wxid-authenticated-bot",
        "source_message_id": event_id,
        "source_message_id_is_fallback": False,
        "sender_type": "human",
        "raw_type": 1,
        "is_mentioned": None,
        "conversation_id": "stable-private-chat-id",
        "conversation_type": "private",
        "is_self": False,
        "sender_id": "wxid-observed-employee",
        "sender_name": "SECRET-DO-NOT-PRINT-NAME",
        "message_type": "text",
        "content": "SECRET-DO-NOT-PRINT-MESSAGE",
        "timestamp": datetime(2026, 9, 6, 1, tzinfo=UTC),
    }
    return NormalizedWechatMessage(**(values | changes))


def persist(engine: Engine, message: NormalizedWechatMessage) -> int:
    with Session(engine) as session:
        stored, _ = MessageStore(session).create(wechat_message_to_event(message))
        return stored.id


def approve(engine: Engine, config: business.BusinessApprovalConfig, message_id: int) -> dict:
    return business.approve(
        engine,
        message_id=message_id,
        config=config,
        authenticated_account_id="wxid-authenticated-bot",
    )


def test_empty_discovery_does_not_create_configuration(engine: Engine) -> None:
    result = business.discover(engine)
    assert result["business_state"] == "awaiting_configuration"
    assert result["end_to_end_ready"] is False
    assert result["items"] == []
    assert result["total"] == 0
    assert all(value == 0 for value in result["counts"].values())


def test_approval_does_not_replay_denied_messages_then_disable_blocks_new_messages(
    engine: Engine, config: business.BusinessApprovalConfig
) -> None:
    old = incoming("before-approval")
    with Session(engine) as session:
        denied = MessageAdmissionService(session, v2_routing_enabled=True).process(old)
        assert not denied.should_create_task
        message_id = denied.message_id
    observed = business.discover(engine)
    assert observed["total"] == 1
    item = observed["items"][0]
    assert item["message_id"] == message_id
    assert item["sender_id"] == "wxid-observed-employee"
    assert item["source_account_id"] == "wxid-authenticated-bot"
    assert item["permitted"] is False
    assert item["reason_code"] == "identity_unresolved"
    assert "SECRET-DO-NOT-PRINT" not in json.dumps(observed)
    approved = approve(engine, config, message_id)
    assert approved["permitted"] is True
    assert approved["agent_profile_id"]
    assert approved["enterprise_identity_id"]
    assert approved["replayed_messages"] == 0
    assert approved["historical_admission"]["admitted"] is False
    assert business.status(engine)["counts"]["configured_private_routes"] == 1
    assert approve(engine, config, message_id)["created_stages"] == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0
        service = MessageAdmissionService(session, v2_routing_enabled=True)
        still_denied = service.process(old)
        assert still_denied.should_create_task is False
        fresh = incoming("after-approval")
        accepted = service.process(fresh)
        repeated = service.process(fresh)
        assert accepted.should_create_task is True
        assert repeated.dispatch_record_id == accepted.dispatch_record_id
        fresh_id = accepted.message_id
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1
    current = business.status(engine, message_id=fresh_id)
    assert current["ai_thread_id"] and current["workspace_id"]
    assert current["historical_admission"]["admitted"] is True
    disabled = business.disable(engine, employee_id=config.identity.employee_id)
    assert disabled["changed"] is True
    assert disabled["enabled"] is False
    assert business.disable(engine, employee_id=config.identity.employee_id)["changed"] is False
    assert business.status(engine, message_id=message_id)["reason_code"] == "user_not_allowed"
    assert business.status(engine)["business_state"] == "awaiting_configuration"
    with pytest.raises(InitialIdentityError, match="user_policy"):
        approve(engine, config, message_id)
    with Session(engine) as session:
        service = MessageAdmissionService(session, v2_routing_enabled=True)
        assert not service.process(incoming("after-disable")).should_create_task
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1
        assert session.scalar(select(func.count()).select_from(SourceIdentityMapping)) == 1
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 1


@pytest.mark.parametrize("account", [None, "old-or-different-bot"])
def test_approval_requires_current_authenticated_account(
    engine: Engine, config: business.BusinessApprovalConfig, account: str | None
) -> None:
    message_id = persist(engine, incoming("observed"))
    with pytest.raises(business.BusinessAccessError, match="wechat_auth"):
        business.approve(
            engine, message_id=message_id, config=config, authenticated_account_id=account
        )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 0
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 0


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"is_self": True}, "inbound_human_message_required"),
        ({"sender_type": "system", "sender_id": None}, "inbound_human_message_required"),
        ({"message_type": "image"}, "text_message_required"),
        (
            {
                "conversation_type": "group",
                "conversation_id": "real@chatroom",
                "is_mentioned": True,
            },
            "private_conversation_required",
        ),
    ],
)
def test_discovery_does_not_implicitly_enable_noneligible_messages(
    engine: Engine, config: business.BusinessApprovalConfig, changes: dict, reason: str
) -> None:
    message_id = persist(engine, incoming("noneligible", **changes))
    discovery = business.discover(engine)
    assert discovery["items"][0]["approval_eligible"] is False
    assert discovery["items"][0]["approval_reason_code"] == reason
    with pytest.raises(business.BusinessAccessError, match=reason):
        approve(engine, config, message_id)
    assert business.status(engine)["counts"]["identities"] == 0


def test_existing_identity_without_route_is_not_configured(
    engine: Engine, config: business.BusinessApprovalConfig
) -> None:
    message_id = persist(engine, incoming("not-configured"))
    with Session(engine) as session:
        service = IdentityService(session)
        employee = service.create_identity(**config.identity.model_dump())
        service.create_mapping(
            platform="wechat",
            account_id="wxid-authenticated-bot",
            sender_id="wxid-observed-employee",
            enterprise_identity_id=employee.id,
        )
        policies = AccessPolicyService(session)
        policies.upsert_user_policy(enterprise_identity_id=employee.id)
        policies.upsert_gateway_policy(allowed_risk_levels={RiskLevel.NORMAL})
        AgentProfileStore(session).create_agent_profile(**config.profile.model_dump())
    overview = business.status(engine)
    assert overview["counts"]["active_identities"] == 1
    assert overview["counts"]["active_profiles"] == 1
    assert overview["business_state"] == "awaiting_configuration"
    item = business.status(engine, message_id=message_id)
    assert item["permitted"] is False
    assert item["reason_code"] == "private_conversation_profile_not_configured"
    with Session(engine) as session:
        denied = MessageAdmissionService(session, v2_routing_enabled=True).process(
            incoming("not-configured")
        )
        assert denied.admission.reason.value == "route_unavailable"
    assert approve(engine, config, message_id)["permitted"] is True
    with Session(engine) as session:
        service = MessageAdmissionService(session, v2_routing_enabled=True)
        assert not service.process(incoming("not-configured")).should_create_task
        assert service.process(incoming("new-after-route-configured")).should_create_task
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1


def test_disable_preserves_scope_skills_window_and_mapping(
    engine: Engine, config: business.BusinessApprovalConfig
) -> None:
    message_id = persist(engine, incoming("preserve"))
    approved = approve(engine, config, message_id)
    identity_id = approved["enterprise_identity_id"]
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=20)
    with Session(engine) as session:
        policy = AccessPolicyService(session).upsert_user_policy(
            enterprise_identity_id=identity_id,
            permission_scope={"approved.scope"},
            allowed_skills={"approved-skill"},
            valid_from=start,
            valid_until=end,
        )
        policy_id = policy.id
    business.disable(engine, employee_id=config.identity.employee_id)
    with Session(engine) as session:
        policy = AccessPolicyStore(session).get_user_policy(identity_id)
        assert policy.id == policy_id
        assert policy.permission_scope == {"approved.scope"}
        assert policy.allowed_skills == {"approved-skill"}
        assert policy.valid_from.replace(tzinfo=UTC) == start
        assert policy.valid_until.replace(tzinfo=UTC) == end
        assert policy.enabled is False
        assert session.scalar(select(SourceIdentityMapping.enabled)) is True


def test_approval_conflict_is_atomic_and_does_not_rebind_observed_sender(
    engine: Engine, config: business.BusinessApprovalConfig
) -> None:
    message_id = persist(engine, incoming("conflict"))
    approved = approve(engine, config, message_id)
    different = config.model_copy(deep=True)
    different.identity.employee_id = "another-employee"
    different.profile.profile_key = "another-profile"
    with pytest.raises(InitialIdentityError, match="source_mapping"):
        approve(engine, different, message_id)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 1
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 1
    assert (
        business.status(engine, message_id=message_id)["enterprise_identity_id"]
        == approved["enterprise_identity_id"]
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"wechat": {"account_id": "injected"}},
        {"version": True},
        {"version": 2},
        {"sender_id": "injected"},
    ],
)
def test_approval_schema_rejects_unimplemented_versions_and_source_overrides(
    config: business.BusinessApprovalConfig, extra: dict
) -> None:
    with pytest.raises(ValidationError):
        business.BusinessApprovalConfig.model_validate(config.model_dump() | extra)


def test_source_identifiers_are_derived_without_changing_original_record(
    engine: Engine, config: business.BusinessApprovalConfig
) -> None:
    message_id = persist(engine, incoming("derive"))
    result = approve(engine, config, message_id)
    assert result["sender_id"] == "wxid-observed-employee"
    assert result["conversation_id"] == "stable-private-chat-id"
    with Session(engine) as session:
        original = MessageStore(session).get(message_id)
        assert original.content == "SECRET-DO-NOT-PRINT-MESSAGE"
        assert session.scalar(select(func.count()).select_from(Message)) == 1
        assert session.scalar(select(func.count()).select_from(Conversation)) == 1
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0


def test_legacy_explicit_input_still_refuses_reactivation(
    engine: Engine, config: business.BusinessApprovalConfig
) -> None:
    message_id = persist(engine, incoming("legacy"))
    approve(engine, config, message_id)
    business.disable(engine, employee_id=config.identity.employee_id)
    explicit = InitialIdentityConfig(
        **config.model_dump(),
        wechat={
            "account_id": "wxid-authenticated-bot",
            "sender_id": "wxid-observed-employee",
            "conversation_id": "stable-private-chat-id",
        },
    )
    with pytest.raises(InitialIdentityError, match="user_policy"):
        business.initialize_identity(engine, explicit)


@pytest.fixture
def approval_file(tmp_path: Path, config: business.BusinessApprovalConfig) -> Path:
    path = tmp_path / "approval.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_approval_file_safe_read_and_invalid_content(approval_file: Path) -> None:
    assert business.read_approval(approval_file).identity.employee_id == "approved-employee"
    approval_file.write_text('{"password":"MUST-NOT-PRINT"}', encoding="utf-8")
    with pytest.raises(ValidationError):
        business.read_approval(approval_file)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission baseline")
@pytest.mark.parametrize("mode", [0o644, 0o660, 0o777])
def test_approval_file_rejects_unsafe_permissions(approval_file: Path, mode: int) -> None:
    approval_file.chmod(mode)
    with pytest.raises(ValueError, match="permissions"):
        business.read_approval(approval_file)


@pytest.mark.skipif(os.name != "posix", reason="POSIX links/FIFO baseline")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_approval_file_rejects_links_and_fifo_without_blocking(
    approval_file: Path, tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "unsafe"
    if kind == "symlink":
        path.symlink_to(approval_file)
    elif kind == "hardlink":
        os.link(approval_file, path)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises(ValueError, match="approval file"):
        business.read_approval(path)


def test_cli_uses_auth_only_for_approval_and_sanitizes_failures(
    engine: Engine,
    approval_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(v2_routing_enabled=True),
        hermes=SimpleNamespace(model="hermes-agent"),
        database=SimpleNamespace(url=str(engine.url)),
    )
    monkeypatch.setattr(business, "load_settings", lambda path: settings)
    calls = []

    def auth(current):
        calls.append(current)
        return {
            "authenticated": True,
            "status": "authenticated",
            "account_id": "wxid-authenticated-bot",
        }

    monkeypatch.setattr(business, "inspect_authentication", auth)
    assert business.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["business_state"] == "awaiting_configuration"
    assert calls == []
    message_id = persist(engine, incoming("cli"))
    assert (
        business.main(["approve", "--message-id", str(message_id), "--input", str(approval_file)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["permitted"] is True
    assert calls == [settings]

    def fail_auth(current):
        raise RuntimeError("PASSWORD-SHOULD-NOT-APPEAR")

    monkeypatch.setattr(business, "inspect_authentication", fail_auth)
    assert (
        business.main(["approve", "--message-id", str(message_id), "--input", str(approval_file)])
        == 1
    )
    output = capsys.readouterr()
    assert json.loads(output.err) == {
        "error_code": "business_access_failed",
        "stage": "wechat_auth",
    }
    assert "PASSWORD" not in output.err


def test_unknown_employee_or_message_is_not_created(engine: Engine) -> None:
    with pytest.raises(business.BusinessAccessError, match="employee_not_found"):
        business.disable(engine, employee_id="unknown-employee")
    with pytest.raises(business.BusinessAccessError, match="observed_message_not_found"):
        business.status(engine, message_id=12345)
    assert business.status(engine)["counts"]["identities"] == 0
