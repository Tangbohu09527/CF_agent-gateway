"""Fresh execution continuation with real SQL, claims, grants and delivery.

Hermes is an explicit deterministic test peer and WeChat is a recording sender.
MemoryStaging substitutes only Linux dirfd storage. Native Hermes fork/runtime/
tool execution remains a separate opt-in probe; these tests do not prove it.
"""

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_artifact_return_integration import PDF, PNG
from test_hermes_dispatch_ingestion import normalized_message
from test_inbound_media_queue import HOST, MEDIA
from test_inbound_media_queue import rig as queue_rig

from cf_agent_gateway.adapters.wechat.inbound_media import parse_inbound_media
from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult, MediaFetchError
from cf_agent_gateway.artifact import ArtifactRepository
from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
from cf_agent_gateway.config import ArtifactSettings, DatabaseSettings, Settings, WorkerSettings
from cf_agent_gateway.database import create_database_engine, create_database_session_factory
from cf_agent_gateway.delivery import ChannelDeliveryWorker
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.hermes import HermesChatResult, HermesDispatchService
from cf_agent_gateway.hermes.errors import HermesDispatchError, HermesResponseError
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.inbound.access import read_authorized
from cf_agent_gateway.inbound.host_binding import resolve_binding
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.host_binding_routes import OwnerRequest, closed, events
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.worker import InboundMediaWorker
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import AIThread

rig = queue_rig
HOST_TOKEN = "synthetic-continuation-host-token-" + "h" * 32
HOST_NONCE = "synthetic-continuation-owner-nonce-" + "n" * 32


class MediaPeer:
    def __init__(self, data, kind):
        self.data, self.kind = data, kind

    def fetch(self, source):
        payload = {
            "type": "file" if self.kind == "pdf" else "image",
            "filename": "fixture.pdf" if self.kind == "pdf" else "fixture.png",
            "data": base64.b64encode(self.data).decode(),
        }
        return BoundMediaResult(source.fingerprint, parse_inbound_media(payload))


class Sender:
    """No real WeChat/network. Captures the real delivery worker's exact bytes."""

    account_id = "wxid-gateway"

    def __init__(self):
        self.texts, self.media = [], []

    def send_text(self, conversation_id, content):
        self.texts.append((conversation_id, content))
        return {"messageId": "synthetic-text-" + str(len(self.texts))}

    def send_media(self, conversation_id, media_type, data, mime_type, filename=None):
        self.media.append((conversation_id, media_type, data, mime_type, filename))
        return {"messageId": "synthetic-media-" + str(len(self.media))}


