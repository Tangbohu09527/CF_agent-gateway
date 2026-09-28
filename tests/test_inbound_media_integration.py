"""Linux loopback Poll -> durable intake -> authorized GET -> Hermes contract.

Synthetic HTTP peers only. This never proves a deployed WeChat PDF download.
"""

import asyncio
import base64
import hashlib
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn
from inbound_media_download_peer import fetch_verified
from sqlalchemy import func, select
from test_hermes_dispatch_ingestion import allow_sender
from test_inbound_media_staging import bound

from cf_agent_gateway.adapters.wechat.inbound_media_http import (
    InboundMediaHTTPClient,
    MediaFetchError,
)
from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
from cf_agent_gateway.config import DatabaseSettings, InboundMediaSettings, Settings, WechatSettings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.hermes import HermesClient
from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.worker import InboundMediaWorker
from cf_agent_gateway.message.models import Attachment, Message
from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
from cf_agent_gateway.runtime.wechat import run_wechat_poll_once
from cf_agent_gateway.task.model import HermesDispatchRecord

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires Linux private dirfd storage")
TOKEN = "synthetic-inbound-token"
PDF = b"%PDF-1.7\nsynthetic-document\n"
JPEG = b"\xff\xd8\xffsynthetic-image"


@contextmanager
def gateway_server(settings):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = replace(
        settings, inbound_media=replace(settings.inbound_media, public_base_url=origin)
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


def test_real_poll_mixed_messages_pending_and_http_handoff(tmp_path, monkeypatch):
    monkeypatch.delenv("CF_AGENT_WECHAT_TOKEN_FILE", raising=False)
    monkeypatch.setenv("TEST_INBOUND_TOKEN", TOKEN)
    monkeypatch.setenv("CF_GATEWAY_API_TOKEN", "synthetic-status-token")
    host_token = "synthetic-host-service-" + "x" * 32
    monkeypatch.setenv("TEST_INBOUND_HOST_TOKEN", host_token)
    monkeypatch.setenv("TEST_INBOUND_HOST_KEY", base64.b64encode(b"k" * 32).decode())
    bodies = [(1, "first"), (3, ""), (49, "资料.pdf"), (3, ""), (1, "last")]
    rows = [
        {
            "localId": index,
            "serverId": str(index),
            "chatId": "wxid-alice",
            "sender": "wxid-alice",
            "type": kind,
            "content": text,
            "isSelf": False,
            "timestamp": f"2026-09-27T00:00:0{index}+00:00",
        }
        for index, (kind, text) in enumerate(bodies, 1)
    ]
    state = {"pdf_gets": 0, "calls": [], "downloads": [], "descriptors": [], "sessions": {}}

    def host_download(session_id):
        # This is a protocol test host, NOT the real FileBrowser user plugin.
        prefix = state["gateway_origin"] + "/internal/hermes/inbound-bindings"
        body = {
            "schema": "cf-inbound-host-binding/v1",
            "session_id": session_id,
            "task_id": session_id,
            "host_instance_id": "integration-host",
            "host_nonce": "synthetic-nonce-" + "n" * 32,
        }
        auth = {"Authorization": "Bearer " + host_token}
        with httpx.Client(trust_env=False) as peer:
            response = peer.post(prefix + "/resolve", headers=auth, json=body)
            assert response.status_code == 200
            grant = response.json()
            assert peer.post(prefix + "/resolve", headers=auth, json=body).json() == grant
            event_headers = {
                **auth,
                "X-CF-Session-Id": session_id,
                "X-CF-Task-Id": session_id,
                "X-CF-Host-Instance-Id": body["host_instance_id"],
                "X-CF-Host-Nonce": body["host_nonce"],
                "X-CF-Claim-Epoch": grant["claim_epoch"],
            }
            with peer.stream(
                "GET", prefix + f"/{grant['binding_id']}/events", headers=event_headers
            ) as stream:
                assert stream.status_code == 200
                for line in stream.iter_lines():
                    if line.startswith("data: "):
                        assert json.loads(line[6:])["state"] == "running"
                        break
                descriptor = grant["attachments"][0]
                data = asyncio.run(fetch_verified(descriptor))
                assert len(data) == descriptor["size"]
                assert hashlib.sha256(data).hexdigest() == descriptor["sha256"]
                state["downloads"].append(data)
                state["descriptors"].append(descriptor)
                response = peer.post(
                    prefix + f"/{grant['binding_id']}/closed",
                    headers=auth,
                    json={**body, "claim_epoch": grant["claim_epoch"]},
                )
                assert response.status_code == 200

    class Peer(BaseHTTPRequestHandler):
        def reply(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if self.headers.get("X-Hermes-Session-Id"):
                self.send_header("X-Hermes-Session-Id", self.headers["X-Hermes-Session-Id"])
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/sessions/"):
                assert self.headers["Authorization"] == "Bearer synthetic-hermes-key"
                path = self.path.split("?", 1)[0].split("/")
                sid = path[3]
                session = state["sessions"][sid]
                if path[-1] == "messages":
                    return self.reply({"session_id": sid, "data": session["history"]})
                return self.reply({"object": "hermes.session", "session": session["row"]})
            assert self.headers["Authorization"] == "Bearer " + TOKEN
            if self.path == "/api/status/auth":
                return self.reply({"status": "logged_in", "loggedInUser": "wxid-gateway"})
            if self.path == "/api/chats":
                return self.reply([{"id": "wxid-alice", "name": "fixture"}])
            if self.path == "/api/messages/wxid-alice":
                return self.reply(rows)
            assert self.path.startswith("/api/messages/wxid-alice/media/")
            index = int(self.path.rsplit("/", 1)[-1])
            if index == 3:
                state["pdf_gets"] += 1
                if state["pdf_gets"] == 1:
                    return self.reply({"type": "pending"})
            return self.reply(
                {
                    "type": "file" if index == 3 else "image",
                    "filename": "fixture.pdf" if index == 3 else "fixture.jpg",
                    "data": base64.b64encode(PDF if index == 3 else JPEG).decode(),
                }
            )

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.startswith("/api/sessions"):
                assert self.headers["Authorization"] == "Bearer synthetic-hermes-key"
                if self.path.endswith("/model"):
                    sid = self.path.split("/")[3]
                    return self.reply(
                        {
                            "object": "hermes.session.model_lock",
                            "session_id": sid,
                            "runtime": {
                                "model": request["model"],
                                "provider": request["provider"],
                                "requested": {
                                    "model": request["model"],
                                    "provider": request["provider"],
                                },
                                "model_lock": "accepted",
                            },
                        }
                    )
                sid = request["id"]
                parent_id = self.path.split("/")[3] if self.path.endswith("/fork") else None
                parent = state["sessions"].get(parent_id)
                state["sessions"][sid] = {
                    "row": {
                        "id": sid,
                        "parent_session_id": parent_id,
                        "model": "fixture",
                        "has_system_prompt": False,
                    },
                    "history": list(parent["history"]) if parent else [],
                }
                if parent:
                    parent["row"]["end_reason"] = "branched"
                return self.reply(
                    {"object": "hermes.session", "session": state["sessions"][sid]["row"]}, 201
                )
            assert self.path == "/v1/chat/completions"
            sid = self.headers["X-Hermes-Session-Id"]
            session = state["sessions"].setdefault(
                sid,
                {
                    "row": {
                        "id": sid,
                        "parent_session_id": None,
                        "model": "fixture",
                        "has_system_prompt": False,
                    },
                    "history": [],
                },
            )
            content = request["messages"][0]["content"]
            state["calls"].append(content)
            assert "cf-inbound-read/v1" not in json.dumps(request)
            assert "Bearer " not in json.dumps(request)
            if '"attachment_id":' in content:
                assert request["provider"] == "custom"
                host_download(sid)
            session["history"].extend(
                [
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": "received bytes"},
                ]
            )
            return self.reply(
                {
                    "id": "synthetic-completion",
                    "object": "chat.completion",
                    "model": "fixture",
                    "choices": [{"message": {"role": "assistant", "content": "received bytes"}}],
                }
            )

        def log_message(self, *_args):
            pass

    peer = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    peer_thread = threading.Thread(target=peer.serve_forever, daemon=True)
    peer_thread.start()
    origin = f"http://127.0.0.1:{peer.server_port}"
    cache = tmp_path / "staging"
    cache.mkdir(mode=0o700)
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite:///{(tmp_path / 'integration.db').as_posix()}"),
        wechat=WechatSettings(
            enabled=True, base_url=origin, bootstrap_mode="backfill", token_env="TEST_INBOUND_TOKEN"
        ),
        inbound_media=InboundMediaSettings(
            enabled=True, staging_root=str(cache), public_base_url="http://127.0.0.1"
        ),
        host_binding=HostBindingSettings(
            enabled=True,
            dedicated_endpoint_confirmed=True,
            host_id="synthetic-host",
            profile_reference="synthetic-profile",
            runtime_model="fixture",
            runtime_provider="custom",
            legacy_runtime_confirmed=True,
            service_token_env="TEST_INBOUND_HOST_TOKEN",
            encryption_key_env="TEST_INBOUND_HOST_KEY",
        ),
    )
    engine = create_database_engine(settings.database.url)
    initialize_database(engine)
    sessions = create_database_session_factory(engine)
    try:
        with sessions() as session:
            allow_sender(session)
        with gateway_server(settings) as configured:
            state["gateway_origin"] = configured.inbound_media.public_base_url
            assert run_wechat_poll_once(configured).messages_processed == 5
            now = [datetime.now(UTC)]
            intake = InboundMediaWorker(
                sessions,
                InboundMediaHTTPClient(origin, TOKEN),
                InboundMediaStaging(cache),
                clock=lambda: now[0],
            )
            assert intake.run_once() == "ready"
            assert intake.run_once() == "pending"
            assert intake.run_once() == "ready"
            # AI has been offline throughout intake; no request has been attempted.
            with sessions() as session:
                assert session.scalar(select(func.sum(HermesDispatchRecord.attempt_count))) == 0
                assert session.scalar(select(func.count()).select_from(Attachment)) == 2
                assert session.get(Message, 2).content == ""
            now[0] += timedelta(seconds=3)
            assert intake.run_once() == "ready"
            with HermesClient(origin, "synthetic-hermes-key", "fixture") as hermes:
                dispatch = build_dispatch_worker(
                    configured, session_factory=sessions, hermes_client=hermes, sender_factory=None
                )
                for _ in rows:
                    assert dispatch.run_once().status.value == "success"
                assert dispatch.run_once() is None
            assert state["calls"][0] == "first" and state["calls"][-1] == "last"
            assert state["downloads"] == [JPEG, PDF, JPEG]
            descriptor = state["descriptors"][0]
            assert httpx.get(descriptor["url"], trust_env=False).status_code == 403
            # Successful dispatch invalidates even the correct capability.
            assert (
                httpx.get(
                    descriptor["url"],
                    headers={"Authorization": descriptor["authorization"]},
                    trust_env=False,
                ).status_code
                == 403
            )
            with sessions() as session:
                assert session.scalar(select(func.count()).select_from(Attachment)) == 3
                assert {row.state for row in session.scalars(select(InboundMediaJob))} == {"ready"}
    finally:
        engine.dispose()
        peer.shutdown()
        peer.server_close()
        peer_thread.join(5)


def test_native_link_publication_crash_and_lock_busy(tmp_path, monkeypatch):
    import fcntl

    tmp_path.chmod(0o700)
    staging = InboundMediaStaging(tmp_path)
    original_unlink = os.unlink
    crashed = []

    def crash_unlink(name, **kwargs):
        if name.startswith(".intake-") and not crashed:
            crashed.append(name)
            raise RuntimeError("crash after link before unlink")
        return original_unlink(name, **kwargs)

    monkeypatch.setattr(os, "unlink", crash_unlink)
    with pytest.raises(RuntimeError):
        staging.publish(bound())
    assert any(path.stat().st_nlink == 2 for path in tmp_path.iterdir())
    recovered = staging.publish(bound())
    assert (
        staging.read(recovered.reference, size=recovered.size, sha256=recovered.sha256)
        == bound().media.data
    )
    assert all(path.stat().st_nlink == 1 for path in tmp_path.iterdir())
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MediaFetchError, match="media_staging_busy"):
            staging.read(recovered.reference, size=recovered.size, sha256=recovered.sha256)
    finally:
        os.close(fd)
