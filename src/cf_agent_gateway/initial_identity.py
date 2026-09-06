"""Create one explicitly approved V2 text route using existing business stores."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RiskLevel, evaluate_access
from cf_agent_gateway.access.models import RequestFacts
from cf_agent_gateway.access.policy_store import AccessPolicyStore
from cf_agent_gateway.adapters.wechat.diagnose import inspect_authentication
from cf_agent_gateway.agent_profile import AgentProfileStatus, AgentProfileStore
from cf_agent_gateway.config import load_settings
from cf_agent_gateway.database import check_database_migrations, create_database_engine
from cf_agent_gateway.identity.models import IdentityStatus
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.identity.store import IdentityStore
from cf_agent_gateway.message.schemas import ConversationCreate
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.routing.resolver import RouteResolver

RequiredValue = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
Item = TypeVar("Item")


class InitialIdentityError(RuntimeError):
    def __init__(self, code: str, stage: str) -> None:
        self.code = code
        self.stage = stage
        super().__init__(f"{code}: {stage}")


class InitialSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def reject_placeholders(cls, value: object) -> object:
        if isinstance(value, str) and (
            "*" in value
            or value.upper().startswith("REPLACE_")
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("explicit values are required")
        return value


class InitialProfile(InitialSchema):
    profile_key: str = Field(min_length=1, max_length=64)
    revision: int = Field(strict=True, gt=0)
    provider: Literal["hermes"]
    external_profile_ref: RequiredValue
    model: RequiredValue


class InitialBusinessIdentity(InitialSchema):
    employee_id: RequiredValue
    display_name: RequiredValue


class InitialWechatIdentity(InitialSchema):
    account_id: RequiredValue
    sender_id: RequiredValue
    conversation_id: RequiredValue

    @field_validator("conversation_id")
    @classmethod
    def private_conversation_only(cls, value: str) -> str:
        if value.endswith("@chatroom"):
            raise ValueError("first-install configuration supports a private text route only")
        return value


class InitialIdentityConfig(InitialSchema):
    version: Literal[1]
    profile: InitialProfile
    identity: InitialBusinessIdentity
    wechat: InitialWechatIdentity


def _require_same(item: object, fields: dict[str, object], stage: str) -> None:
    if any(getattr(item, name) != value for name, value in fields.items()):
        raise InitialIdentityError("initial_identity_conflict", stage)


def initialize_identity(
    engine: Engine,
    config: InitialIdentityConfig,
    *,
    check_only: bool = False,
) -> dict[str, object]:
    """All stages commit together; store commits cannot commit the outer transaction.

    SERIALIZABLE prevents a concurrent edit between a comparison and an existing
    store's upsert from silently replacing an operator's policy or binding.
    A serialization failure requires a fresh, safe retry of this entire operation.
    """
    check_database_migrations(engine)
    created: list[str] = []

    def obtain(existing: Item | None, create: Callable[[], Item], stage: str) -> Item:
        if existing is not None:
            return existing
        if check_only:
            raise InitialIdentityError("initial_identity_missing", stage)
        result = create()
        created.append(stage)
        return result

    with (
        engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection,
        connection.begin(),
        Session(
            bind=connection,
            join_transaction_mode="rollback_only",
            autoflush=False,
            expire_on_commit=False,
        ) as session,
    ):
        identities = IdentityStore(session)
        identity_service = IdentityService(session)
        profiles = AgentProfileStore(session)
        policy_store = AccessPolicyStore(session)
        policies = AccessPolicyService(session)
        messages = MessageStore(session)

        profile_fields = {**config.profile.model_dump(), "status": AgentProfileStatus.ACTIVE}
        profile = obtain(
            profiles.get_agent_profile_revision(
                config.profile.profile_key, config.profile.revision
            ),
            lambda: profiles.create_agent_profile(**profile_fields)[0],
            "profile",
        )
        _require_same(profile, profile_fields, "profile")

        identity_fields = {**config.identity.model_dump(), "status": IdentityStatus.ACTIVE}
        identity = obtain(
            identities.get_identity_by_employee_id(config.identity.employee_id),
            lambda: identity_service.create_identity(**identity_fields),
            "identity",
        )
        _require_same(identity, identity_fields, "identity")

        source_key = {
            "platform": "wechat",
            "account_id": config.wechat.account_id,
            "sender_id": config.wechat.sender_id,
        }
        mapping = obtain(
            identities.get_mapping(**source_key),
            lambda: identity_service.create_mapping(
                **source_key, enterprise_identity_id=identity.id
            ),
            "source_mapping",
        )
        _require_same(
            mapping,
            {"enterprise_identity_id": identity.id, "enabled": True},
            "source_mapping",
        )

        user_fields = {
            "enabled": True,
            "permission_scope": frozenset(),
            "allowed_skills": frozenset(),
            "valid_from": None,
            "valid_until": None,
        }
        user_policy = obtain(
            policy_store.get_user_policy(identity.id),
            lambda: policies.upsert_user_policy(enterprise_identity_id=identity.id),
            "user_policy",
        )
        _require_same(user_policy, user_fields, "user_policy")

        gateway_fields = {
            "enabled": True,
            "permission_scope": frozenset(),
            "allowed_skills": frozenset(),
            "allowed_risk_levels": frozenset({RiskLevel.NORMAL}),
        }
        gateway_policy = obtain(
            policy_store.get_gateway_policy(),
            lambda: policies.upsert_gateway_policy(**gateway_fields),
            "gateway_policy",
        )
        _require_same(gateway_policy, gateway_fields, "gateway_policy")

        conversation_key = {
            "source": "wechat",
            "source_account_id": config.wechat.account_id,
            "conversation_id": config.wechat.conversation_id,
        }
        conversation = obtain(
            messages.get_conversation(**conversation_key),
            lambda: messages.prepare_conversation(
                ConversationCreate(**conversation_key, conversation_type="private")
            )[0],
            "conversation",
        )
        _require_same(conversation, {"conversation_type": "private"}, "conversation")
        binding = obtain(
            profiles.get_conversation_agent_profile_binding(conversation.id),
            lambda: profiles.bind_conversation_agent_profile(
                conversation_record_id=conversation.id, agent_profile_id=profile.id
            )[0],
            "profile_binding",
        )
        _require_same(binding, {"agent_profile_id": profile.id}, "profile_binding")

        # Exercise the same authoritative resolution paths as message admission.
        route = RouteResolver(session).resolve(
            **conversation_key,
            conversation_type="private",
            enterprise_identity_id=identity.id,
        )
        decision = evaluate_access(
            policies.resolve_source_identity_facts(
                source="wechat",
                source_account_id=config.wechat.account_id,
                sender_id=config.wechat.sender_id,
            ),
            policies.resolve_conversation_facts(conversation_type="private"),
            RequestFacts(frozenset(), frozenset(), RiskLevel.NORMAL),
            policies.resolve_gateway_policy_facts(),
        )
        if not decision.allowed or route.agent_profile_id != profile.id:
            raise InitialIdentityError("initial_identity_verification_failed", "resolution")

    return {"status": "ready", "created_stages": created, "check_only": check_only}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="approved initial-identity JSON file"
    )
    parser.add_argument(
        "--check", action="store_true", help="verify existing configuration read-only"
    )
    arguments = parser.parse_args(argv)
    stage = "input"
    engine = None
    try:
        # Bound operator input; neither validation exceptions nor secrets are echoed.
        if arguments.input.stat().st_size > 65536:
            raise ValueError("input too large")
        config = InitialIdentityConfig.model_validate_json(
            arguments.input.read_text(encoding="utf-8")
        )
        stage = "configuration"
        settings = load_settings(os.getenv("CF_GATEWAY_CONFIG", "config/config.yaml"))
        if not settings.runtime.v2_routing_enabled:
            raise InitialIdentityError("v2_routing_required", "runtime.v2_routing_enabled")
        if settings.hermes.model != config.profile.model:
            raise InitialIdentityError("hermes_model_conflict", "profile.model")
        if not arguments.check:
            stage = "wechat_auth"
            authentication = inspect_authentication(settings)
            if not authentication["authenticated"]:
                raise InitialIdentityError("wechat_" + str(authentication["status"]), "wechat_auth")
            if authentication["account_id"] != config.wechat.account_id:
                raise InitialIdentityError("wechat_account_changed", "wechat_auth")
        stage = "database"
        engine = create_database_engine(settings.database.url)
        result = initialize_identity(engine, config, check_only=arguments.check)
    except InitialIdentityError as error:
        print(json.dumps({"error_code": error.code, "stage": error.stage}), file=sys.stderr)
        return 1
    except Exception:
        print(
            json.dumps({"error_code": "initial_identity_failed", "stage": stage}), file=sys.stderr
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