class DeterministicHermes:
    """Session API peer; never substitutes its observations for native evidence."""

    def __init__(self, harness, *, sessions=None):
        self.h = harness
        self.sessions = {} if sessions is None else sessions
        self.preparations, self.calls, self.tips, self.downloads, self.grants = [], [], [], [], []
        self.uploads = []
        self.rotate_tip = False
        self.lose_create_reply = False

    def prepare_inbound_session(self, session_id, **kwargs):
        parent = kwargs["parent_session_id"]
        self.preparations.append((session_id, parent, kwargs["allow_create"]))
        history = [] if parent is None else list(self.sessions[parent])
        history_digest = hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()
        if kwargs["allow_create"]:
            assert session_id not in self.sessions
            self.sessions[session_id] = history
            kwargs["record_history"](history_digest)
            if self.lose_create_reply:
                self.lose_create_reply = False
                raise HermesResponseError(operation="synthetic_lost_create_reply")
        else:
            assert session_id in self.sessions
            assert kwargs["expected_history_digest"] == history_digest

    def verify_inbound_session_tip(self, session_id):
        self.tips.append(session_id)
        if self.rotate_tip:
            raise HermesResponseError(operation="inbound_session_tip")

    async def _download(self, session_id):
        h = self.h
        with h.rig.sessions() as session:
            grant = resolve_binding(
                session,
                h.settings.host_binding,
                session_id=session_id,
                task_id=session_id,
                host_instance_id="synthetic-host-instance",
                host_nonce=HOST_NONCE,
            )
        self.grants.append(grant)
        headers = {
            "authorization": "Bearer " + HOST_TOKEN,
            "x-cf-session-id": session_id,
            "x-cf-task-id": session_id,
            "x-cf-host-instance-id": "synthetic-host-instance",
            "x-cf-host-nonce": HOST_NONCE,
            "x-cf-claim-epoch": grant["claim_epoch"],
        }

        async def connected():
            return False

        request = SimpleNamespace(app=h.client.app, headers=headers, is_disconnected=connected)
        stream = await events(grant["binding_id"], request)
        first = await stream.body_iterator.__anext__()
        assert '"state":"running"' in first
        try:
            descriptor = grant["attachments"][0]
            with h.rig.sessions() as session:
                binding = session.get(InboundHostBinding, grant["binding_id"])
                assert binding.events_connected
                data, mime = read_authorized(
                    session,
                    binding.job_id,
                    descriptor["authorization"],
                    h.rig.staging,
                    require_host=True,
                )
            assert len(data) == descriptor["size"]
            assert hashlib.sha256(data).hexdigest() == descriptor["sha256"]
            self.downloads.append(data)
            body = OwnerRequest.model_validate(
                {
                    "schema": grant["schema"],
                    "session_id": session_id,
                    "task_id": session_id,
                    "host_instance_id": "synthetic-host-instance",
                    "host_nonce": HOST_NONCE,
                    "claim_epoch": grant["claim_epoch"],
                }
            )
            assert closed(grant["binding_id"], request, body).status_code == 200
            return data, mime
        finally:
            await stream.body_iterator.aclose()

    def chat(self, content, **kwargs):
        session_id = kwargs["hermes_thread_id"]
        self.calls.append((content, dict(kwargs)))
        self.sessions.setdefault(session_id, [])
        if '"attachment_id":' in content:
            payload = json.loads(content.split("\n", 1)[1])
            data, mime = asyncio.run(self._download(session_id))
            assert payload["attachment_id"] == self.grants[-1]["attachments"][0]["attachment_id"]
            context = kwargs["artifact_return_context"]
            filename = "fixture.pdf" if mime == "application/pdf" else "fixture.png"
            path = urlsplit(context["url"]).path + "/artifacts/0"
            headers = {
                "Authorization": context["authorization"],
                "X-CF-Return-Intent": "current-chat",
                "X-CF-Filename": quote(filename, safe=""),
                "X-CF-Artifact-Kind": "file" if mime == "application/pdf" else "image",
                "Content-Type": mime,
                "X-CF-Content-SHA256": hashlib.sha256(data).hexdigest(),
            }
            first = self.h.client.put(path, headers=headers, content=data)
            assert first.status_code == 200, first.text
            second = self.h.client.put(path, headers=headers, content=data)
            assert second.status_code == 200 and second.json() == first.json()
            self.uploads.append((path, headers, data, first.json()))
        self.sessions[session_id].append(content)
        return HermesChatResult(assistant_content="synthetic complete", hermes_thread_id=session_id)


