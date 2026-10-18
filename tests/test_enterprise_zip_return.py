"""Real Gateway SQL/HTTP/import/return and protocol substitutes; no live services."""

import base64
import hashlib
import io
import json
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select
from test_artifact_return_http import rig as source_rig
from test_artifact_return_integration import WechatReceiver, deliver, protocol_peer

from cf_agent_gateway.artifact import ArtifactRepository
from cf_agent_gateway.artifact.enterprise_exports import EnterpriseExportSettings
from cf_agent_gateway.artifact.models import Artifact
from cf_agent_gateway.artifact.return_handoff import issue_context
from cf_agent_gateway.config import APISettings
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
from cf_agent_gateway.hermes.return_bridge import plugin, transport
from cf_agent_gateway.hermes.return_bridge.enterprise_files import export_metadata
from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError
from cf_agent_gateway.hermes.return_bridge.scope import (
    ReturnScope,
    activate_scope,
    prepare_enterprise_return,
    trusted_enterprise_context,
)
from cf_agent_gateway.hermes.return_bridge.zip_contract import verify_zip
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.response.runtime import ResponsePersistenceProcessor
from cf_agent_gateway.task.model import HermesDispatchRecord

rig = source_rig


def zipped(name="中文 空格 (1)/主图.png", content=b"synthetic image bytes"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        member = zipfile.ZipInfo(name)
        member.filename = name  # Preserve deliberately unsafe raw names in negative fixtures.
        archive.writestr(member, content)
    return stream.getvalue()


def descriptor(content):
    return {
        "source": "products",
        "path": "/approved/exports/测试 (1).zip",
        "name": "测试 (1).zip",
        "mime_type": "application/zip",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "manifest": verify_zip(io.BytesIO(content)),
    }


@contextmanager
def file_service(content, requests):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer synthetic-export-token"
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def enable(rig, monkeypatch, origin):
    monkeypatch.setenv("TEST_ENTERPRISE_TOKEN", "synthetic-export-token")
    with rig.factory() as session:
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        message = session.get(Message, record.message_id)
        settings = replace(
            rig.settings,
            api=APISettings(max_request_body_bytes=128 * 1024 * 1024),
            artifact_return=replace(rig.settings.artifact_return, max_bytes=128 * 1024 * 1024),
            enterprise_exports=EnterpriseExportSettings(
                enabled=True,
                base_url=origin,
                token_env="TEST_ENTERPRISE_TOKEN",
                source="products",
                root="/approved",
                allowed_bindings=(
                    (
                        message.source_account_id,
                        message.conversation_id,
                        record.enterprise_identity_id,
                    ),
                ),
            ),
        )
        context = issue_context(
            session, record, settings.artifact_return, session_id="synthetic-session"
        )
        session.commit()
    rig.app.state.settings = settings
    rig.settings = settings
    rig.context = context
    return {"Authorization": context["authorization"], "X-CF-Return-Intent": "current-chat"}


def endpoint(rig):
    return f"/internal/hermes/returns/{rig.dispatch_id}/artifacts/0/enterprise-import"


def test_stream_import_large_zip_idempotent_current_task(rig, monkeypatch):
    # > the previous 1 MiB return limit; content is a bounded deterministic fixture.
    content = zipped(content=bytes(range(256)) * 8192)
    requests = []
    with file_service(content, requests) as origin:
        headers = enable(rig, monkeypatch, origin)
        export = descriptor(content)
        first = rig.client.post(endpoint(rig), headers=headers, json=export)
        assert first.status_code == 200, first.text
        second = rig.client.post(endpoint(rig), headers=headers, json=export)
        assert second.status_code == 200 and second.json() == first.json()
        assert len(requests) == 1
        receipt = first.json()
        assert receipt["mime_type"] == "application/zip" and receipt["kind"] == "file"
        assert receipt["sha256"] == hashlib.sha256(content).hexdigest()
        with rig.factory() as session:
            artifacts = list(session.scalars(select(Artifact)))
            assert len(artifacts) == 1
            stored = ArtifactRepository(
                session, rig.settings.artifact.storage_root, max_artifact_bytes=128 * 1024 * 1024
            ).read(receipt["artifact_id"])
            assert stored == content


@pytest.mark.parametrize(
    "change",
    [
        {"path": "/unapproved/file.zip"},
        {"path": "/approved/../secret.zip"},
        {"source": "other"},
        {"sha256": "0" * 64},
        {"size_bytes": 1},
        {"manifest": [{"path": "wrong.png", "size_bytes": 1, "sha256": "0" * 64}]},
    ],
)
def test_import_rejects_scope_or_changed_bytes(rig, monkeypatch, change):
    content = zipped()
    with file_service(content, []) as origin:
        headers = enable(rig, monkeypatch, origin)
        export = {**descriptor(content), **change}
        assert rig.client.post(endpoint(rig), headers=headers, json=export).status_code in {
            409,
            422,
        }
        with rig.factory() as session:
            assert list(session.scalars(select(Artifact))) == []


def test_import_rejects_revoked_claim_and_binding(rig, monkeypatch):
    content, requests = zipped(), []
    with file_service(content, requests) as origin:
        headers = enable(rig, monkeypatch, origin)
        rig.app.state.settings = replace(
            rig.settings,
            enterprise_exports=replace(
                rig.settings.enterprise_exports, allowed_bindings=(("wrong", "wrong", "wrong"),)
            ),
        )
        assert (
            rig.client.post(endpoint(rig), headers=headers, json=descriptor(content)).status_code
            == 409
        )
        rig.app.state.settings = rig.settings
        with rig.factory() as session:
            record = session.get(HermesDispatchRecord, rig.dispatch_id)
            record.claim_token = "new-claim"
            session.commit()
        assert (
            rig.client.post(endpoint(rig), headers=headers, json=descriptor(content)).status_code
            == 403
        )
        assert requests == []


def test_scope_descriptor_no_zip_bytes_no_model_paths(rig, monkeypatch, tmp_path):
    enable(rig, monkeypatch, "http://127.0.0.1:1")
    scope = ReturnScope(
        return_url=rig.context["url"],
        authorization=rig.context["authorization"],
        gateway_origin="https://gateway.invalid",
        request_id="synthetic",
        work_root=tmp_path,
        session_id="synthetic-session",
    )
    with activate_scope(scope):
        binding, active = trusted_enterprise_context()
        export = descriptor(zipped())
        first = prepare_enterprise_return(export=export, binding=binding, check_active=active)
        second = prepare_enterprise_return(export=export, binding=binding, check_active=active)
        assert first == second and first["file_ref"].startswith("enterprise-")
        assert set(first) == {"file_ref", "kind", "filename", "size_bytes", "sha256"}
        assert not any(path.name.startswith("enterprise-") for path in tmp_path.iterdir())
        with pytest.raises(ReturnBridgeError):
            prepare_enterprise_return(
                export=export, binding={**binding, "session_id": "other"}, check_active=active
            )
        scope.close()
        with pytest.raises(ReturnBridgeError):
            trusted_enterprise_context()


@pytest.mark.parametrize("name", ["../evil", "/absolute", "C:/evil", "a\\b", "a/../b", "a\x01b"])
def test_unsafe_zip_rejected(name):
    with pytest.raises(ValueError):
        verify_zip(io.BytesIO(zipped(name)))


def test_no_runtime_scope_is_not_authority():
    with pytest.raises(ReturnBridgeError):
        trusted_enterprise_context()
    with pytest.raises(ValueError):
        export_metadata({"url": "http://forbidden.invalid"})


def test_trusted_descriptor_tool_http_seal_and_delivery(rig, monkeypatch, tmp_path):
    """Model/WeChat substitutes; real tool handler, HTTP, SQL, import and delivery."""
    content = zipped()
    requests = []
    with file_service(content, requests) as origin:
        enable(rig, monkeypatch, origin)
        scope = ReturnScope(
            return_url=rig.context["url"],
            authorization=rig.context["authorization"],
            gateway_origin="https://gateway.invalid",
            request_id="synthetic-model-turn",
            work_root=tmp_path,
            session_id="synthetic-session",
        )

        async def asgi_exchange(self, scope, item, method, headers, remaining):
            assert method == "POST" and remaining > 0
            assert not hasattr(item.file, "content")
            response = rig.client.post(endpoint(rig), headers=headers, json=item.file.export)
            return response.status_code, "0", response.json()

        monkeypatch.setattr(transport.UploadTransport, "_exchange", asgi_exchange)
        with activate_scope(scope):
            binding, active = trusted_enterprise_context()
            ref = prepare_enterprise_return(
                export=descriptor(content), binding=binding, check_active=active
            )
            execution = plugin.Execution(scope, "synthetic-session", None)
            token = plugin._execution.set(execution)
            try:
                plugin.pre_llm_call(session_id="synthetic-session", task_id="task", turn_id="turn")
                result = plugin.tool_execution(
                    tool_name=plugin.TOOL_NAME,
                    args={"file_ref": ref["file_ref"], "kind": "file"},
                    next_call=plugin.handle_return,
                    session_id="synthetic-session",
                    task_id="task",
                    turn_id="turn",
                    tool_call_id="call-return",
                )
            finally:
                plugin._execution.reset(token)
            result = json.loads(result)
            assert result["success"] and result["status"] == "pending_delivery"
        with rig.factory() as session:
            record = session.get(HermesDispatchRecord, rig.dispatch_id)
            completed = HermesDispatchResponseStore(session).complete_success(
                rig.dispatch_id,
                claim_token=rig.claim,
                outcome=HermesDispatchOutcome(
                    message_id=record.message_id,
                    workspace_id=record.workspace_id,
                    ai_thread_id=record.ai_thread_id,
                    assistant_content="已打包本次选定商品资料。",
                ),
                artifact_return=rig.settings.artifact_return,
                artifact_storage_root=rig.settings.artifact.storage_root,
            )
            ResponsePersistenceProcessor(session).process(
                HermesDispatchResponseStore.to_outcome(completed, record)
            )
            message = session.get(Message, rig.message_id)
            account, chat = message.source_account_id, message.conversation_id
        receiver = WechatReceiver(account)
        with protocol_peer(receiver) as wechat:
            assert deliver(rig.settings, rig.factory, wechat).status.value == "delivered"
        assert len(receiver.requests) == 2
        assert all(item["chatId"] == chat for item in receiver.requests)
        sent = receiver.requests[1]["file"]
        assert sent["filename"] == "测试 (1).zip"
        assert base64.b64decode(sent["data"], validate=True) == content
        assert len(requests) == 1
        scope.close()


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_original_followup_cannot_import_generated_zip(rig, monkeypatch):
    from test_attachment_followup_returns import registered_followup

    content = zipped()
    monkeypatch.setenv("CF_GATEWAY_HOST_GRANT_KEY", base64.b64encode(b"x" * 32).decode())
    enable(rig, monkeypatch, "http://127.0.0.1:1")
    registered_followup(rig, "read-return")
    with rig.factory() as session:
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        context = issue_context(
            session, record, rig.settings.artifact_return, session_id=rig.session_id
        )
        session.commit()
    headers = {"Authorization": context["authorization"], "X-CF-Return-Intent": "current-chat"}
    assert (
        rig.client.post(endpoint(rig), headers=headers, json=descriptor(content)).status_code == 403
    )


@pytest.mark.parametrize("prefix", ["/other", "/approved/nested", "/approved/../other"])
def test_reader_scope_must_equal_fixed_export_root(prefix):
    with pytest.raises(ValueError):
        EnterpriseExportSettings(
            enabled=True,
            base_url="https://files.invalid",
            source="products",
            root="/approved",
            download_scope_prefix=prefix,
            allowed_bindings=(("account", "chat", "identity"),),
        )


def test_narrow_reader_scope_import_retains_producer_authority(rig, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    content, requests = zipped(), []
    with file_service(content, requests) as origin:
        headers = enable(rig, monkeypatch, origin)
        settings = replace(
            rig.settings,
            enterprise_exports=replace(
                rig.settings.enterprise_exports, download_scope_prefix="/approved"
            ),
        )
        rig.app.state.settings = settings
        response = rig.client.post(endpoint(rig), headers=headers, json=descriptor(content))
        assert response.status_code == 200, response.text
        assert len(requests) == 1
        assert parse_qs(urlsplit(requests[0]).query) == {
            "source": ["products"],
            "file": ["/exports/测试 (1).zip"],
        }
        outside = {**descriptor(content), "path": "/approved-other/exports/测试 (1).zip"}
        assert rig.client.post(
            endpoint(rig).replace("/0/", "/1/"), headers=headers, json=outside
        ).status_code in {409, 422}
        assert len(requests) == 1


@pytest.mark.parametrize("limit", ["enterprise", "return", "wechat"])
def test_import_rejects_output_limit_before_fetch_and_artifact(rig, monkeypatch, limit):
    content, requests = zipped(content=b"bounded image" * 128), []
    with file_service(content, requests) as origin:
        headers = enable(rig, monkeypatch, origin)
        maximum = len(content) - 1
        settings = rig.settings
        if limit == "enterprise":
            settings = replace(
                settings, enterprise_exports=replace(settings.enterprise_exports, max_bytes=maximum)
            )
        elif limit == "return":
            # Config validation requires the enterprise ceiling to fit the return ceiling.
            settings = replace(
                settings,
                enterprise_exports=replace(settings.enterprise_exports, max_bytes=maximum),
                artifact_return=replace(settings.artifact_return, max_bytes=maximum),
            )
        else:
            settings = replace(settings, wechat=replace(settings.wechat, max_media_bytes=maximum))
        rig.app.state.settings = settings
        with rig.factory() as session:
            record = session.get(HermesDispatchRecord, rig.dispatch_id)
            current = issue_context(
                session, record, settings.artifact_return, session_id="synthetic-session"
            )
            session.commit()
        headers["Authorization"] = current["authorization"]
        for _ in range(2):
            result = rig.client.post(endpoint(rig), headers=headers, json=descriptor(content))
            assert result.status_code == 413
            assert result.json()["detail"] == "enterprise export too large"
        assert requests == []
        with rig.factory() as session:
            assert list(session.scalars(select(Artifact))) == []


def test_enterprise_output_too_large_is_terminal_clear_tool_result(rig, monkeypatch, tmp_path):
    enable(rig, monkeypatch, "http://127.0.0.1:1")
    scope = ReturnScope(
        return_url=rig.context["url"],
        authorization=rig.context["authorization"],
        gateway_origin="https://gateway.invalid",
        request_id="synthetic-limit",
        work_root=tmp_path,
        session_id="synthetic-session",
    )
    calls = []

    async def rejected(self, scope, item, method, headers, remaining):
        calls.append(method)
        return 413, "0", None

    monkeypatch.setattr(transport.UploadTransport, "_exchange", rejected)
    with activate_scope(scope):
        binding, active = trusted_enterprise_context()
        ref = prepare_enterprise_return(
            export=descriptor(zipped()), binding=binding, check_active=active
        )
        execution = plugin.Execution(scope, "synthetic-session", None)
        token = plugin._execution.set(execution)
        try:
            plugin.pre_llm_call(session_id="synthetic-session", task_id="task", turn_id="turn")
            results = [
                json.loads(
                    plugin.tool_execution(
                        tool_name=plugin.TOOL_NAME,
                        args={"file_ref": ref["file_ref"], "kind": "file"},
                        next_call=plugin.handle_return,
                        session_id="synthetic-session",
                        task_id="task",
                        turn_id="turn",
                        tool_call_id=f"limit-{attempt}",
                    )
                )
                for attempt in range(2)
            ]
        finally:
            plugin._execution.reset(token)
        assert results[0] == {"success": False, "error": "return_too_large"}
        assert results[1]["success"] is False
        assert calls == ["POST"]
