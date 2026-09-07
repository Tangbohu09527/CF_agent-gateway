from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from cf_agent_gateway import initial_identity
from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.access.policy_models import GatewayAccessPolicy, UserAccessPolicy
from cf_agent_gateway.adapters.wechat import NormalizedWechatMessage, wechat_message_to_event
from cf_agent_gateway.adapters.wechat.client import AgentWechatClient
from cf_agent_gateway.adapters.wechat.diagnose import inspect_authentication
from cf_agent_gateway.agent_profile import AgentProfile, AgentProfileStatus, AgentProfileStore
from cf_agent_gateway.database import create_database_engine, initialize_database
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.initial_identity import (
    InitialIdentityConfig,
    InitialIdentityError,
    initialize_identity,
)
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.message.schemas import ConversationCreate
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.routing.resolver import RouteResolver
from cf_agent_gateway.task.model import HermesDispatchRecord


def approved_input() -> dict:
    return {
        "version": 1,
        "profile": {
            "profile_key": "approved-test-profile",
            "revision": 1,
            "provider": "hermes",
            "external_profile_ref": "profiles/test-only/1",
            "model": "hermes-agent",
        },
        "identity": {"employee_id": "employee-test-only", "display_name": "Test operator"},
        "wechat": {
            "account_id": "wxid-test-bot",
            "sender_id": "wxid-approved-sender",
            "conversation_id": "wxid-approved-sender",
        },
    }


@pytest.fixture
def config() -> InitialIdentityConfig:
    return InitialIdentityConfig.model_validate(approved_input())


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_database_engine(f"sqlite:///{tmp_path / 'initial.db'}")
    initialize_database(database)
    try:
        yield database
    finally:
        database.dispose()


def incoming(*, event_id: str, sender_id: str = "wxid-approved-sender") -> NormalizedWechatMessage:
    return NormalizedWechatMessage(
        event_id=event_id,
        source="wechat",
        source_account_id="wxid-test-bot",
        source_message_id=event_id,
        source_message_id_is_fallback=False,
        sender_type="human",
        raw_type=1,
        is_mentioned=None,
        conversation_id="wxid-approved-sender",
        conversation_type="private",
        is_self=False,
        sender_id=sender_id,
        message_type="text",
        content=f"unique-install-check-{event_id}",
        timestamp=datetime(2026, 9, 6, 1, tzinfo=UTC),
    )


def test_empty_database_route_executes_only_approved_sender_and_deduplicates(
    engine: Engine, config: InitialIdentityConfig
) -> None:
    result = initialize_identity(engine, config)
    assert result["status"] == "ready"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        service = MessageAdmissionService(session, v2_routing_enabled=True)
        event = incoming(event_id="unique-install-approved")
        first = service.process(event)
        repeated = service.process(event)
        rejected = service.process(
            incoming(event_id="unique-install-unapproved", sender_id="wxid-not-approved")
        )
        assert first.should_create_task is True
        assert first.ai_thread_id is not None
        assert first.dispatch_record_id is not None
        assert repeated.message_id == first.message_id
        assert repeated.dispatch_record_id == first.dispatch_record_id
        assert repeated.message_created is False
        assert rejected.should_create_task is False
        assert rejected.dispatch_record_id is None
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 1
        assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 1


def test_rerun_preserves_ids_names_and_business_messages(
    engine: Engine, config: InitialIdentityConfig
) -> None:
    initialize_identity(engine, config)
    with Session(engine) as session:
        profile_id = session.scalar(select(AgentProfile.id))
        identity_id = session.scalar(select(EnterpriseIdentity.id))
        conversation = session.scalar(select(Conversation))
        conversation.conversation_name = "Operator-managed conversation name"
        session.commit()
        MessageStore(session).create(wechat_message_to_event(incoming(event_id="retained-message")))
    assert initialize_identity(engine, config)["created_stages"] == []
    assert initialize_identity(engine, config, check_only=True)["created_stages"] == []
    with Session(engine) as session:
        assert session.scalar(select(AgentProfile.id)) == profile_id
        assert session.scalar(select(EnterpriseIdentity.id)) == identity_id
        assert session.scalar(select(Conversation.conversation_name)) == (
            "Operator-managed conversation name"
        )
        assert session.scalar(select(Message.content)) == "unique-install-check-retained-message"