class Harness:
    def __init__(self, rig, tmp_path, monkeypatch):
        self.rig = rig
        monkeypatch.setenv("TEST_CONTINUATION_HOST", HOST_TOKEN)
        monkeypatch.setenv("TEST_CONTINUATION_RETURN", "synthetic-return-key-" + "k" * 40)
        self.settings = Settings(
            database=DatabaseSettings(url=str(rig.engine.url)),
            artifact=ArtifactSettings(storage_root=str(tmp_path / "artifacts")),
            inbound_media=MEDIA,
            host_binding=replace(
                HOST, legacy_runtime_confirmed=False, service_token_env="TEST_CONTINUATION_HOST"
            ),
            artifact_return=ArtifactReturnSettings(
                enabled=True,
                host_contract_confirmed=True,
                public_base_url="http://127.0.0.1",
                profile_reference=HOST.profile_reference,
                signing_key_env="TEST_CONTINUATION_RETURN",
            ),
            worker=WorkerSettings(retry_limit=3),
        )
        self.sender = Sender()
        self.host = DeterministicHermes(self)
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()

    def close(self):
        self.client.__exit__(None, None, None)

    def configure(self, **changes):
        # This deterministic local implementation has no fallback runtime.
        changes.setdefault("runtime_fallback_disabled_confirmed", True)
        self.settings = replace(
            self.settings, host_binding=replace(self.settings.host_binding, **changes)
        )
        self.client.app.state.settings = self.settings

    def admit(self, index, *, kind="text", content="", conversation="wxid-alice", intake=True):
        incoming = normalized_message(
            event_id=f"continuation:{index}",
            source_message_id=str(index),
            source_local_id=str(index),
            source_server_id=str(index),
            message_type="text" if kind == "text" else "app" if kind == "pdf" else "image",
            raw_type=1 if kind == "text" else 49 if kind == "pdf" else 3,
            content=(content or "fixture.pdf") if kind == "pdf" else content,
            conversation_id=conversation,
        )
        with self.rig.sessions() as session:
            outcome = MessageAdmissionService(session, inbound_media=MEDIA).process(incoming)
        assert outcome.dispatch_record_id is not None
        if kind != "text" and intake:
            fetcher = MediaPeer(PDF if kind == "pdf" else PNG, kind)
            intake = InboundMediaWorker(self.rig.sessions, fetcher, self.rig.staging)
            assert intake.run_once() == "ready"
        return outcome

    def run(self):
        return build_dispatch_worker(
            self.settings,
            session_factory=self.rig.sessions,
            hermes_client=self.host,
            sender_factory=None,
        ).run_once()

    def deliver_all(self):
        while True:
            with self.rig.sessions() as session:
                result = ChannelDeliveryWorker(
                    session,
                    lambda **_: self.sender,
                    artifact_repository=ArtifactRepository(
                        session, self.settings.artifact.storage_root
                    ),
                ).run_once()
            if result is None:
                break
            assert result.status.value == "delivered"

    def snapshot(self, record_id):
        with self.rig.sessions() as session:
            record = session.get(HermesDispatchRecord, record_id)
            return {
                column.name: getattr(record, column.name) for column in record.__table__.columns
            }

    def tip(self, thread_id):
        with self.rig.sessions() as session:
            return session.get(AIThread, thread_id).hermes_thread_id

    def restart(self):
        """Recreate Gateway engine/client/worker and the synthetic host facade."""
        sessions = self.host.sessions
        self.close()
        self.rig.engine.dispose()
        self.rig.engine = create_database_engine(self.settings.database.url)
        self.rig.sessions = create_database_session_factory(self.rig.engine)
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()
        self.host = DeterministicHermes(self, sessions=sessions)


