"""Portable durable orchestration tests; native filesystem/HTTP in integration suite."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_hermes_dispatch_ingestion import allow_sender, normalized_message

from cf_agent_gateway.adapters.wechat.inbound_media import parse_inbound_media
from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult, MediaFetchError
from cf_agent_gateway.config import InboundMediaSettings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.hermes import HermesChatResult, HermesDispatchService
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.inbound.access import grant_read, read_authorized
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.worker import InboundMediaWorker, _manifest
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.message.models import Attachment, Message
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore

IMAGE = b"\xff\xd8\xffsynthetic-jpeg"
MEDIA = InboundMediaSettings(enabled=True, public_base_url="http://127.0.0.1:8000")


class MemoryStaging:
    """Queue unit-test double, never substitutes for Linux security verification."""

    def __init__(self):
        self.blobs = {}
        self.crash = False

    def publish(self, bound):
        manifest = _manifest(bound)
        self.blobs[manifest["reference"]] = bound.media.data
        if self.crash:
            self.crash = False
            raise RuntimeError("simulated process crash after blob publication")
        return SimpleNamespace(reference=manifest["reference"])

    def read(self, reference, *, size, sha256):
        if reference not in self.blobs:
            raise MediaFetchError("media_staging_missing", retryable=True)
        data = self.blobs[reference]
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise MediaFetchError("media_staging_conflict")
        return data


class Fetcher:
    def __init__(self, *states):
        self.states = list(states) or ["ready"]
        self.calls = 0

    def fetch(self, source):
        self.calls += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, Exception):
            raise state
        payload = {"type": state}
        if state == "ready":
            payload = {
                "type": "image" if source.raw_type == 3 else "file",
                "data": base64.b64encode(IMAGE).decode(),
                "filename": "fixture.jpg",
            }
        return BoundMediaResult(source.fingerprint, parse_inbound_media(payload))


@pytest.fixture
def rig(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'queue.db').as_posix()}")
    initialize_database(engine)
    sessions = create_database_session_factory(engine)
    with sessions() as session:
        allow_sender(session)
    value = SimpleNamespace(
        sessions=sessions,
        engine=engine,
        staging=MemoryStaging(),
        now=[datetime.now(UTC)],
        media=MEDIA,
    )
    yield value
    engine.dispose()


def admit(rig, index=1, *, kind="image", content="", enabled=True):
    message = normalized_message(
        event_id=f"media:{index}",
        source_message_id=str(index),
        source_local_id=str(index),
        source_server_id=str(index),
        message_type=kind,
        raw_type=3 if kind == "image" else 1 if kind == "text" else 49,
        content=content,
    )
    with rig.sessions() as session:
        outcome = MessageAdmissionService(
            session,
            inbound_media=MEDIA if enabled else InboundMediaSettings(),
        ).process(message)
    rig.now[0] = max(rig.now[0], datetime.now(UTC))
    return outcome


def worker(rig, fetcher=None):
    return InboundMediaWorker(
        rig.sessions, fetcher or Fetcher(), rig.staging, clock=lambda: rig.now[0]
    )


def job(rig, index=1):
    with rig.sessions() as session:
        return session.get(InboundMediaJob, index)


class Hermes:
    def __init__(self):
        self.contents = []

    def chat(self, content, **kwargs):
        self.contents.append(content)
        return HermesChatResult(assistant_content="received", hermes_thread_id="synthetic-session")


def test_empty_image_pending_ready_fifo_and_plain_text_regression(rig):
    image = admit(rig)
    text = admit(rig, 2, kind="text", content="original text")
    fetcher = Fetcher("pending", "ready")
    intake = worker(rig, fetcher)
    assert intake.run_once() == "pending"
    with rig.sessions() as session:
        assert HermesDispatchRecordStore(session).claim_next() is None
        assert session.get(HermesDispatchRecord, image.dispatch_record_id).attempt_count == 0
    rig.now[0] += timedelta(seconds=3)
    assert intake.run_once() == "ready"
    assert job(rig).manifest["original_comparison"] == "not_checked"
    with rig.sessions() as session:
        store = HermesDispatchRecordStore(session)
        record = store.claim_next(claim_token="image-claim")
        assert record.message_id == image.message_id
        hermes = Hermes()
        HermesDispatchService(session, hermes, inbound_media=MEDIA).dispatch_record(record)
        assert '"schema": "cf-inbound-read/v1"' in hermes.contents[0]
        assert '"size":' in hermes.contents[0] and "base64" not in hermes.contents[0]
        assert session.get(Message, image.message_id).content == ""
        store.mark_success(record.id, claim_token="image-claim")
        record = store.claim_next(claim_token="text-claim")
        assert record.message_id == text.message_id
        HermesDispatchService(session, hermes, inbound_media=MEDIA).dispatch_record(record)
        assert hermes.contents[-1] == "original text"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("unsupported", "unsupported"),
        (MediaFetchError("media_http_403"), "failed"),
        (MediaFetchError("media_transport_failed", retryable=True), "pending"),
    ],
)
def test_fetch_classification_and_no_hermes_attempts(rig, state, expected):
    result = admit(rig)
    assert worker(rig, Fetcher(state)).run_once() == expected
    assert job(rig).state == expected
    with rig.sessions() as session:
        assert session.get(HermesDispatchRecord, result.dispatch_record_id).attempt_count == 0


def test_timeout_local_failure_notice_then_next_message(rig):
    admit(rig)
    admit(rig, 2, kind="text", content="following")
    rig.now[0] += timedelta(seconds=901)
    fetcher = Fetcher()
    assert worker(rig, fetcher).run_once() == "timed_out"
    assert fetcher.calls == 0
    with rig.sessions() as session:
        store = HermesDispatchRecordStore(session)
        record = store.claim_next(claim_token="failure")
        hermes = Hermes()
        outcome = HermesDispatchService(session, hermes, inbound_media=MEDIA).dispatch_record(
            record
        )
        assert "media_wait_expired" in outcome.assistant_content and not hermes.contents
        store.mark_success(record.id, claim_token="failure")
        assert store.claim_next() is not None


def test_duplicate_admission_fetch_and_ai_offline(rig):
    first = admit(rig)
    second = admit(rig)
    assert first.message_id == second.message_id
    intake = worker(rig)
    assert intake.run_once() == "ready"
    assert intake.run_once() is None
    with rig.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Attachment)) == 1
        assert session.scalar(select(func.count()).select_from(InboundMediaJob)) == 1
        assert session.get(HermesDispatchRecord, first.dispatch_record_id).attempt_count == 0


@pytest.mark.parametrize("after_blob", [False, True])
def test_crash_recovery_and_stale_completion(rig, after_blob):
    admit(rig)
    fetcher = Fetcher()
    intake = worker(rig, fetcher)
    old_claim = intake.claim_once()
    if after_blob:
        rig.staging.crash = True
        with pytest.raises(RuntimeError):
            intake.process_claim(old_claim)
    assert intake.claim_once() is None
    rig.now[0] += timedelta(seconds=181)
    recovered = worker(rig, Fetcher(MediaFetchError("must_not_fetch")) if after_blob else Fetcher())
    assert recovered.run_once() == "ready"
    assert intake.process_claim(old_claim) == "lease_lost"
    with rig.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Attachment)) == 1


@pytest.mark.parametrize("mutation", ["mapping", "identity", "source"])
def test_identity_and_message_changes_fail_before_fetch(rig, mutation):
    admit(rig)
    with rig.sessions() as session:
        if mutation == "mapping":
            session.scalar(select(SourceIdentityMapping)).enabled = False
        elif mutation == "identity":
            session.scalar(select(EnterpriseIdentity)).status = IdentityStatus.DISABLED
        else:
            session.get(Message, 1).content = "changed"
        session.commit()
    fetcher = Fetcher()
    assert worker(rig, fetcher).run_once() == "failed" and fetcher.calls == 0


def test_capability_bound_to_message_identity_lease_and_verified_blob(rig):
    admit(rig)
    assert worker(rig).run_once() == "ready"
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="scoped")
        descriptor = grant_read(
            session, session.get(InboundMediaJob, 1), public_base_url=MEDIA.public_base_url
        )
        session.commit()
        auth = descriptor["authorization"]
        assert read_authorized(session, 1, auth, rig.staging)[0] == IMAGE
        for job_id, token in [(2, auth), (1, None), (1, "Bearer wrong")]:
            with pytest.raises(MediaFetchError):
                read_authorized(session, job_id, token, rig.staging)
        session.scalar(select(SourceIdentityMapping)).enabled = False
        session.commit()
        with pytest.raises(MediaFetchError):
            read_authorized(session, 1, auth, rig.staging)
        session.scalar(select(SourceIdentityMapping)).enabled = True
        session.commit()
        HermesDispatchRecordStore(session).mark_success(record.id, claim_token="scoped")
        with pytest.raises(MediaFetchError):
            read_authorized(session, 1, auth, rig.staging)


def test_bare_filename_pdf_is_probed_but_link_and_reply_are_not(rig):
    admit(rig, 1, kind="app", content="资料.pdf")
    admit(rig, 2, kind="app", content="https://example.test/report.pdf")
    admit(rig, 3, kind="reply", content="资料.pdf")
    with rig.sessions() as session:
        assert session.scalar(select(func.count()).select_from(InboundMediaJob)) == 1


def test_corrupt_publication_manifest_finishes_visibly(rig):
    admit(rig)
    with rig.sessions() as session:
        session.get(InboundMediaJob, 1).manifest = {"broken": True}
        session.commit()
    assert worker(rig).run_once() == "failed"
    assert job(rig).last_error_code == "media_registry_conflict"


def test_database_failure_after_publication_rolls_back_attachment_then_recovers(rig, monkeypatch):
    from sqlalchemy.orm import Session

    admit(rig)
    original_commit = Session.commit
    failed = []

    def crash_commit(session):
        if not failed and any(
            isinstance(item, InboundMediaJob) and item.state == "ready" for item in session.dirty
        ):
            failed.append(True)
            raise RuntimeError("simulated registration commit failure")
        original_commit(session)

    monkeypatch.setattr(Session, "commit", crash_commit)
    with pytest.raises(RuntimeError):
        worker(rig).run_once()
    with rig.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Attachment)) == 0
    assert job(rig).manifest is not None and rig.staging.blobs
    rig.now[0] += timedelta(seconds=181)
    assert worker(rig, Fetcher(MediaFetchError("upstream_gone"))).run_once() == "ready"


def test_terminal_notice_is_durable_and_not_repeated(rig):
    from cf_agent_gateway.config import Settings
    from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
    from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
    from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker

    admit(rig)
    worker(rig, Fetcher("unsupported")).run_once()
    hermes = Hermes()
    dispatcher = build_dispatch_worker(
        Settings(inbound_media=MEDIA),
        session_factory=rig.sessions,
        hermes_client=hermes,
        sender_factory=None,
    )
    assert dispatcher.run_once().status.value == "success"
    assert dispatcher.run_once() is None and not hermes.contents
    with rig.sessions() as session:
        response = session.scalar(select(HermesDispatchResponse))
        assert response.hermes_response_id == "inbound-media-notice:1"
        assert session.scalar(select(func.count()).select_from(DeliveryOutboxRecord)) == 1


def test_upgrade_existing_database_leaves_historical_dead_untouched(rig):
    from alembic import command
    from test_migrations import migration_config

    result = admit(rig, enabled=False)
    with rig.sessions() as session:
        store = HermesDispatchRecordStore(session)
        record = store.claim_next(claim_token="historic")
        store.mark_dead(record.id, claim_token="historic", error_code="empty_message_content")
        before = (record.id, record.message_id, record.idempotency_key, record.attempt_count)
    config = migration_config(str(rig.engine.url))
    command.downgrade(config, "20260823_04")
    command.upgrade(config, "head")
    assert admit(rig).message_id == result.message_id
    with rig.sessions() as session:
        record = session.get(HermesDispatchRecord, result.dispatch_record_id)
        assert (
            record.id,
            record.message_id,
            record.idempotency_key,
            record.attempt_count,
        ) == before
        assert record.status.value == "dead"
        assert session.scalar(select(func.count()).select_from(InboundMediaJob)) == 0
        assert HermesDispatchRecordStore(session).claim_next() is None


def test_shared_group_accepts_two_identities_and_uses_scoped_v2_descriptors(rig):
    from test_routing_runtime import (
        bind_group_type,
        create_conversation,
        create_profile,
        message,
        provision_sender,
    )

    from cf_agent_gateway.adapters.wechat import WechatConversationType, WechatMessageType
    from cf_agent_gateway.workspace.models import ThreadPolicy

    with rig.sessions() as session:
        for sender in ("sender-one", "sender-two"):
            provision_sender(session, sender)
        conversation = create_conversation(session, "123@chatroom", conversation_type="group")
        bind_group_type(session, conversation, create_profile(session), ThreadPolicy.GROUP_SHARED)
        service = MessageAdmissionService(session, v2_routing_enabled=True, inbound_media=MEDIA)
        outcomes = []
        for sequence, sender in enumerate(("sender-one", "sender-two"), 1):
            source = message(
                sequence=sequence,
                sender_id=sender,
                conversation_id=conversation.conversation_id,
                conversation_type=WechatConversationType.GROUP,
            )
            source = source.model_copy(
                update={
                    "source_local_id": str(sequence),
                    "raw_type": 3,
                    "message_type": WechatMessageType.IMAGE,
                    "content": "",
                }
            )
            outcomes.append(service.process(source))
    assert outcomes[0].ai_thread_id == outcomes[1].ai_thread_id
    assert outcomes[0].workspace_id != outcomes[1].workspace_id
    rig.now[0] = datetime.now(UTC)
    intake = worker(rig)
    assert intake.run_once() == intake.run_once() == "ready"
    with rig.sessions() as session:
        store = HermesDispatchRecordStore(session)
        for outcome in outcomes:
            record = store.claim_next(claim_token="group-claim")
            hermes = Hermes()
            HermesDispatchService(session, hermes, inbound_media=MEDIA).dispatch_record(record)
            assert outcome.admission.enterprise_identity_id in hermes.contents[0]
            store.mark_success(record.id, claim_token="group-claim")


def test_http_authorization_and_message_status_do_not_expose_read_secret(rig, monkeypatch):
    from fastapi.testclient import TestClient

    from cf_agent_gateway.config import DatabaseSettings, Settings
    from cf_agent_gateway.gateway.app import create_app
    from cf_agent_gateway.inbound import access

    admit(rig)
    assert worker(rig).run_once() == "ready"
    with rig.sessions() as session:
        HermesDispatchRecordStore(session).claim_next(claim_token="http-read")
        descriptor = grant_read(
            session, session.get(InboundMediaJob, 1), public_base_url=MEDIA.public_base_url
        )
        session.commit()
    monkeypatch.setattr(access, "InboundMediaStaging", lambda _root: rig.staging)
    monkeypatch.setenv("CF_GATEWAY_API_TOKEN", "synthetic-status-only")
    settings = Settings(database=DatabaseSettings(url=str(rig.engine.url)), inbound_media=MEDIA)
    with TestClient(create_app(settings)) as client:
        assert client.get("/messages/1/inbound-media").status_code == 401
        status = client.get(
            "/messages/1/inbound-media",
            headers={
                "Authorization": "Bearer synthetic-status-only",
            },
        )
        assert status.status_code == 200 and status.json()["state"] == "ready"
        assert descriptor["authorization"] not in status.text
        assert "read_token_hash" not in status.text
        assert client.get("/inbound-media/1/content").status_code == 403
        assert (
            client.get(
                "/inbound-media/1/content",
                headers={
                    "Authorization": "Bearer synthetic-status-only",
                },
            ).status_code
            == 403
        )
        headers = {"Authorization": descriptor["authorization"]}
        content = client.get("/inbound-media/1/content", headers=headers)
        assert content.status_code == 200 and content.content == IMAGE
        assert content.headers["cache-control"] == "no-store"
        with rig.sessions() as session:
            session.get(InboundMediaJob, 1).read_expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
            session.commit()
        assert client.get("/inbound-media/1/content", headers=headers).status_code == 403


def test_two_workers_cannot_claim_same_job(rig):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    admit(rig)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return worker(rig).claim_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim), pool.submit(claim)]
        claims = [future.result(timeout=10) for future in futures]
    assert sum(item is not None for item in claims) == 1
    assert job(rig).attempt_count == 1