def test_check_on_empty_database_never_creates_anything(
    engine: Engine, config: InitialIdentityConfig
) -> None:
    with pytest.raises(InitialIdentityError) as error:
        initialize_identity(engine, config, check_only=True)
    assert error.value.code == "initial_identity_missing"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 0


def test_late_failure_rolls_back_every_stage_then_retry_succeeds(
    engine: Engine, config: InitialIdentityConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_resolve = RouteResolver.resolve

    def fail_resolution(*args, **kwargs):
        raise RuntimeError("injected interruption after all configuration stages")

    monkeypatch.setattr(RouteResolver, "resolve", fail_resolution)
    with pytest.raises(RuntimeError, match="injected interruption"):
        initialize_identity(engine, config)
    with Session(engine) as session:
        for model in (
            AgentProfile,
            EnterpriseIdentity,
            SourceIdentityMapping,
            UserAccessPolicy,
            GatewayAccessPolicy,
            Conversation,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    monkeypatch.setattr(RouteResolver, "resolve", real_resolve)
    assert initialize_identity(engine, config)["status"] == "ready"


def test_existing_partial_matching_profile_is_resumed(
    engine: Engine, config: InitialIdentityConfig
) -> None:
    with Session(engine) as session:
        profile, _ = AgentProfileStore(session).create_agent_profile(**config.profile.model_dump())
        original_id = profile.id
    result = initialize_identity(engine, config)
    assert "profile" not in result["created_stages"]
    assert "profile_binding" in result["created_stages"]
    with Session(engine) as session:
        assert session.scalar(select(AgentProfile.id)) == original_id


@pytest.mark.parametrize(
    "stage", ["profile", "identity", "source_mapping", "user_policy", "gateway_policy"]
)
def test_disabled_configuration_is_never_reenabled(
    engine: Engine, config: InitialIdentityConfig, stage: str
) -> None:
    initialize_identity(engine, config)
    with Session(engine) as session:
        if stage == "profile":
            row = session.scalar(select(AgentProfile))
            row.status = AgentProfileStatus.DISABLED
        elif stage == "identity":
            row = session.scalar(select(EnterpriseIdentity))
            row.status = IdentityStatus.DISABLED
        else:
            model = {
                "source_mapping": SourceIdentityMapping,
                "user_policy": UserAccessPolicy,
                "gateway_policy": GatewayAccessPolicy,
            }[stage]
            row = session.scalar(select(model))
            row.enabled = False
        session.commit()
    with pytest.raises(InitialIdentityError) as error:
        initialize_identity(engine, config)
    assert error.value.stage == stage
    assert error.value.code == "initial_identity_conflict"
    with Session(engine) as session:
        if stage == "profile":
            assert session.scalar(select(AgentProfile.status)) == AgentProfileStatus.DISABLED
        elif stage == "identity":
            assert session.scalar(select(EnterpriseIdentity.status)) == IdentityStatus.DISABLED
        else:
            assert session.scalar(select(model.enabled)) is False


def test_existing_policy_scope_is_not_replaced(
    engine: Engine, config: InitialIdentityConfig
) -> None:
    with Session(engine) as session:
        AccessPolicyService(session).upsert_gateway_policy(
            permission_scope={"already-approved"}, allowed_risk_levels={RiskLevel.NORMAL}
        )
    with pytest.raises(InitialIdentityError, match="gateway_policy"):
        initialize_identity(engine, config)
    with Session(engine) as session:
        policy = session.scalar(select(GatewayAccessPolicy))
        assert policy.permission_scope == {"already-approved"}
        assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 0


def test_existing_mapping_cannot_be_claimed_by_another_employee(
    engine: Engine, config: InitialIdentityConfig
) -> None:
    with Session(engine) as session:
        service = IdentityService(session)
        owner = service.create_identity(employee_id="existing-owner")
        service.create_mapping(
            platform="wechat",
            account_id=config.wechat.account_id,
            sender_id=config.wechat.sender_id,
            enterprise_identity_id=owner.id,
        )
        owner_id = owner.id
    with pytest.raises(InitialIdentityError, match="source_mapping"):
        initialize_identity(engine, config)
    with Session(engine) as session:
        assert session.scalar(select(SourceIdentityMapping.enterprise_identity_id)) == owner_id
        assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 1
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 0


def test_existing_profile_binding_is_preserved(
    engine: Engine, config: InitialIdentityConfig
) -> None:
    initialize_identity(engine, config)
    revised = config.model_copy(
        update={
            "profile": config.profile.model_copy(update={"revision": 2}),
        }
    )
    with pytest.raises(InitialIdentityError, match="profile_binding"):
        initialize_identity(engine, revised)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 1


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("wechat", "sender_id", "*"),
        ("wechat", "conversation_id", "test@chatroom"),
        ("wechat", "account_id", "REPLACE_WITH_ACCOUNT"),
        ("profile", "external_profile_ref", " "),
        ("profile", "revision", True),
        ("identity", "employee_id", ""),
        ("profile", "provider", "openai-compatible"),
    ],
)
def test_configuration_requires_explicit_private_identity(section, key, value) -> None:
    payload = deepcopy(approved_input())
    payload[section][key] = value
    with pytest.raises(ValidationError):
        InitialIdentityConfig.model_validate(payload)