@pytest.fixture
def continuation(rig, tmp_path, monkeypatch):
    harness = Harness(rig, tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


def establish_text(h, index=1, content="historic ordinary text"):
    outcome = h.admit(index, content=content)
    assert h.run().status.value == "success"
    assert h.tip(outcome.ai_thread_id)
    return outcome


def first_managed_attachment(h):
    initial = establish_text(h)
    instruction = establish_text(h, 2, "Read and return the next original attachment.")
    h.configure(fresh_session_thread_ids=(initial.ai_thread_id,))
    media = h.admit(3, kind="pdf")
    assert h.run().status.value == "success"
    return initial, instruction, media


def test_default_false_preserves_four_attempt_dead_and_never_calls_media_host(continuation):
    h = continuation
    establish_text(h)
    before_calls = len(h.host.calls)
    media = h.admit(2, kind="pdf")
    for expected in ("failed", "failed", "failed", "dead"):
        assert h.run().status.value == expected
    frozen = h.snapshot(media.dispatch_record_id)
    assert frozen["attempt_count"] == 4
    assert frozen["last_error_code"] == "hermes_dispatch_error:inbound_parent_runtime_unverified"
    assert len(h.host.calls) == before_calls and not h.host.preparations
    assert h.run() is None
    with h.rig.sessions() as session:
        assert session.scalar(select(InboundHostBinding)) is None
        assert session.scalar(select(InboundMediaJob)).read_token_hash is None
        assert (
            session.scalar(
                select(HermesDispatchResponse).where(
                    HermesDispatchResponse.dispatch_record_id == media.dispatch_record_id
                )
            )
            is None
        )
    repeated = h.admit(2, kind="pdf", intake=False)
    assert repeated.message_id == media.message_id
    assert repeated.dispatch_record_id == media.dispatch_record_id
    assert h.run() is None
    assert h.snapshot(media.dispatch_record_id) == frozen


def test_text_pdf_text_png_second_pdf_restart_keeps_bytes_and_old_dead(continuation):
    h = continuation
    original = establish_text(h)
    old_tip = h.tip(original.ai_thread_id)
    rejected = h.admit(2, kind="pdf")
    for _ in range(4):
        h.run()
    old_dead = h.snapshot(rejected.dispatch_record_id)
    assert old_dead["status"].value == "dead"
    instruction = establish_text(h, 3, "Read the next file and return its unchanged bytes.")
    old_history = list(h.host.sessions[old_tip])
    h.configure(fresh_session_thread_ids=(original.ai_thread_id,))
    pdf = h.admit(4, kind="pdf")
    assert h.run().status.value == "success"
    fresh_tip = h.tip(original.ai_thread_id)
    assert fresh_tip != old_tip
    assert h.host.preparations == [(fresh_tip, None, True)]
    with h.rig.sessions() as session:
        binding = session.scalar(
            select(InboundHostBinding).where(
                InboundHostBinding.dispatch_id == pdf.dispatch_record_id
            )
        )
        assert binding.execution_policy == "fresh_runtime_v1"
        assert binding.previous_session_id == old_tip
        assert binding.parent_session_id is None
        assert binding.instruction_message_id == instruction.message_id
        assert binding.state == "closed" and binding.closed_at is not None
        assert binding.grant_ciphertext is None
    payload = json.loads(h.host.calls[-1][0].split("\n", 1)[1])
    assert payload["previous_instruction"] == {
        "message_id": instruction.message_id,
        "text": "Read the next file and return its unchanged bytes.",
    }
    assert h.host.sessions[old_tip] == old_history
    assert "historic ordinary text" not in h.host.sessions[fresh_tip][0]
    followup = establish_text(h, 5, "Use the same approved runtime for the next PNG.")
    assert (
        followup.ai_thread_id == original.ai_thread_id and h.tip(original.ai_thread_id) == fresh_tip
    )
    invocation = h.host.calls[-1][1]
    assert invocation["runtime_model"] == HOST.runtime_model
    assert invocation["runtime_provider"] == HOST.runtime_provider
    assert invocation["runtime_model_options"] == HOST.runtime_model_options
    assert fresh_tip in h.host.tips
    png = h.admit(6, kind="png")
    assert h.run().status.value == "success"
    png_tip = h.tip(original.ai_thread_id)
    assert h.host.preparations[-1] == (png_tip, fresh_tip, True)
    assert h.host.downloads == [PDF, PNG]
    h.deliver_all()
    assert [(item[1], item[2]) for item in h.sender.media] == [("file", PDF), ("image", PNG)]
    assert all(item[0] == "wxid-alice" for item in h.sender.media + h.sender.texts)
    # All prior claims and return slots are dead capabilities after completion.
    for grant in h.host.grants:
        descriptor = grant["attachments"][0]
        with h.rig.sessions() as session, pytest.raises(MediaFetchError):
            binding = session.get(InboundHostBinding, grant["binding_id"])
            read_authorized(
                session,
                binding.job_id,
                descriptor["authorization"],
                h.rig.staging,
                require_host=True,
            )
    for path, headers, data, _ in h.host.uploads:
        assert h.client.put(path, headers=headers, content=data).status_code == 403
    before_restart_history = list(h.host.sessions[png_tip])
    h.restart()
    second = h.admit(7, kind="pdf")
    assert h.run().status.value == "success"
    second_tip = h.tip(original.ai_thread_id)
    assert h.host.preparations == [(second_tip, png_tip, True)]
    assert h.host.sessions[second_tip][:-1] == before_restart_history
    assert h.host.downloads == [PDF]
    h.deliver_all()
    assert [item[2] for item in h.sender.media] == [PDF, PNG, PDF]
    # Re-delivery of the original natural input cannot revive the old dead record.
    repeated = h.admit(2, kind="pdf", intake=False)
    assert repeated.message_id == rejected.message_id
    assert repeated.dispatch_record_id == rejected.dispatch_record_id
    with h.rig.sessions() as session:
        assert session.get(HermesDispatchRecord, rejected.dispatch_record_id).status.value == "dead"
        assert session.get(HermesDispatchRecord, second.dispatch_record_id).attempt_count == 1
        assert len(session.scalars(select(InboundHostBinding)).all()) == 3
        for media in (pdf, png, second):
            response = session.scalar(
                select(ResponseRecord).where(ResponseRecord.message_id == media.message_id)
            )
            assert response is not None and response.part_count == 2
        assert (
            session.scalar(
                select(ResponseRecord).where(ResponseRecord.message_id == rejected.message_id)
            )
            is None
        )
        assert all(row.state == "closed" for row in session.scalars(select(InboundHostBinding)))
        assert all(
            row.status.value == "delivered" for row in session.scalars(select(DeliveryOutboxRecord))
        )
    assert h.snapshot(rejected.dispatch_record_id) == old_dead
    assert h.run() is None


@pytest.mark.parametrize("kind", ["text", "pdf"])
@pytest.mark.parametrize("change", ["model", "provider", "options", "profile", "revision", "tip"])
def test_managed_runtime_or_tip_drift_rejects_without_another_reset(continuation, kind, change):
    h = continuation
    initial, _, _ = first_managed_attachment(h)
    old_calls, old_preparations = len(h.host.calls), len(h.host.preparations)
    if change == "tip":
        with h.rig.sessions() as session:
            session.get(AIThread, initial.ai_thread_id).hermes_thread_id = "unknown-synthetic-tip"
            session.commit()
    else:
        changes = {
            "model": {"runtime_model": "other-model"},
            "provider": {"runtime_provider": "other-provider"},
            "options": {"runtime_model_options": {"reasoning": {"enabled": False}}},
            "profile": {"profile_reference": "different-profile"},
            "revision": {"profile_revision": 2},
        }
        h.configure(**changes[change])
    h.admit(4, kind=kind, content="follow-up" if kind == "text" else "")
    result = h.run()
    assert result.status.value != "success"
    assert len(h.host.calls) == old_calls
    assert len(h.host.preparations) == old_preparations
    with h.rig.sessions() as session:
        assert len(session.scalars(select(InboundHostBinding)).all()) == 1


@pytest.mark.parametrize("change", ["identity", "mapping"])
def test_managed_identity_revocation_has_no_host_effect(continuation, change):
    h = continuation
    first_managed_attachment(h)
    h.admit(4, kind="pdf")
    old_calls, old_preparations = len(h.host.calls), len(h.host.preparations)
    with h.rig.sessions() as session:
        if change == "identity":
            session.scalar(select(EnterpriseIdentity)).status = IdentityStatus.DISABLED
        else:
            session.scalar(select(SourceIdentityMapping)).enabled = False
        session.commit()
    assert h.run().status.value != "success"
    assert len(h.host.calls) == old_calls and len(h.host.preparations) == old_preparations


def test_unlisted_other_chat_keeps_legacy_guard_and_receives_no_managed_history(continuation):
    h = continuation
    initial, _, _ = first_managed_attachment(h)
    other = h.admit(4, content="Other conversation history.", conversation="wxid-other")
    assert h.run().status.value == "success"
    other_tip = h.tip(other.ai_thread_id)
    assert other.ai_thread_id != initial.ai_thread_id
    count = len(h.host.calls)
    h.admit(5, kind="pdf", conversation="wxid-other")
    assert h.run().status.value == "failed"
    assert len(h.host.calls) == count
    assert h.host.sessions[other_tip] == ["Other conversation history."]
    assert len(h.host.preparations) == 1


def test_same_claim_lost_create_reply_reuses_frozen_intent_and_instruction(continuation):
    h = continuation
    initial = establish_text(h)
    instruction = establish_text(h, 2, "The frozen instruction.")
    h.configure(fresh_session_thread_ids=(initial.ai_thread_id,))
    h.admit(3, kind="pdf")
    h.host.lose_create_reply = True
    with h.rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="synthetic-same-claim")
        service = HermesDispatchService(
            session,
            h.host,
            inbound_media=MEDIA,
            host_binding=h.settings.host_binding,
            artifact_return=h.settings.artifact_return,
        )
        with pytest.raises(HermesResponseError):
            service.dispatch_record(record)
        session.rollback()
        binding = session.scalar(select(InboundHostBinding))
        frozen = (
            binding.id,
            binding.session_id,
            binding.previous_session_id,
            binding.instruction_message_id,
            binding.grant_ciphertext,
        )
        assert binding.instruction_message_id == instruction.message_id
        result = service.dispatch_record(record)
        session.refresh(binding)
        assert (
            binding.id,
            binding.session_id,
            binding.previous_session_id,
            binding.instruction_message_id,
            binding.grant_ciphertext,
        )[:4] == frozen[:4]
        assert binding.state == "closed"
    assert h.host.preparations == [(frozen[1], None, True), (frozen[1], None, False)]
    assert len(h.host.calls) == 3
    assert result.next_hermes_thread_id == frozen[1]


