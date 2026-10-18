"""Explicit non-WeChat source uses real admission/claim/capture, model substitute."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from test_artifact_return_http import rig as source_rig

from cf_agent_gateway.controlled_test import SOURCE, execute, submit, verify_tool_inventory
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.hermes.models import HermesChatResult
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import AIThread

rig = source_rig


def controlled(rig):
    with rig.factory() as session:
        old = session.get(HermesDispatchRecord, rig.dispatch_id)
        message = session.get(Message, old.message_id)
        thread = session.get(AIThread, old.ai_thread_id)
        request = dict(
            run_id="product-validation-01",
            account=message.source_account_id,
            chat=message.conversation_id,
            sender=message.sender_id,
            identity=old.enterprise_identity_id,
            profile_id=thread.agent_profile_id,
            content="Controlled non-WeChat task: inspect approved product materials.",
        )
    bindings = (
        tuple(request[key] for key in ("account", "chat", "sender", "identity", "profile_id")),
    )
    settings = replace(
        rig.settings,
        artifact_return=replace(rig.settings.artifact_return, controlled_test_bindings=bindings),
        host_binding=HostBindingSettings(
            enabled=True,
            dedicated_endpoint_confirmed=True,
            host_id="controlled-fixture",
            profile_reference=rig.settings.artifact_return.profile_reference,
            profile_revision=rig.settings.artifact_return.profile_revision,
            runtime_model="synthetic-model",
            runtime_provider="synthetic",
            runtime_fallback_disabled_confirmed=True,
        ),
    )
    rig.app.state.settings = settings
    return settings, request


class Client:
    def __init__(self):
        self.calls = []

    def _request(self, *_args, **_kwargs):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "cf_artifact_return",
                        "enabled": True,
                        "tools": ["cf_return_current_chat"],
                    },
                    {
                        "name": "cf_filebridge_products",
                        "enabled": True,
                        "tools": [
                            "filebrowser_product_materials",
                            "filebrowser_ingest_product_zip",
                        ],
                    },
                ]
            },
        )

    def chat(self, content, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["runtime_model"] == "synthetic-model"
        assert kwargs["runtime_provider"] == "synthetic"
        assert kwargs["runtime_model_options"] == {}
        token = kwargs["artifact_return_context"]["authorization"].split(" ")[1].split(".")[0]
        claims = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        assert claims["account_id"] == "wxid-gateway"
        assert claims["conversation_id"] == "wxid-alice"
        return HermesChatResult(
            assistant_content="Controlled result; no WeChat receipt.",
            hermes_thread_id=kwargs["hermes_thread_id"],
        )


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_controlled_task_normal_admission_exact_claim_capture_restart(rig):
    settings, request = controlled(rig)
    with rig.factory() as session:
        first = submit(session, settings, **request)
        again = submit(session, settings, **request)
        assert first.message_id == again.message_id
        assert first.dispatch_record_id == again.dispatch_record_id
        assert first.ai_thread_id != session.get(HermesDispatchRecord, rig.dispatch_id).ai_thread_id
        message = session.get(Message, first.message_id)
        assert message.source == SOURCE and message.raw_type is None and not message.attachments
        assert message.raw_payload.payload["delivery"] == "capture-only"
        assert HermesDispatchRecordStore(session).claim_next() is None
    client = Client()
    result = execute(rig.factory, settings, client, first.dispatch_record_id)
    assert result["status"] == "success", result
    assert result["capture_error"] is None and len(client.calls) == 1
    with rig.factory() as session:
        assert session.scalar(
            select(ResponseRecord).where(ResponseRecord.message_id == first.message_id)
        )
        assert not list(session.scalars(select(DeliveryOutboxRecord)))
        old = session.get(HermesDispatchRecord, rig.dispatch_id)
        assert old.status.value == "running" and old.attempt_count == 1
    assert (
        execute(rig.factory, settings, client, first.dispatch_record_id)["status"]
        == "already_complete"
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize("rig", [True], indirect=True)
@pytest.mark.parametrize("field", ["account", "chat", "sender", "identity", "profile_id"])
def test_controlled_binding_mismatch_denied_before_new_message(rig, field):
    settings, request = controlled(rig)
    with rig.factory() as session:
        before = len(list(session.scalars(select(Message))))
        with pytest.raises(ValueError, match="binding_denied"):
            submit(session, settings, **{**request, field: "other"})
        assert len(list(session.scalars(select(Message)))) == before


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_controlled_duplicate_content_cannot_change_and_source_revoke_blocks(rig):
    settings, request = controlled(rig)
    with rig.factory() as session:
        item = submit(session, settings, **request)
        with pytest.raises(ValueError, match="duplicate_changed"):
            submit(session, settings, **{**request, "content": "changed"})
        IdentityService(session).disable_mapping(
            platform="wechat", account_id=request["account"], sender_id=request["sender"]
        )
    with pytest.raises(ValueError, match="identity_denied"):
        execute(rig.factory, settings, Client(), item.dispatch_record_id)
    with rig.factory() as session:
        assert session.get(HermesDispatchRecord, item.dispatch_record_id).status.value == "queued"


def test_tool_inventory_rejects_broad_default_profile():
    client = Client()
    original = client._request

    def broad(*args, **kwargs):
        payload = original(*args, **kwargs).json()
        payload["data"].append({"name": "skills", "enabled": True, "tools": ["skill_manage"]})
        return httpx.Response(200, json=payload)

    client._request = broad
    with pytest.raises(ValueError, match="not_restricted"):
        verify_tool_inventory(client)


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_runtime_drift_and_expiry_reject_before_claim(rig):
    settings, request = controlled(rig)
    with rig.factory() as session:
        item = submit(session, settings, **request)
    drifted = replace(
        settings,
        host_binding=replace(settings.host_binding, runtime_model="different-model"),
    )
    with pytest.raises(ValueError, match="runtime_changed"):
        execute(rig.factory, drifted, Client(), item.dispatch_record_id)
    with rig.factory() as session:
        message = session.get(Message, item.message_id)
        message.raw_payload.payload = {
            **message.raw_payload.payload,
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        session.commit()
    with pytest.raises(ValueError, match="controlled_expired"):
        execute(rig.factory, settings, Client(), item.dispatch_record_id)
    with rig.factory() as session:
        row = session.get(HermesDispatchRecord, item.dispatch_record_id)
        assert row.status.value == "queued" and row.attempt_count == 0


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_new_controlled_message_continues_own_thread_not_original(rig):
    settings, request = controlled(rig)
    with rig.factory() as session:
        first = submit(session, settings, **request)
    assert execute(rig.factory, settings, Client(), first.dispatch_record_id)["status"] == "success"
    with rig.factory() as session:
        second = submit(session, settings, **{**request, "run_id": "product-validation-02"})
        assert second.ai_thread_id == first.ai_thread_id
        assert second.dispatch_record_id != first.dispatch_record_id
        assert second.message_id != first.message_id
    assert (
        execute(rig.factory, settings, Client(), second.dispatch_record_id)["status"] == "success"
    )
