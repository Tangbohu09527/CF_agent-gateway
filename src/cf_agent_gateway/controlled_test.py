"""Explicit operator-controlled source, never an employee WeChat message.

Reuses normal identity/admission/claim/return persistence. The CLI executes only
its exact new Dispatch and captures a Response without a Delivery job. Feature
bindings are empty by default and an independent tool-restricted Hermes endpoint
is required. This is not a model tool, public HTTP input or authorization platform.
"""

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cf_agent_gateway.agent_profile import AgentProfileStore
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.schemas import ConversationCreate, MessageEvent
from cf_agent_gateway.message.store import MessageStore

SOURCE = "cf-controlled-test"
TOOLS = {
    "filebrowser_product_materials",
    "filebrowser_ingest_product_zip",
    "cf_return_current_chat",
}
TOOLSETS = {"cf_filebridge_products", "cf_artifact_return"}


def execution_runtime(settings):
    """Use the same explicit runtime contract as the managed attachment path."""
    host = settings.host_binding
    if (
        not host.enabled
        or not host.dedicated_endpoint_confirmed
        or not host.runtime_fallback_disabled_confirmed
        or (host.profile_reference, host.profile_revision)
        != (settings.artifact_return.profile_reference, settings.artifact_return.profile_revision)
    ):
        raise ValueError("controlled_runtime_not_verified")
    return {
        "model": host.runtime_model,
        "provider": host.runtime_provider,
        "model_options": dict(host.runtime_model_options),
    }


class RuntimeClient:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self.runtime = execution_runtime(settings)

    def chat(self, content, **kwargs):
        if (kwargs.get("profile_reference"), kwargs.get("profile_revision")) != (
            self.settings.artifact_return.profile_reference,
            self.settings.artifact_return.profile_revision,
        ):
            raise ValueError("controlled_runtime_profile_changed")
        for name, value in self.runtime.items():
            key = "runtime_" + name
            if key in kwargs and kwargs[key] != value:
                raise ValueError("controlled_runtime_changed")
            kwargs[key] = value
        return self.client.chat(content, **kwargs)


def _original(session, account, chat, sender, identity, profile_id):
    from cf_agent_gateway.access import (
        AccessPolicyService,
        RequestFacts,
        RiskLevel,
        evaluate_access,
    )
    from cf_agent_gateway.adapters.wechat import WechatConversationType

    found = IdentityService(session).resolve_identity(
        platform="wechat", account_id=account, sender_id=sender
    )
    conversation = MessageStore(session).get_conversation(
        source="wechat", source_account_id=account, conversation_id=chat
    )
    if (
        not found.is_executable
        or found.enterprise_identity_id != identity
        or conversation is None
        or conversation.conversation_type != "private"
    ):
        raise ValueError("controlled_source_identity_denied")
    profile = AgentProfileStore(session).resolve_private_agent_profile(conversation.id)
    if profile.id != profile_id or profile.status.value != "active":
        raise ValueError("controlled_source_profile_denied")
    policies = AccessPolicyService(session)
    decision = evaluate_access(
        policies.resolve_source_identity_facts(
            source="wechat", source_account_id=account, sender_id=sender
        ),
        policies.resolve_conversation_facts(
            conversation_type=WechatConversationType.PRIVATE, is_mentioned=None
        ),
        RequestFacts(
            requested_scope=frozenset(),
            requested_skill_ids=frozenset(),
            risk_level=RiskLevel.NORMAL,
        ),
        policies.resolve_gateway_policy_facts(),
    )
    if not decision.allowed:
        raise ValueError("controlled_access_revoked")
    return profile


def validate_controlled_message(session, message, allowed):
    if message.source != SOURCE or message.raw_payload is None:
        raise ValueError("controlled_source_required")
    proof = message.raw_payload.payload
    if not isinstance(proof, dict) or set(proof) != {
        "schema",
        "run_id",
        "origin",
        "identity",
        "profile_id",
        "expires_at",
        "content_sha256",
        "delivery",
        "execution_runtime",
    }:
        raise ValueError("controlled_proof_invalid")
    binding = (
        message.source_account_id,
        message.conversation_id,
        message.sender_id,
        proof["identity"],
        proof["profile_id"],
    )
    if (
        binding not in {tuple(row) for row in allowed}
        or proof["schema"] != "cf-controlled-test/v1"
        or proof["origin"] != "operator-cli"
        or proof["delivery"] != "capture-only"
        or message.conversation_type != "private"
        or message.is_self
        or message.attachments
    ):
        raise ValueError("controlled_binding_denied")
    expiry = datetime.fromisoformat(proof["expires_at"])
    if expiry.tzinfo is None or not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(
        hours=12
    ):
        raise ValueError("controlled_expired")
    if (
        proof["content_sha256"] != hashlib.sha256(message.content.encode()).hexdigest()
        or message.source_message_id != proof["run_id"]
        or not isinstance(proof["execution_runtime"], dict)
        or set(proof["execution_runtime"]) != {"model", "provider", "model_options"}
    ):
        raise ValueError("controlled_input_changed")
    return _original(session, *binding)