def test_recent_success_in_other_chat_never_replaces_original_instruction(continuation):
    h = continuation
    original = establish_text(h)
    instruction = establish_text(h, 2, "Only this private chat's next file.")
    h.configure(fresh_session_thread_ids=(original.ai_thread_id,))
    other = h.admit(3, content="Wrong conversation instruction.", conversation="wxid-other")
    assert h.run().status.value == "success"
    assert other.ai_thread_id != original.ai_thread_id
    h.admit(4, kind="pdf")
    assert h.run().status.value == "success"
    payload = json.loads(h.host.calls[-1][0].split("\n", 1)[1])
    assert payload["previous_instruction"] == {
        "message_id": instruction.message_id,
        "text": "Only this private chat's next file.",
    }
    assert "Wrong conversation instruction." not in h.host.calls[-1][0]


def test_frozen_instruction_edit_after_create_intent_is_rejected(continuation):
    h = continuation
    initial = establish_text(h)
    instruction = establish_text(h, 2, "Frozen instruction before intent.")
    h.configure(fresh_session_thread_ids=(initial.ai_thread_id,))
    h.admit(3, kind="pdf")
    h.host.lose_create_reply = True
    with h.rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="synthetic-frozen-claim")
        service = HermesDispatchService(
            session,
            h.host,
            inbound_media=MEDIA,
            host_binding=h.settings.host_binding,
            artifact_return=h.settings.artifact_return,
        )
        with pytest.raises(HermesResponseError):
            service.dispatch_record(record)
        session.rollback()
        binding = session.scalar(select(InboundHostBinding))
        frozen = (
            binding.id,
            binding.session_id,
            binding.instruction_message_id,
            binding.instruction_digest,
        )
        session.get(Message, instruction.message_id).content = "Changed instruction after intent."
        session.commit()
        with pytest.raises(HermesDispatchError, match="inbound_execution_instruction_changed"):
            service.dispatch_record(record)
        session.refresh(binding)
        assert (
            binding.id,
            binding.session_id,
            binding.instruction_message_id,
            binding.instruction_digest,
        ) == frozen
    assert len(h.host.preparations) == 1 and len(h.host.calls) == 2


