"""Explicit non-WeChat source uses real admission/claim/capture, model substitute."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_controlled_context_survives_separate_process_and_never_delivers(rig, tmp_path):
    import os
    import subprocess
    import sys

    settings, request = controlled(rig)
    with rig.factory() as session:
        first = submit(session, settings, **request)
    client = Client()
    assert execute(rig.factory, settings, client, first.dispatch_record_id)["status"] == "success"
    payload = dict(
        request=request,
        database_url=settings.database.url,
        storage_root=settings.artifact.storage_root,
        first_message=first.message_id,
        first_dispatch=first.dispatch_record_id,
        first_thread=first.ai_thread_id,
        first_hermes_thread=client.calls[0]["hermes_thread_id"],
    )
    input_file = tmp_path / "restart-input.json"
    input_file.write_text(json.dumps(payload), encoding="utf-8")
    child = r"""
import json, sys
from sqlalchemy import select
from cf_agent_gateway.config import Settings, DatabaseSettings, ArtifactSettings
from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
from cf_agent_gateway.database import create_database_engine, create_database_session_factory
from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings
from cf_agent_gateway.controlled_test import submit, execute, SOURCE
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.workspace.models import AIThread
from test_controlled_product_task import Client

x = json.load(open(sys.argv[1], encoding="utf-8"))
r = x["request"]
s = Settings(
    database=DatabaseSettings(url=x["database_url"]),
    artifact=ArtifactSettings(storage_root=x["storage_root"]),
    artifact_return=ArtifactReturnSettings(
        enabled=True,
        host_contract_confirmed=True,
        public_base_url="https://gateway.invalid",
        profile_reference="synthetic-profile",
        signing_key_env="TEST_ARTIFACT_RETURN_HTTP_KEY",
        controlled_test_bindings=(
            tuple(r[k] for k in ("account", "chat", "sender", "identity", "profile_id")),
        ),
    ),
    host_binding=HostBindingSettings(
        enabled=True,
        dedicated_endpoint_confirmed=True,
        host_id="controlled-fixture",
        profile_reference="synthetic-profile",
        runtime_model="synthetic-model",
        runtime_provider="synthetic",
        runtime_fallback_disabled_confirmed=True,
    ),
)
e = create_database_engine(s.database.url)
factory = create_database_session_factory(e)
with factory() as db:
    old = db.get(AIThread, x["first_thread"])
    assert old.hermes_thread_id == x["first_hermes_thread"]
    second = submit(
        db,
        s,
        **{
            **r,
            "run_id": "product-restarted-02",
            "content": "Now package the materials from our earlier controlled operation.",
        },
    )
    assert (
        second.ai_thread_id == x["first_thread"]
        and second.message_id != x["first_message"]
        and second.dispatch_record_id != x["first_dispatch"]
    )
client = Client()
result = execute(factory, s, client, second.dispatch_record_id)
assert result["status"] == "success"
assert client.calls[0]["hermes_thread_id"] == x["first_hermes_thread"]
with factory() as db:
    assert db.get(Message, second.message_id).source == SOURCE
    assert not list(db.scalars(select(DeliveryOutboxRecord)))
e.dispose()
print(
    json.dumps(
        {
            "success": True,
            "separate_process": True,
            "same_controlled_thread": True,
            "same_hermes_session": True,
            "delivery_rows": 0,
            "model_substitute": True,
        }
    )
)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "src")]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", child, str(input_file)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["same_hermes_session"] is True