def submit(session, settings, *, run_id, account, chat, sender, identity, profile_id, content):
    from cf_agent_gateway.ingestion import MessageAdmissionService

    if (
        not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{7,95}", run_id)
        or not isinstance(content, str)
        or not 1 <= len(content) <= 32768
    ):
        raise ValueError("controlled_input_invalid")
    binding = (account, chat, sender, identity, profile_id)
    allowed = settings.artifact_return.controlled_test_bindings
    if binding not in {tuple(row) for row in allowed}:
        raise ValueError("controlled_binding_denied")
    profile = _original(session, *binding)
    runtime = execution_runtime(settings)
    if (profile.external_profile_ref, profile.revision) != (
        settings.artifact_return.profile_reference,
        settings.artifact_return.profile_revision,
    ):
        raise ValueError("controlled_runtime_profile_mismatch")
    store = MessageStore(session)
    existing = (
        session.query(Message)
        .filter_by(source=SOURCE, source_account_id=account, source_message_id=run_id)
        .one_or_none()
    )
    if existing is not None:
        validate_controlled_message(session, existing, allowed)
        if (
            existing.content != content
            or existing.conversation_id != chat
            or existing.sender_id != sender
            or existing.raw_payload.payload["execution_runtime"] != runtime
        ):
            raise ValueError("controlled_duplicate_changed")
        event = MessageEvent(
            event_id=existing.event_id,
            source=SOURCE,
            source_account_id=account,
            source_message_id=run_id,
            conversation_id=chat,
            conversation_type="private",
            sender_id=sender,
            is_self=False,
            message_type="text",
            content=content,
            occurred_at=existing.occurred_at,
            raw_payload=existing.raw_payload.payload,
        )
    else:
        IdentityService(session).create_mapping(
            platform=SOURCE, account_id=account, sender_id=sender, enterprise_identity_id=identity
        )
        conversation, _ = store.prepare_conversation(
            ConversationCreate(
                source=SOURCE,
                source_account_id=account,
                conversation_id=chat,
                conversation_type="private",
            )
        )
        profiles = AgentProfileStore(session)
        current = profiles.get_conversation_agent_profile_binding(conversation.id)
        if current is not None and current.agent_profile_id != profile_id:
            raise ValueError("controlled_existing_profile_changed")
        profiles.bind_conversation_agent_profile(
            conversation_record_id=conversation.id, agent_profile_id=profile_id
        )
        now = datetime.now(UTC)
        event = MessageEvent(
            event_id=SOURCE + ":" + run_id,
            source=SOURCE,
            source_account_id=account,
            source_message_id=run_id,
            conversation_id=chat,
            conversation_type="private",
            sender_id=sender,
            is_self=False,
            message_type="text",
            content=content,
            occurred_at=now,
            raw_payload={
                "schema": "cf-controlled-test/v1",
                "run_id": run_id,
                "origin": "operator-cli",
                "identity": identity,
                "profile_id": profile_id,
                "expires_at": (now + timedelta(hours=12)).isoformat(),
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "delivery": "capture-only",
                "execution_runtime": runtime,
            },
        )
    return MessageAdmissionService(session, v2_routing_enabled=True).process_event(event)


def verify_tool_inventory(client):
    """Read official endpoint; never infer enabled tools from disk configuration."""
    response = client._request(
        "GET", "v1/toolsets", operation="controlled_tool_inventory", json=None
    )
    payload = response.json()
    rows = payload.get("data", [])
    enabled = [row for row in rows if row.get("enabled") is True]
    names = {row.get("name") for row in enabled}
    tools = {name for row in enabled for name in row.get("tools", [])}
    if names != TOOLSETS or not tools >= TOOLS or tools - TOOLS:
        raise ValueError("controlled_tool_inventory_not_restricted")
    return hashlib.sha256(json.dumps(enabled, sort_keys=True).encode()).hexdigest()