def test_unknown_config_keys_fail() -> None:
    payload = approved_input()
    payload["allow_all"] = True
    with pytest.raises(ValidationError):
        InitialIdentityConfig.model_validate(payload)


def test_conversation_configuration_preserves_existing_data(engine: Engine) -> None:
    target = ConversationCreate(
        source="wechat",
        source_account_id="bot",
        conversation_id="sender",
        conversation_type="private",
    )
    with Session(engine) as session:
        store = MessageStore(session)
        created, is_new = store.prepare_conversation(target)
        same, repeated_is_new = store.prepare_conversation(target)
        assert created.id == same.id
        assert is_new is True
        assert repeated_is_new is False
        assert session.scalar(select(func.count()).select_from(Message)) == 0


def cli_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: Engine) -> Path:
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(
        "runtime:\n  v2_routing_enabled: true\n"
        "wechat:\n  enabled: true\n  base_url: http://wechat.unit.invalid:6174\n",
        encoding="utf-8",
    )
    input_path = tmp_path / "identity.json"
    input_path.write_text(json.dumps(approved_input()), encoding="utf-8")
    monkeypatch.setenv("CF_GATEWAY_CONFIG", str(config_path))
    monkeypatch.setenv("CF_AGENT_GATEWAY_DATABASE_URL", str(engine.url))
    return input_path


def stub_auth_transport(monkeypatch: pytest.MonkeyPatch, payload: object) -> list[str]:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/status/auth"
        assert request.headers["Authorization"] == "Bearer synthetic-cli-token"
        requests.append(request.url.path)
        return httpx.Response(200, json=payload)

    def inspect(settings):
        return inspect_authentication(
            settings,
            environment_reader=lambda name: (
                "synthetic-cli-token" if name == settings.wechat.token_env else None
            ),
            client_factory=lambda base_url, token: AgentWechatClient(
                base_url, token, transport=httpx.MockTransport(respond)
            ),
        )

    monkeypatch.setattr(initial_identity, "inspect_authentication", inspect)
    return requests


