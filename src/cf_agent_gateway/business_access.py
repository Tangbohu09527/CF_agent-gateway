"""Discover observed WeChat identities and explicitly approve or disable text access."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from sqlalchemy import Engine, and_, func, select
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AccessPolicyService, RiskLevel, evaluate_access
from cf_agent_gateway.access.models import RequestFacts
from cf_agent_gateway.access.policy_models import UserAccessPolicy
from cf_agent_gateway.access.policy_store import AccessPolicyStore
from cf_agent_gateway.adapters.wechat.diagnose import inspect_authentication
from cf_agent_gateway.admin.store import AdminArchiveStore, AdminQuery
from cf_agent_gateway.admission.store import MessageAdmissionOutcomeStore
from cf_agent_gateway.agent_profile import AgentProfile, AgentProfileStatus
from cf_agent_gateway.agent_profile.errors import AgentProfileStoreError
from cf_agent_gateway.config import load_settings
from cf_agent_gateway.database import check_database_migrations, create_database_engine
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.identity.store import IdentityStore
from cf_agent_gateway.initial_identity import (
    InitialBusinessIdentity,
    InitialIdentityConfig,
    InitialIdentityError,
    InitialProfile,
    InitialSchema,
    initialize_identity,
)
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.routing.errors import RouteResolutionError
from cf_agent_gateway.routing.resolver import RouteResolver
from cf_agent_gateway.task.model import HermesDispatchRecordStore


class BusinessAccessError(RuntimeError):
    def __init__(self, code: str, stage: str) -> None:
        self.code = code
        self.stage = stage
        super().__init__(f"{code}: {stage}")


class BusinessApprovalConfig(InitialSchema):
    """Source identifiers come from an observed message, never this input."""

    version: Literal[1]
    profile: InitialProfile
    identity: InitialBusinessIdentity

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value


def read_approval(path: Path) -> BusinessApprovalConfig:
    """Read a bounded, non-writable-by-others, regular approval input."""
    descriptor = None
    try:
        before = path.lstat()
        _validate_input_status(before)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        _validate_input_status(opened)
        if _file_identity(before) != _file_identity(opened):
            raise ValueError("input changed")
        data = os.read(descriptor, 65537)
        after = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(after) or len(data) != after.st_size:
            raise ValueError("input changed")
        return BusinessApprovalConfig.model_validate_json(data)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    # Reading can update atime. Compare ownership, object identity and content timestamps.
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns if os.name == "posix" else 0,
    )


def _validate_input_status(value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or not 1 <= value.st_size <= 65536:
        raise ValueError("invalid approval file")
    if os.name == "posix" and (
        stat.S_IMODE(value.st_mode) not in {0o400, 0o600, 0o440, 0o640}
        or value.st_uid not in {0, os.geteuid()}
    ):
        raise ValueError("invalid approval permissions")


def _message(session: Session, message_id: int) -> Message:
    message = MessageStore(session).get(message_id)
    if message is None or message.source != "wechat":
        raise BusinessAccessError("observed_message_not_found", "message")
    return message


def _eligibility(message: Message) -> str | None:
    if message.is_self or message.direction != "inbound":
        return "inbound_human_message_required"
    if message.sender_type != "human" or not message.sender_id:
        return "inbound_human_message_required"
    if message.message_type != "text":
        return "text_message_required"
    if message.conversation_type != "private" or message.conversation_id.endswith("@chatroom"):
        return "private_conversation_required"
    return None


def _configuration_facts(
    session: Session,
    *,
    source: str,
    account_id: str,
    sender_id: str | None,
    conversation_id: str,
    conversation_type: str,
    is_mentioned: bool | None = None,
) -> dict[str, object]:
    policies = AccessPolicyService(session)
    identity = (
        policies.resolve_source_identity_facts(
            source=source, source_account_id=account_id, sender_id=sender_id
        )
        if sender_id
        else policies.resolve_identity_facts()
    )
    decision = evaluate_access(
        identity,
        policies.resolve_conversation_facts(
            conversation_type=conversation_type, is_mentioned=is_mentioned
        ),
        RequestFacts(frozenset(), frozenset(), RiskLevel.NORMAL),
        policies.resolve_gateway_policy_facts(),
    )
    identity_record = (
        IdentityStore(session).get_identity(identity.enterprise_identity_id)
        if identity.enterprise_identity_id is not None
        else None
    )
    conversation = MessageStore(session).get_conversation(
        source=source, source_account_id=account_id, conversation_id=conversation_id
    )
    result: dict[str, object] = {
        "permitted": False,
        "reason_code": decision.reason_code.value,
        "enterprise_identity_id": identity.enterprise_identity_id,
        "employee_id": identity_record.employee_id if identity_record is not None else None,
        "conversation_record_id": conversation.id if conversation is not None else None,
        "agent_profile_id": None,
        "agent_profile_key": None,
        "agent_profile_revision": None,
    }
    if identity.enterprise_identity_id is not None:
        try:
            route = RouteResolver(session).resolve(
                source=source,
                source_account_id=account_id,
                conversation_id=conversation_id,
                conversation_type=conversation_type,
                enterprise_identity_id=identity.enterprise_identity_id,
            )
        except (AgentProfileStoreError, RouteResolutionError) as error:
            if decision.allowed:
                result["reason_code"] = error.code
        else:
            result.update(
                permitted=decision.allowed,
                agent_profile_id=route.agent_profile_id,
                agent_profile_key=route.agent_profile_key,
                agent_profile_revision=route.agent_profile_revision,
            )
    return result


def _message_status(session: Session, message: Message) -> dict[str, object]:
    current = _configuration_facts(
        session,
        source=message.source,
        account_id=message.source_account_id,
        sender_id=message.sender_id,
        conversation_id=message.conversation_id,
        conversation_type=message.conversation_type,
        is_mentioned=message.is_mentioned,
    )
    eligibility = _eligibility(message)
    if eligibility is not None and eligibility != "private_conversation_required":
        current.update(permitted=False, reason_code=eligibility)
    outcome = MessageAdmissionOutcomeStore(session).get_by_message_id(message.id)
    dispatch = HermesDispatchRecordStore(session).get_by_message_id(message.id)
    # Whitelist metadata. Do not serialize Message/AdminMessageItem wholesale: it contains text.
    return {
        "scope": "current_normal_text_configuration",
        "end_to_end_ready": False,
        "message_id": message.id,
        "source": message.source,
        "source_account_id": message.source_account_id,
        "sender_id": message.sender_id,
        "conversation_id": message.conversation_id,
        "conversation_type": message.conversation_type,
        "message_type": message.message_type,
        "approval_eligible": eligibility is None,
        "approval_reason_code": eligibility,
        **current,
        "ai_thread_id": dispatch.ai_thread_id if dispatch is not None else None,
        "workspace_id": dispatch.workspace_id if dispatch is not None else None,
        "historical_admission": (
            {
                "state": outcome.state.value,
                "admitted": outcome.should_create_task,
                "reason_code": outcome.authorization_reason_code or outcome.admission_reason,
            }
            if outcome is not None
            else None
        ),
    }


def _overview(session: Session) -> dict[str, object]:
    def count(model: type, *predicates: object) -> int:
        return session.scalar(select(func.count()).select_from(model).where(*predicates)) or 0

    # A mapping and a private route must share the same source/account and pass the
    # authoritative policy and route resolvers. An identity count alone is insufficient.
    pairs = session.execute(
        select(Conversation, SourceIdentityMapping)
        .join(
            SourceIdentityMapping,
            and_(
                SourceIdentityMapping.platform == Conversation.source,
                SourceIdentityMapping.account_id == Conversation.source_account_id,
            ),
        )
        .where(
            Conversation.source == "wechat",
            Conversation.conversation_type == "private",
            SourceIdentityMapping.enabled.is_(True),
        )
    )
    configured_routes: set[int] = set()
    configured_pairs: set[tuple[str, int]] = set()
    for conversation, mapping in pairs:
        facts = _configuration_facts(
            session,
            source=conversation.source,
            account_id=conversation.source_account_id,
            sender_id=mapping.sender_id,
            conversation_id=conversation.conversation_id,
            conversation_type=conversation.conversation_type,
        )
        if facts["permitted"]:
            configured_routes.add(conversation.id)
            configured_pairs.add((str(facts["enterprise_identity_id"]), conversation.id))
    return {
        "scope": "database_configuration_overview",
        "end_to_end_ready": False,
        "business_state": "configured" if configured_pairs else "awaiting_configuration",
        "counts": {
            "identities": count(EnterpriseIdentity),
            "active_identities": count(
                EnterpriseIdentity, EnterpriseIdentity.status == IdentityStatus.ACTIVE
            ),
            "enabled_user_policies": count(UserAccessPolicy, UserAccessPolicy.enabled.is_(True)),
            "active_profiles": count(
                AgentProfile, AgentProfile.status == AgentProfileStatus.ACTIVE
            ),
            "observed_messages": count(Message, Message.source == "wechat"),
            "observed_conversations": count(
                Conversation,
                Conversation.source == "wechat",
                select(Message.id)
                .where(
                    Message.source == Conversation.source,
                    Message.source_account_id == Conversation.source_account_id,
                    Message.conversation_id == Conversation.conversation_id,
                )
                .exists(),
            ),
            "configured_private_routes": len(configured_routes),
            "configured_identity_route_pairs": len(configured_pairs),
        },
    }


def status(engine: Engine, *, message_id: int | None = None) -> dict[str, object]:
    check_database_migrations(engine)
    with Session(engine) as session:
        if message_id is not None:
            return _message_status(session, _message(session, message_id))
        return _overview(session)


def discover(engine: Engine, *, limit: int = 50, offset: int = 0) -> dict[str, object]:
    if not 1 <= limit <= 100 or offset < 0:
        raise BusinessAccessError("invalid_pagination", "input")
    check_database_migrations(engine)
    with Session(engine) as session:
        page = AdminArchiveStore(session).list_messages(
            AdminQuery(source="wechat", limit=limit, offset=offset)
        )
        return {
            **_overview(session),
            "items": [_message_status(session, _message(session, item.id)) for item in page.items],
            "total": page.total,
            "limit": page.limit,
            "offset": page.offset,
        }


def approve(
    engine: Engine,
    *,
    message_id: int,
    config: BusinessApprovalConfig,
    authenticated_account_id: str | None,
) -> dict[str, object]:
    check_database_migrations(engine)
    with Session(engine) as session:
        message = _message(session, message_id)
        reason = _eligibility(message)
        if reason is not None:
            raise BusinessAccessError(reason, "message")
        if authenticated_account_id is None:
            raise BusinessAccessError("wechat_login_required", "wechat_auth")
        if message.source_account_id != authenticated_account_id:
            raise BusinessAccessError("wechat_account_changed", "wechat_auth")
        initial = InitialIdentityConfig(
            **config.model_dump(),
            wechat={
                "account_id": message.source_account_id,
                "sender_id": message.sender_id,
                "conversation_id": message.conversation_id,
            },
        )
        if initial.wechat.model_dump() != {
            "account_id": message.source_account_id,
            "sender_id": message.sender_id,
            "conversation_id": message.conversation_id,
        }:
            raise BusinessAccessError("observed_source_identity_invalid", "message")
    result = initialize_identity(engine, initial)
    return {
        **status(engine, message_id=message_id),
        "created_stages": result["created_stages"],
        "replayed_messages": 0,
    }


def disable(engine: Engine, *, employee_id: str) -> dict[str, object]:
    check_database_migrations(engine)
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
        identity = IdentityStore(session).get_identity_by_employee_id(employee_id)
        if identity is None:
            raise BusinessAccessError("employee_not_found", "identity")
        policy = AccessPolicyStore(session).get_user_policy(identity.id)
        if policy is None:
            raise BusinessAccessError("user_policy_not_found", "user_policy")
        changed = policy.enabled
        AccessPolicyService(session).upsert_user_policy(
            enterprise_identity_id=identity.id,
            enabled=False,
            permission_scope=policy.permission_scope,
            allowed_skills=policy.allowed_skills,
            valid_from=policy.valid_from,
            valid_until=policy.valid_until,
        )
        return {
            "employee_id": identity.employee_id,
            "enterprise_identity_id": identity.id,
            "enabled": False,
            "changed": changed,
            "replayed_messages": 0,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover", help="list observed metadata without message text")
    discovery.add_argument("--limit", type=int, default=50)
    discovery.add_argument("--offset", type=int, default=0)
    inspection = commands.add_parser(
        "status", help="read current configuration, not end-to-end health"
    )
    inspection.add_argument("--message-id", type=int)
    approval = commands.add_parser("approve", help="approve one observed private sender and route")
    approval.add_argument("--message-id", type=int, required=True)
    approval.add_argument("--input", type=Path, required=True)
    disabling = commands.add_parser("disable", help="disable an employee policy without replay")
    disabling.add_argument("--employee-id", required=True)
    arguments = parser.parse_args(argv)
    engine = None
    stage = "input"
    try:
        config = read_approval(arguments.input) if arguments.command == "approve" else None
        stage = "configuration"
        settings = load_settings(os.getenv("CF_GATEWAY_CONFIG", "config/config.yaml"))
        if not settings.runtime.v2_routing_enabled:
            raise BusinessAccessError("v2_routing_required", "runtime.v2_routing_enabled")
        if config is not None and settings.hermes.model != config.profile.model:
            raise BusinessAccessError("hermes_model_conflict", "profile.model")
        stage = "database"
        engine = create_database_engine(settings.database.url)
        if arguments.command == "discover":
            result = discover(engine, limit=arguments.limit, offset=arguments.offset)
        elif arguments.command == "status":
            result = status(engine, message_id=arguments.message_id)
        elif arguments.command == "disable":
            result = disable(engine, employee_id=arguments.employee_id)
        else:
            assert config is not None
            stage = "wechat_auth"
            authentication = inspect_authentication(settings)
            if not authentication["authenticated"]:
                raise BusinessAccessError("wechat_" + str(authentication["status"]), "wechat_auth")
            account_id = authentication["account_id"]
            stage = "approval"
            result = approve(
                engine,
                message_id=arguments.message_id,
                config=config,
                authenticated_account_id=account_id,
            )
    except (BusinessAccessError, InitialIdentityError) as error:
        print(json.dumps({"error_code": error.code, "stage": error.stage}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error_code": "business_access_failed", "stage": stage}), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
