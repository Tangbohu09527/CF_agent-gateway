"""Loopback HTTP host -> Gateway -> durable delivery -> WeChat protocol peer.

The execution host and WeChat receiver are explicit protocol substitutes. These
tests do not run a model, load an installed Skill, or prove real WeChat receipt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select
from test_hermes_dispatch_ingestion import normalized_message

from cf_agent_gateway.access import AccessPolicyService, RiskLevel
from cf_agent_gateway.adapters.wechat import WechatConversationType, WechatHttpMediaSender
from cf_agent_gateway.artifact import ArtifactRepository, ArtifactStatus
from cf_agent_gateway.artifact.models import Artifact
from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
from cf_agent_gateway.config import ArtifactSettings, DatabaseSettings, Settings, WorkerSettings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.delivery import ChannelDeliveryWorker
from cf_agent_gateway.delivery.models import DeliveryAttempt, DeliveryOutboxRecord, DeliveryReceipt
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.hermes import HermesClient
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
from cf_agent_gateway.hermes.worker import HermesDispatchWorker
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.response.models import ResponsePartRecord, ResponseRecord
from cf_agent_gateway.response.runtime import ResponsePersistenceProcessor
from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
from cf_agent_gateway.task.model import HermesDispatchRecord

PDF = b"%PDF-1.7\n1 0 obj <</Type /Catalog>> endobj\ntrailer <</Root 1 0 R>>\n%%EOF\n"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII="
)
KEY_ENV = "TEST_ARTIFACT_RETURN_KEY"
HERMES_TOKEN = "synthetic-hermes-token"
WECHAT_TOKEN = "synthetic-wechat-token"
FINAL = "已按本次任务返回。"


@contextmanager
def protocol_peer(callback):
    errors = []

    class Peer(BaseHTTPRequestHandler):
        def handle_request(self):
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                status, payload = callback(self.command, self.path, self.headers, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                grant = self.headers.get("X-CF-Artifact-Return-Authorization")
                if grant is not None:
                    # This peer explicitly implements the trusted host contract.
                    self.send_header(
                        "X-CF-Artifact-Return-Accepted",
                        hashlib.sha256(grant.encode("utf-8")).hexdigest(),
                    )
                if self.headers.get("X-Hermes-Session-Id"):
                    self.send_header("X-Hermes-Session-Id", self.headers["X-Hermes-Session-Id"])
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Expected when the sender's bounded read timeout expires.
                pass
            except Exception as error:
                errors.append(error)
                self.send_error(500)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors, errors


@contextmanager
def gateway_http(settings):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = replace(
        settings,
        artifact_return=ArtifactReturnSettings(
            enabled=True,
            host_contract_confirmed=True,
            public_base_url=origin,
            profile_reference="test-profile",
            signing_key_env=KEY_ENV,
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield settings
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


class ExecutionHost:
    """Authenticated backend uploads bytes; no authorization enters model JSON."""

    def __init__(self, *, kind="file", fail=False):
        self.kind = kind
        self.fail = fail
        self.calls = []
        self.uploads = []
        self.return_url = None
        self.authorization = None
        self.filename = "任务资料.pdf" if kind == "file" else "任务图片.png"
        self.content = PDF if kind == "file" else PNG
        self.mime = "application/pdf" if kind == "file" else "image/png"

    def __call__(self, method, path, headers, body):
        assert method == "POST" and path == "/v1/chat/completions"
        assert headers["Authorization"] == f"Bearer {HERMES_TOKEN}"
        request = json.loads(body)
        self.calls.append(request)
        self.return_url = headers["X-CF-Artifact-Return-URL"]
        self.authorization = headers["X-CF-Artifact-Return-Authorization"]
        assert self.authorization.startswith("Bearer ")
        assert self.authorization not in body.decode()
        assert self.return_url not in body.decode()
        assert WECHAT_TOKEN not in body.decode()
        assert "artifact_ref" not in body.decode()
        if self.kind != "text":
            with httpx.Client(trust_env=False, timeout=5) as client:
                first = client.put(self.return_url + "/artifacts/0", **self.upload_args())
                assert first.status_code in {200, 201}, first.text
                second = client.put(self.return_url + "/artifacts/0", **self.upload_args())
                assert second.status_code in {200, 201}, second.text
                assert first.json() == second.json()
                receipt = first.json()
                assert receipt["status"] == "ready"
                assert receipt["size"] == len(self.content)
                assert receipt["sha256"] == hashlib.sha256(self.content).hexdigest()
                assert receipt["filename"] == self.filename
                assert receipt["kind"] == self.kind
                assert receipt["mime_type"] == self.mime
                self.uploads.append(receipt)
        return 200, {
            "choices": [{"message": {"role": "assistant", "content": FINAL}}],
            "hermes": {"failed": self.fail, "completed": not self.fail},
        }

    def upload_args(self):
        return {
            "content": self.content,
            "headers": {
                "Authorization": self.authorization,
                "X-CF-Return-Intent": "current-chat",
                "X-CF-Filename": quote(self.filename, safe=""),
                "X-CF-Artifact-Kind": self.kind,
                "Content-Type": self.mime,
                "X-CF-Content-SHA256": hashlib.sha256(self.content).hexdigest(),
            },
        }


class WechatReceiver:
    def __init__(self, account, media_outcomes=()):
        self.account = account
        self.media_outcomes = deque(media_outcomes)
        self.requests = []
        self.auth_reads = 0

    def __call__(self, method, path, headers, body):
        assert headers["Authorization"] == f"Bearer {WECHAT_TOKEN}"
        if method == "GET":
            assert path == "/api/status/auth"
            self.auth_reads += 1
            return 200, {"status": "logged_in", "loggedInUser": self.account}
        assert method == "POST" and path == "/api/messages/send"
        payload = json.loads(body)
        self.requests.append(payload)
        outcome = (
            self.media_outcomes.popleft()
            if "text" not in payload and (self.media_outcomes)
            else 200
        )
        if outcome == "timeout":
            time.sleep(0.5)
            outcome = 200
        return outcome, {"success": outcome == 200, "messageId": len(self.requests)}


@contextmanager
def runtime(
    tmp_path,
    monkeypatch,
    *,
    account="wxid-return-bot",
    group=False,
    content="把这个 PDF 发到当前聊天。",
):
    monkeypatch.setenv(KEY_ENV, "synthetic-return-signing-key-" + "x" * 32)
    monkeypatch.setenv("CF_GATEWAY_API_TOKEN", "synthetic-gateway-api-token")
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+pysqlite:///{tmp_path / 'gateway.db'}"),
        artifact=ArtifactSettings(storage_root=str(tmp_path / "artifacts")),
        worker=WorkerSettings(retry_limit=0),
    )
    engine = create_database_engine(settings.database.url)
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    chat = "return-fixture@chatroom" if group else "wxid-return-user"
    with factory() as session:
        identities = IdentityService(session)
        identity = identities.create_identity(employee_id="return-test-employee")
        identities.create_mapping(
            platform="wechat",
            account_id=account,
            sender_id="wxid-alice",
            enterprise_identity_id=identity.id,
        )
        policy = AccessPolicyService(session)
        policy.upsert_user_policy(enterprise_identity_id=identity.id, enabled=True)
        policy.upsert_gateway_policy(enabled=True, allowed_risk_levels={RiskLevel.NORMAL})
        admitted = MessageAdmissionService(session).process(
            normalized_message(
                source_account_id=account,
                conversation_id=chat,
                conversation_type=(
                    WechatConversationType.GROUP if group else WechatConversationType.PRIVATE
                ),
                is_mentioned=True if group else None,
                content=content,
            )
        )
        assert admitted.admission.admitted
        assert admitted.dispatch_record_id is not None
    try:
        with gateway_http(settings) as configured:
            yield configured, factory, admitted, chat
    finally:
        engine.dispose()


def dispatch_once(settings, factory, hermes_origin):
    observations = []
    with HermesClient(hermes_origin, HERMES_TOKEN, "synthetic-model") as client:
        worker = build_dispatch_worker(
            settings,
            session_factory=factory,
            hermes_client=client,
            sender_factory=None,
            operation_observer=lambda succeeded: observations.append(succeeded),
        )
        result = worker.run_once()
        assert result is not None
        assert worker.run_once() is None
        assert len(observations) == 1
        return result


def deliver(settings, factory, origin, *, now=None, timeout=3):
    def sender_factory(*, account_id):
        return WechatHttpMediaSender(
            account_id,
            origin,
            "TEST_WECHAT_TOKEN",
            timeout=timeout,
            environment_reader=lambda _: WECHAT_TOKEN,
        )

    with factory() as session:
        return ChannelDeliveryWorker(
            session,
            sender_factory,
            artifact_repository=ArtifactRepository(session, settings.artifact.storage_root),
            clock=(lambda: now) if now is not None else None,
        ).run_once()


@pytest.mark.parametrize(
    ("kind", "group", "account"),
    [
        ("file", False, "wxid-private-bot"),
        ("image", True, "wxid-group-bot"),
        ("text", False, "wxid-text-bot"),
    ],
)
def test_http_handoff_to_original_chat_and_text_regression(
    tmp_path,
    monkeypatch,
    kind,
    group,
    account,
    caplog,
):
    host = ExecutionHost(kind=kind)
    receiver = WechatReceiver(account)
    task = {
        "file": "把这个 PDF 发到当前聊天。",
        "image": "把这张图片直接发回来。",
        "text": "只返回已保存资料的文字摘要，不发送附件。",
    }[kind]
    with (
        runtime(tmp_path, monkeypatch, account=account, group=group, content=task) as (
            settings,
            factory,
            admitted,
            chat,
        ),
        protocol_peer(host) as hermes_origin,
        protocol_peer(receiver) as wechat_origin,
    ):
        assert dispatch_once(settings, factory, hermes_origin).status.value == "success"
        assert len(host.calls) == 1
        assert deliver(settings, factory, wechat_origin).status.value == "delivered"
        assert deliver(settings, factory, wechat_origin) is None
        assert receiver.requests[0] == {"chatId": chat, "text": FINAL}
        assert all(request["chatId"] == chat for request in receiver.requests)
        with factory() as session:
            record = session.get(HermesDispatchRecord, admitted.dispatch_record_id)
            delivery = session.scalar(select(DeliveryOutboxRecord))
            assert record.attempt_count == 1 and record.status.value == "success"
            assert delivery.account_id == account and delivery.conversation_id == chat
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
            expected_parts = 1 if kind == "text" else 2
            assert delivery.next_part_ordinal == expected_parts
            assert (
                session.scalar(select(func.count()).select_from(DeliveryReceipt)) == expected_parts
            )
            assert (
                session.scalar(select(func.count()).select_from(DeliveryAttempt)) == expected_parts
            )
            assert len(receiver.requests) == receiver.auth_reads == expected_parts
            artifacts = list(session.scalars(select(Artifact)))
            if kind == "text":
                assert not artifacts and not host.uploads
            else:
                assert len(artifacts) == 1
                artifact = artifacts[0]
                assert artifact.status is ArtifactStatus.READY
                assert artifact.response_id == delivery.response_id
                assert artifact.artifact_id == host.uploads[0]["artifact_id"]
                assert (
                    ArtifactRepository(session, settings.artifact.storage_root).read(
                        artifact.artifact_id
                    )
                    == host.content
                )
                sent = receiver.requests[1][kind]
                assert base64.b64decode(sent["data"], validate=True) == host.content
                if kind == "file":
                    assert sent["filename"] == host.filename
                else:
                    assert sent["mimeType"] == "image/png"
                parts = list(
                    session.scalars(select(ResponsePartRecord).order_by(ResponsePartRecord.ordinal))
                )
                assert [part.part_type.value for part in parts] == ["text", "artifact_ref"]
                assert parts[1].artifact_id == artifact.artifact_id
        if kind != "text":
            assert (
                httpx.put(
                    host.return_url + "/artifacts/0",
                    **host.upload_args(),
                    trust_env=False,
                ).status_code
                == 403
            )
        assert host.authorization not in caplog.text
        assert len(host.calls) == 1


def test_uploaded_file_then_hermes_failed_http_200_never_delivers(tmp_path, monkeypatch):
    host = ExecutionHost(fail=True)
    with runtime(tmp_path, monkeypatch) as (settings, factory, _, _), protocol_peer(host) as origin:
        assert dispatch_once(settings, factory, origin).status.value != "success"
        assert len(host.uploads) == len(host.calls) == 1
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Artifact)) == 1
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 0
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 0
            assert session.scalar(select(func.count()).select_from(HermesDispatchResponse)) == 0
        assert (
            httpx.put(
                host.return_url + "/artifacts/0",
                **host.upload_args(),
                trust_env=False,
            ).status_code
            == 403
        )


@pytest.mark.parametrize("outcome", [429, 500, "timeout"])
def test_media_retry_or_uncertain_never_reexecutes_dispatch(tmp_path, monkeypatch, outcome):
    host = ExecutionHost()
    receiver = WechatReceiver("wxid-return-bot", [outcome])
    with (
        runtime(tmp_path, monkeypatch) as (
            settings,
            factory,
            admitted,
            _,
        ),
        protocol_peer(host) as hermes_origin,
        protocol_peer(receiver) as wechat_origin,
    ):
        assert dispatch_once(settings, factory, hermes_origin).status.value == "success"
        now = datetime.now(UTC)
        first = deliver(settings, factory, wechat_origin, now=now, timeout=0.15)
        assert first.next_part_ordinal == 1
        assert first.status.value == ("queued" if outcome == 429 else "uncertain")
        assert deliver(settings, factory, wechat_origin, now=now) is None
        later = deliver(settings, factory, wechat_origin, now=now + timedelta(seconds=120))
        if outcome == 429:
            assert later.status.value == "delivered"
            assert len(receiver.requests) == 3
            assert receiver.requests[1] == receiver.requests[2]
        else:
            assert later is None
            assert len(receiver.requests) == 2
        with factory() as session:
            record = session.get(HermesDispatchRecord, admitted.dispatch_record_id)
            assert record.status.value == "success" and record.attempt_count == 1
            assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 1
            receipts = list(session.scalars(select(DeliveryReceipt)))
            assert len(receipts) == (2 if outcome == 429 else 1)
        assert len(host.calls) == 1


def test_success_committed_before_response_failure_reconciles_with_artifact(
    tmp_path,
    monkeypatch,
):
    host = ExecutionHost()
    receiver = WechatReceiver("wxid-return-bot")
    original = ResponsePersistenceProcessor.handle
    failures = []

    def fail_once(self, outcome):
        if not failures:
            failures.append(outcome.message_id)
            raise RuntimeError("synthetic crash before Response and Delivery persistence")
        return original(self, outcome)

    monkeypatch.setattr(ResponsePersistenceProcessor, "handle", fail_once)
    with (
        runtime(tmp_path, monkeypatch) as (
            settings,
            factory,
            admitted,
            _,
        ),
        protocol_peer(host) as hermes_origin,
        protocol_peer(receiver) as wechat_origin,
    ):
        with HermesClient(hermes_origin, HERMES_TOKEN, "synthetic-model") as client:
            worker = build_dispatch_worker(
                settings, session_factory=factory, hermes_client=client, sender_factory=None
            )
            assert worker.run_once().status.value == "success"
            with factory() as session:
                assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 0
                assert session.scalar(select(func.count()).select_from(HermesDispatchResponse)) == 1
            # A fresh worker simulates restart, replaying persisted success only.
            restarted = build_dispatch_worker(
                settings, session_factory=factory, hermes_client=client, sender_factory=None
            )
            assert restarted.reconcile_once()
            assert restarted.run_once() is None
        assert deliver(settings, factory, wechat_origin).status.value == "delivered"
        assert len(host.calls) == len(host.uploads) == 1
        assert len(receiver.requests) == 2
        assert base64.b64decode(receiver.requests[1]["file"]["data"]) == PDF
        with factory() as session:
            record = session.get(HermesDispatchRecord, admitted.dispatch_record_id)
            assert record.attempt_count == 1 and record.status.value == "success"
            assert session.scalar(select(func.count()).select_from(Artifact)) == 1
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
            assert session.scalar(select(func.count()).select_from(DeliveryReceipt)) == 2


def test_repeated_explicit_429_exhausts_delivery_budget_without_hermes_retry(
    tmp_path,
    monkeypatch,
):
    host = ExecutionHost()
    receiver = WechatReceiver("wxid-return-bot", [429, 429, 429])
    with (
        runtime(tmp_path, monkeypatch) as (
            settings,
            factory,
            admitted,
            _,
        ),
        protocol_peer(host) as hermes_origin,
        protocol_peer(receiver) as wechat_origin,
    ):
        assert dispatch_once(settings, factory, hermes_origin).status.value == "success"
        now = datetime.now(UTC)
        statuses = [
            deliver(
                settings, factory, wechat_origin, now=now + timedelta(seconds=120 * i)
            ).status.value
            for i in range(3)
        ]
        assert statuses == ["queued", "queued", "failed"]
        assert deliver(settings, factory, wechat_origin, now=now + timedelta(days=1)) is None
        assert len(receiver.requests) == 4  # One text, three failed attempts of the same file.
        assert receiver.requests[1] == receiver.requests[2] == receiver.requests[3]
        with factory() as session:
            record = session.get(HermesDispatchRecord, admitted.dispatch_record_id)
            assert record.status.value == "success" and record.attempt_count == 1
            assert session.scalar(select(func.count()).select_from(DeliveryReceipt)) == 1
        assert len(host.calls) == 1


def test_inflight_http_upload_serializes_seal_and_reaches_first_delivery(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    completing = threading.Event()
    closing = threading.Event()
    completed = threading.Event()
    upload_errors = []
    upload_threads = []
    original_create = ArtifactRepository.create
    original_complete = HermesDispatchResponseStore.complete_success
    original_close = HermesDispatchWorker._close_host_binding

    def blocked_create(self, **kwargs):
        # The real PUT already holds the SQLite dispatch write fence at this point.
        entered.set()
        assert release.wait(10), "test did not release the pending upload"
        return original_create(self, **kwargs)

    def observe_complete(self, *args, **kwargs):
        completing.set()
        try:
            return original_complete(self, *args, **kwargs)
        finally:
            completed.set()

    def observe_close(self, claim, reason):
        if reason == "completed":
            closing.set()
        return original_close(self, claim, reason)

    monkeypatch.setattr(ArtifactRepository, "create", blocked_create)
    monkeypatch.setattr(HermesDispatchResponseStore, "complete_success", observe_complete)
    monkeypatch.setattr(HermesDispatchWorker, "_close_host_binding", observe_close)

    class BackgroundUploadHost(ExecutionHost):
        def __call__(self, method, path, headers, body):
            assert method == "POST" and path == "/v1/chat/completions"
            assert headers["Authorization"] == f"Bearer {HERMES_TOKEN}"
            self.calls.append(json.loads(body))
            self.return_url = headers["X-CF-Artifact-Return-URL"]
            self.authorization = headers["X-CF-Artifact-Return-Authorization"]
            assert self.authorization not in body.decode()

            def upload():
                try:
                    response = httpx.put(
                        self.return_url + "/artifacts/0",
                        **self.upload_args(),
                        trust_env=False,
                        timeout=10,
                    )
                    assert response.status_code == 200, response.text
                    self.uploads.append(response.json())
                except Exception as error:
                    upload_errors.append(error)

            thread = threading.Thread(target=upload, daemon=True)
            upload_threads.append(thread)
            thread.start()
            assert entered.wait(5), "PUT did not reach the real fenced repository"
            # Return completion before our PUT has returned. Sealing must wait.
            return 200, {"choices": [{"message": {"role": "assistant", "content": FINAL}}]}

    host = BackgroundUploadHost()
    receiver = WechatReceiver("wxid-return-bot")
    with (
        runtime(tmp_path, monkeypatch) as (
            settings,
            factory,
            _,
            _,
        ),
        protocol_peer(host) as hermes_origin,
        protocol_peer(receiver) as wechat_origin,
    ):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(dispatch_once, settings, factory, hermes_origin)
            try:
                assert entered.wait(5)
                # Current worker closes its host barrier before sealing. That real
                # dispatch write also waits on the upload, so complete_success
                # has not necessarily been reached while the PUT is blocked.
                assert closing.wait(5)
                assert not completed.wait(0.1), "completion crossed a live upload write fence"
                assert not future.done()
                with factory() as session:
                    assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 0
                    assert session.scalar(select(func.count()).select_from(Artifact)) == 0
            finally:
                release.set()
            assert future.result(timeout=10).status.value == "success"
        assert completing.is_set() and completed.is_set()
        for thread in upload_threads:
            thread.join(5)
            assert not thread.is_alive()
        assert not upload_errors, upload_errors
        assert len(host.uploads) == len(host.calls) == 1
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Artifact)) == 1
            artifact = session.scalar(select(Artifact))
            assert artifact.status is ArtifactStatus.READY
            response = session.scalar(select(ResponseRecord))
            assert response.part_count == 2
            assert response.parts[1].artifact_id == artifact.artifact_id
            assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1
        assert deliver(settings, factory, wechat_origin).status.value == "delivered"
        assert base64.b64decode(receiver.requests[1]["file"]["data"]) == PDF
        assert deliver(settings, factory, wechat_origin) is None


@pytest.mark.parametrize(
    "fault",
    ["unready", "corrupt", "foreign-reference", "foreign-response", "unknown-reference"],
)
def test_invalid_artifact_seal_cannot_deliver_or_repeat_model(tmp_path, monkeypatch, fault):
    host = ExecutionHost()
    receiver = WechatReceiver("wxid-return-bot")
    with runtime(tmp_path, monkeypatch) as (settings, factory, admitted, _):

        def faulty_host(method, path, headers, body):
            status, payload = host(method, path, headers, body)
            with factory() as session:
                artifact = session.get(Artifact, host.uploads[0]["artifact_id"])
                if fault == "unready":
                    artifact.status = ArtifactStatus.CREATED
                    session.commit()
                elif fault == "corrupt":
                    # Only this fixture's isolated Gateway storage is modified.
                    content_path = Path(settings.artifact.storage_root) / artifact.storage_key
                    assert content_path.resolve().is_relative_to(tmp_path.resolve())
                    content_path.write_bytes(PDF + b"changed-after-ready")
                else:
                    supplied_artifact_id = artifact.artifact_id
                    supplied_response_id = host.uploads[0]["response_id"]
                    if fault == "foreign-reference":
                        foreign = ArtifactRepository(
                            session, settings.artifact.storage_root
                        ).create(
                            response_id="another-task-response",
                            kind="file",
                            filename="foreign.pdf",
                            mime_type="application/pdf",
                            content=PDF,
                        )
                        supplied_artifact_id = foreign.artifact_id
                    elif fault == "foreign-response":
                        supplied_response_id = "another-task-response"
                    else:
                        supplied_artifact_id = "4a134eef-31a6-4421-b2d1-44abaf0bdac2"
                    payload = {
                        "response_id": supplied_response_id,
                        "parts": [
                            {"type": "text", "text": FINAL},
                            {"type": "artifact_ref", "artifact_id": supplied_artifact_id},
                        ],
                    }
            return status, payload

        with protocol_peer(faulty_host) as hermes_origin, protocol_peer(receiver) as wechat_origin:
            assert dispatch_once(settings, factory, hermes_origin).status.value == "uncertain"
            assert deliver(settings, factory, wechat_origin) is None
            assert not receiver.requests
            assert len(host.calls) == len(host.uploads) == 1
            with factory() as session:
                record = session.get(HermesDispatchRecord, admitted.dispatch_record_id)
                assert record.attempt_count == 1 and record.status.value == "uncertain"
                assert session.scalar(select(func.count()).select_from(ResponseRecord)) == 0
                assert session.scalar(select(func.count()).select_from(HermesDispatchResponse)) == 0
                assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 0


def test_structured_hermes_result_reuses_uploaded_task_artifact(tmp_path, monkeypatch):
    host = ExecutionHost(kind="image")
    receiver = WechatReceiver("wxid-return-bot")

    def structured_host(method, path, headers, body):
        host(method, path, headers, body)
        receipt = host.uploads[0]
        return 200, {
            "response_id": receipt["response_id"],
            "parts": [
                {"type": "text", "text": FINAL},
                {"type": "artifact_ref", "artifact_id": receipt["artifact_id"]},
                {"type": "text", "text": "图片后的说明。"},
            ],
        }

    with (
        runtime(tmp_path, monkeypatch, group=True, content="把这张图片直接发回来。") as (
            settings,
            factory,
            _,
            chat,
        ),
        protocol_peer(structured_host) as hermes_origin,
        protocol_peer(receiver) as wechat_origin,
    ):
        assert dispatch_once(settings, factory, hermes_origin).status.value == "success"
        assert deliver(settings, factory, wechat_origin).status.value == "delivered"
        assert len(host.calls) == len(host.uploads) == 1
        assert receiver.requests == [
            {"chatId": chat, "text": FINAL},
            {
                "chatId": chat,
                "image": {
                    "data": base64.b64encode(PNG).decode(),
                    "mimeType": "image/png",
                },
            },
            {"chatId": chat, "text": "图片后的说明。"},
        ]
        with factory() as session:
            response = session.scalar(select(ResponseRecord))
            assert response.response_id == host.uploads[0]["response_id"]
            assert response.part_count == 3
            assert response.parts[1].artifact_id == host.uploads[0]["artifact_id"]
            assert session.scalar(select(func.count()).select_from(Artifact)) == 1