def test_cli_uses_runtime_database_and_checks_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: Engine, capsys
) -> None:
    input_path = cli_inputs(tmp_path, monkeypatch, engine)
    requests = stub_auth_transport(
        monkeypatch, {"status": "logged_in", "loggedInUser": "wxid-test-bot"}
    )
    assert initial_identity.main(["--input", str(input_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready"
    assert initial_identity.main(["--input", str(input_path), "--check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["check_only"] is True
    assert result["created_stages"] == []
    assert requests == ["/api/status/auth"]


@pytest.mark.parametrize(
    "yaml,error_stage",
    [
        ("runtime:\n  v2_routing_enabled: false\n", "runtime.v2_routing_enabled"),
        ("runtime:\n  v2_routing_enabled: true\nhermes:\n  model: different\n", "profile.model"),
    ],
)
def test_cli_rejects_inconsistent_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: Engine, capsys, yaml, error_stage
) -> None:
    input_path = cli_inputs(tmp_path, monkeypatch, engine)
    (tmp_path / "gateway.yaml").write_text(yaml, encoding="utf-8")
    assert initial_identity.main(["--input", str(input_path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["stage"] == error_stage
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 0


def test_cli_does_not_echo_invalid_secret_input(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "bad-input.json"
    input_path.write_text('{"secret": "never-print-this-secret"}', encoding="utf-8")
    assert initial_identity.main(["--input", str(input_path)]) == 1
    output = capsys.readouterr()
    assert "never-print-this-secret" not in output.err + output.out
    assert json.loads(output.err) == {"error_code": "initial_identity_failed", "stage": "input"}


@pytest.mark.parametrize(
    "payload, error_code",
    [
        (
            {"status": "logged_in", "loggedInUser": "different-current-account"},
            "wechat_account_changed",
        ),
        ({"status": "not_logged_in"}, "wechat_not_logged_in"),
        ({"status": "logged_in"}, "wechat_invalid_account"),
        (
            {"status": "logged_in", "loggedInUser": {"secret": "MUST-NOT-PRINT"}},
            "wechat_invalid_account",
        ),
        ({"status": 42, "loggedInUser": "wxid-test-bot"}, "wechat_invalid_account"),
    ],
)
def test_legacy_cli_rejects_unmatched_or_invalid_auth_before_creating_business_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    capsys: pytest.CaptureFixture,
    payload: object,
    error_code: str,
) -> None:
    input_path = cli_inputs(tmp_path, monkeypatch, engine)
    requests = stub_auth_transport(monkeypatch, payload)
    assert initial_identity.main(["--input", str(input_path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error_code": error_code, "stage": "wechat_auth"}
    assert "MUST-NOT-PRINT" not in output.err
    assert "different-current-account" not in output.err
    assert requests == ["/api/status/auth"]
    with Session(engine) as session:
        for model in (AgentProfile, EnterpriseIdentity, SourceIdentityMapping, UserAccessPolicy):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_legacy_check_does_not_require_running_wechat_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    config: InitialIdentityConfig,
    capsys: pytest.CaptureFixture,
) -> None:
    initialize_identity(engine, config)
    input_path = cli_inputs(tmp_path, monkeypatch, engine)

    def prohibited_auth(settings):
        raise AssertionError("read-only check must not inspect authentication")

    monkeypatch.setattr(initial_identity, "inspect_authentication", prohibited_auth)
    assert initial_identity.main(["--input", str(input_path), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["check_only"] is True


def test_legacy_auth_unexpected_error_does_not_echo_credentials_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    capsys: pytest.CaptureFixture,
) -> None:
    input_path = cli_inputs(tmp_path, monkeypatch, engine)

    def failed_auth(settings):
        raise RuntimeError("Secret:DO-NOT-PRINT")

    monkeypatch.setattr(initial_identity, "inspect_authentication", failed_auth)
    assert initial_identity.main(["--input", str(input_path)]) == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {
        "error_code": "initial_identity_failed",
        "stage": "wechat_auth",
    }
    assert "DO-NOT-PRINT" not in output.out + output.err
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EnterpriseIdentity)) == 0