def test_replacement_claim_cannot_be_adopted_by_stale_fresh_worker(continuation):
    h = continuation
    initial = establish_text(h)
    establish_text(h, 2, "Instruction before stale worker.")
    h.configure(fresh_session_thread_ids=(initial.ai_thread_id,))
    h.admit(3, kind="pdf")

    class PausedService(HermesDispatchService):
        def _resolve_dispatch_profile(self, thread, message, admission):
            profile = super()._resolve_dispatch_profile(thread, message, admission)
            with h.rig.sessions() as replacement:
                record = replacement.scalar(
                    select(HermesDispatchRecord).where(
                        HermesDispatchRecord.message_id == message.id
                    )
                )
                record.claim_token = "synthetic-replacement-claim"
                replacement.commit()
            return profile

    with h.rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="synthetic-stale-claim")
        with pytest.raises(HermesDispatchError, match="inbound_execution_claim_unavailable"):
            PausedService(
                session,
                h.host,
                inbound_media=MEDIA,
                host_binding=h.settings.host_binding,
                artifact_return=h.settings.artifact_return,
            ).dispatch_record(record)
        session.rollback()
        assert session.scalar(select(InboundHostBinding)) is None
        assert session.scalar(select(InboundMediaJob)).read_token_hash is None
    assert len(h.host.calls) == 2 and not h.host.preparations


def test_managed_text_tip_verification_failure_never_advances_or_recreates(continuation):
    h = continuation
    initial, _, _ = first_managed_attachment(h)
    before_tip = h.tip(initial.ai_thread_id)
    before_preparations = len(h.host.preparations)
    h.host.rotate_tip = True
    h.admit(4, content="Ordinary text whose actual session tip changed.")
    result = h.run()
    assert result.status.value == "uncertain"
    assert h.tip(initial.ai_thread_id) == before_tip
    assert h.host.tips[-1] == before_tip
    h.admit(5, kind="pdf")
    assert h.run() is None
    assert len(h.host.preparations) == before_preparations