def execute(session_factory, settings, client, dispatch_id):
    from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
    from cf_agent_gateway.hermes.service import HermesDispatchService
    from cf_agent_gateway.hermes.worker import DispatchClaim, HermesDispatchWorker
    from cf_agent_gateway.response.store import ResponseStore
    from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore

    class Capture:
        def __init__(self, session):
            self.session = session

        def handle(self, outcome):
            message = self.session.get(Message, outcome.message_id)
            validate_controlled_message(
                self.session, message, settings.artifact_return.controlled_test_bindings
            )
            ResponseStore(self.session).save_captured(outcome)

    verify_tool_inventory(client)
    runtime = execution_runtime(settings)
    with session_factory() as session:
        row = session.get(HermesDispatchRecord, dispatch_id)
        if row is None:
            raise ValueError("controlled_dispatch_missing")
        message = session.get(Message, row.message_id)
        validate_controlled_message(
            session, message, settings.artifact_return.controlled_test_bindings
        )
        if message.raw_payload.payload["execution_runtime"] != runtime:
            raise ValueError("controlled_runtime_changed")
        if row.status.value == "success":
            saved = HermesDispatchResponseStore(session).get_by_dispatch_record_id(row.id)
            if saved is None:
                raise ValueError("controlled_result_missing")
            Capture(session).handle(HermesDispatchResponseStore.to_outcome(saved, row))
            return {
                "status": "already_complete",
                "dispatch_id": row.id,
                "source": SOURCE,
                "delivery": "not_attempted",
            }
        if row.status.value != "queued" or row.attempt_count != 0:
            raise ValueError("controlled_task_already_attempted")
        token = uuid4().hex
        row = HermesDispatchRecordStore(session).claim(
            row.id, claim_token=token, lease_seconds=600, retry_limit=0
        )
        claim = DispatchClaim(
            row.id,
            token,
            row.ai_thread_id,
            row.idempotency_key,
            row.attempt_count,
            row.lease_expires_at,
        )
    worker = HermesDispatchWorker(
        session_factory,
        lambda session: HermesDispatchService(
            session, RuntimeClient(client, settings), artifact_return=settings.artifact_return
        ),
        response_processor_factory=Capture,
        lease_seconds=600,
        retry_limit=0,
        artifact_return=settings.artifact_return,
        artifact_storage_root=settings.artifact.storage_root,
    )
    result = worker.process_claim(claim)
    return {
        "status": result.status.value,
        "dispatch_id": result.record_id,
        "source": SOURCE,
        "delivery": "not_attempted",
        "error_code": result.error_code,
        "capture_error": result.delivery_error_code,
    }


def main():
    from cf_agent_gateway.config import load_settings
    from cf_agent_gateway.database import create_database_engine, create_database_session_factory
    from cf_agent_gateway.hermes import HermesClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    settings = load_settings(args.config)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if set(request) != {"run_id", "account", "chat", "sender", "identity", "profile_id", "content"}:
        raise ValueError("controlled_request_invalid")
    factory = create_database_session_factory(create_database_engine(settings.database.url))
    if args.prepare_only:
        with factory() as session:
            outcome = submit(session, settings, **request)
        print(
            json.dumps(
                {
                    "source": SOURCE,
                    "message_id": outcome.message_id,
                    "dispatch_id": outcome.dispatch_record_id,
                    "thread_id": outcome.ai_thread_id,
                    "model_called": False,
                    "delivery": "not_attempted",
                }
            )
        )
        return 0 if outcome.dispatch_record_id else 1
    with HermesClient(
        settings.hermes.base_url,
        os.environ.get(settings.hermes.api_key_env),
        settings.hermes.model,
        ca_file=settings.hermes.ca_file,
        timeouts=settings.hermes.timeouts,
    ) as client:
        inventory = verify_tool_inventory(client)
        with factory() as session:
            outcome = submit(session, settings, **request)
        if not outcome.dispatch_record_id:
            raise ValueError("controlled_admission_denied")
        result = execute(factory, settings, client, outcome.dispatch_record_id)
        result["tool_inventory_sha256"] = inventory
        print(json.dumps(result))
    return (
        0
        if result["status"] in {"success", "already_complete"} and not result.get("capture_error")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
