"""Real DB/worker lifecycle fences; the plugin's closed acknowledgement is a test peer."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_inbound_media_queue as media_tests
from sqlalchemy import select

from cf_agent_gateway.admin.recovery import (
    DispatchRecoveryStateConflictError,
    DispatchRecoveryStore,
)
from cf_agent_gateway.hermes import HermesClient, HermesDispatchOutcome, HermesDispatchWorker
from cf_agent_gateway.hermes.errors import HermesDispatchError
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.store import aware
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchStateConflictError,
    HermesDispatchStatus,
)

rig = media_tests.rig


def outcome(record):
    return HermesDispatchOutcome(
        message_id=record.message_id,
        workspace_id=record.workspace_id,
        ai_thread_id=record.ai_thread_id,
        assistant_content="synthetic verified response",
    )


def make_worker(rig, dispatcher, *, lease_seconds=5):
    return HermesDispatchWorker(
        rig.sessions, lambda session: dispatcher, lease_seconds=lease_seconds, retry_limit=2
    )


def prepare(rig, dispatcher, *, lease_seconds=5, host_lease_seconds=3):
    media_tests.admit(rig)
    assert media_tests.worker(rig).run_once() == "ready"
    worker = make_worker(rig, dispatcher, lease_seconds=lease_seconds)
    claim = worker.claim_once()
    assert claim is not None
    binding_id = add_binding(rig, claim, host_lease_seconds=host_lease_seconds)
    return worker, claim, binding_id


def add_binding(rig, claim, *, host_lease_seconds=3):
    now = datetime.now(UTC)
    binding_id = str(uuid4())
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.dispatch_id == claim.record_id)
        )
        job.read_token_hash = "a" * 64
        job.read_claim_token = claim.claim_token
        job.read_expires_at = now + timedelta(seconds=30)
        session.add(
            InboundHostBinding(
                id=binding_id,
                dispatch_id=claim.record_id,
                job_id=job.id,
                ai_thread_id=claim.ai_thread_id,
                claim_token_hash=hashlib.sha256(claim.claim_token.encode()).hexdigest(),
                claim_epoch=str(uuid4()),
                session_id=str(uuid4()),
                parent_session_id=None,
                profile_reference="synthetic-profile",
                profile_revision=1,
                host_id="synthetic-host",
                state="running",
                created_at=now,
                grant_expires_at=now + timedelta(seconds=30),
                grant_ciphertext=b"synthetic-ciphertext-for-lifecycle-tests-only",
                host_instance_id="test-peer-instance",
                host_nonce_hash="b" * 64,
                task_id="test-peer-task",
                host_lease_until=min(
                    now + timedelta(seconds=host_lease_seconds), aware(claim.lease_expires_at)
                ),
            )
        )
        session.commit()
    return binding_id


def await_revoked(rig, binding_id):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        with rig.sessions() as session:
            binding = session.get(InboundHostBinding, binding_id)
            if binding.state == "revoked":
                assert binding.grant_ciphertext is None
                assert session.get(InboundMediaJob, binding.job_id).read_token_hash is None
                return binding
        sleep(0.01)
    pytest.fail("binding was not revoked within the test deadline")


def acknowledge_closed(rig, binding_id):
    # Only this lifecycle fixture substitutes a host. Authentication and the
    # public /closed endpoint are exercised by the host-binding HTTP suite.
    with rig.sessions() as session:
        binding = session.get(InboundHostBinding, binding_id)
        binding.state = "closed"
        binding.closed_at = datetime.now(UTC)
        session.commit()


@pytest.mark.parametrize("failure", [False, True])
def test_worker_revokes_and_waits_for_closed_before_completing(rig, failure):
    class Dispatcher:
        def dispatch_record(self, record):
            if failure:
                raise HermesDispatchError(reason="synthetic_pre_execution_failure")
            return outcome(record)

    worker, claim, binding_id = prepare(rig, Dispatcher())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.process_claim, claim)
        await_revoked(rig, binding_id)
        assert not future.done()
        with rig.sessions() as session:
            assert (
                session.get(HermesDispatchRecord, claim.record_id).status
                is HermesDispatchStatus.RUNNING
            )
            assert session.scalar(select(HermesDispatchResponse)) is None
        acknowledge_closed(rig, binding_id)
        result = future.result(timeout=3)
    assert result.status is (
        HermesDispatchStatus.FAILED if failure else HermesDispatchStatus.SUCCESS
    )


def test_lost_closed_ack_uses_finite_host_lease(rig):
    class Dispatcher:
        def dispatch_record(self, record):
            return outcome(record)

    worker, claim, binding_id = prepare(rig, Dispatcher(), host_lease_seconds=0.4)
    with rig.sessions() as session:
        expires_at = aware(session.get(InboundHostBinding, binding_id).host_lease_until)
    started = monotonic()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(worker.process_claim, claim).result(timeout=3)
    assert result.status is HermesDispatchStatus.SUCCESS
    assert datetime.now(UTC) >= expires_at
    assert monotonic() - started < 3
    with rig.sessions() as session:
        binding = session.get(InboundHostBinding, binding_id)
        assert binding.closed_at is None
        assert binding.grant_ciphertext is None


@pytest.mark.parametrize("transition", ["success", "failed", "uncertain", "dead", "result"])
def test_direct_completion_cannot_bypass_unclosed_host_lease(rig, transition):
    worker, claim, binding_id = prepare(rig, object())
    with rig.sessions() as session:
        store = HermesDispatchRecordStore(session)
        record = store.get(claim.record_id)
        with pytest.raises(HermesDispatchStateConflictError):
            if transition == "result":
                HermesDispatchResponseStore(session).complete_success(
                    record.id, claim_token=claim.claim_token, outcome=outcome(record)
                )
            else:
                kwargs = {} if transition == "success" else {"error_code": "synthetic_failure"}
                getattr(store, f"mark_{transition}")(
                    record.id, claim_token=claim.claim_token, **kwargs
                )
        assert store.get(record.id).status is HermesDispatchStatus.RUNNING
        assert session.scalar(select(HermesDispatchResponse)) is None
    acknowledge_closed(rig, binding_id)
    with rig.sessions() as session:
        HermesDispatchRecordStore(session).mark_success(
            claim.record_id, claim_token=claim.claim_token
        )


def test_expiry_revokes_and_manual_recovery_cannot_skip_thread_host_barrier(rig):
    worker, claim, binding_id = prepare(rig, object())
    next_message = media_tests.admit(rig, index=2, kind="text", content="keep FIFO")
    with rig.sessions() as session:
        record = session.get(HermesDispatchRecord, claim.record_id)
        record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert worker.claim_once() is None
    await_revoked(rig, binding_id)
    with rig.sessions() as session:
        assert (
            session.get(HermesDispatchRecord, claim.record_id).status
            is HermesDispatchStatus.UNCERTAIN
        )
        with pytest.raises(DispatchRecoveryStateConflictError):
            DispatchRecoveryStore(session).mark_dead(
                claim.record_id,
                operator="synthetic-operator",
                reference="test-only",
                reason="test recovery",
            )
        assert (
            session.get(HermesDispatchRecord, claim.record_id).status
            is HermesDispatchStatus.UNCERTAIN
        )
    assert worker.claim_once() is None
    acknowledge_closed(rig, binding_id)
    with rig.sessions() as session:
        DispatchRecoveryStore(session).mark_dead(
            claim.record_id,
            operator="synthetic-operator",
            reference="test-only",
            reason="test recovery",
        )
    next_claim = worker.claim_once()
    assert next_claim is not None
    with rig.sessions() as session:
        assert (
            session.get(HermesDispatchRecord, next_claim.record_id).message_id
            == next_message.message_id
        )


def test_lease_renewal_failure_revokes_media_and_rejects_late_success(rig, monkeypatch):
    entered, release = Event(), Event()

    class Dispatcher:
        def dispatch_record(self, record):
            entered.set()
            assert release.wait(3)
            return outcome(record)

    worker, claim, binding_id = prepare(rig, Dispatcher(), lease_seconds=2)
    renew = HermesDispatchRecordStore.renew_lease
    renew_count = 0

    def lose_renewal(store, *args, **kwargs):
        nonlocal renew_count
        renew_count += 1
        if renew_count > 1:
            raise RuntimeError("synthetic unavailable database")
        return renew(store, *args, **kwargs)

    monkeypatch.setattr(HermesDispatchRecordStore, "renew_lease", lose_renewal)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.process_claim, claim)
        try:
            assert entered.wait(2)
            await_revoked(rig, binding_id)
            acknowledge_closed(rig, binding_id)
        finally:
            release.set()
        assert future.result(timeout=2).status is HermesDispatchStatus.UNCERTAIN
    with rig.sessions() as session:
        assert session.scalar(select(HermesDispatchResponse)) is None


def test_shutdown_revokes_only_bound_claim_and_rejects_late_media_result(rig):
    entered, release, stop = Event(), Event(), Event()

    class Dispatcher:
        def dispatch_record(self, record):
            entered.set()
            assert release.wait(3)
            return outcome(record)

    worker, claim, binding_id = prepare(rig, Dispatcher())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.process_claim, claim, stop_event=stop)
        try:
            assert entered.wait(2)
            stop.set()
            await_revoked(rig, binding_id)
            acknowledge_closed(rig, binding_id)
        finally:
            release.set()
        assert future.result(timeout=3).status is HermesDispatchStatus.UNCERTAIN


def test_shutdown_already_requested_cannot_race_fast_media_completion(rig):
    stop = Event()

    class Dispatcher:
        def dispatch_record(self, record):
            return outcome(record)

    worker, claim, binding_id = prepare(rig, Dispatcher(), host_lease_seconds=0.1)
    stop.set()
    assert worker.process_claim(claim, stop_event=stop).status is HermesDispatchStatus.UNCERTAIN
    with rig.sessions() as session:
        assert session.get(InboundHostBinding, binding_id).grant_ciphertext is None
        assert session.scalar(select(HermesDispatchResponse)) is None


def test_abrupt_dispatch_unwind_still_revokes_host_grant(rig):
    class Dispatcher:
        def dispatch_record(self, record):
            raise KeyboardInterrupt()

    worker, claim, binding_id = prepare(rig, Dispatcher(), host_lease_seconds=0.1)
    with pytest.raises(KeyboardInterrupt):
        worker.process_claim(claim)
    with rig.sessions() as session:
        assert session.get(InboundHostBinding, binding_id).grant_ciphertext is None
        assert (
            session.get(HermesDispatchRecord, claim.record_id).status
            is HermesDispatchStatus.RUNNING
        )
        assert session.scalar(select(HermesDispatchResponse)) is None


def test_stale_worker_revoke_cannot_destroy_replacement_claim_grant(rig):
    worker, old_claim, old_binding = prepare(rig, object())
    acknowledge_closed(rig, old_binding)
    with rig.sessions() as session:
        HermesDispatchRecordStore(session).mark_failed(
            old_claim.record_id, claim_token=old_claim.claim_token, error_code="synthetic_failure"
        )
    new_claim = worker.claim_once()
    assert new_claim is not None
    new_binding = add_binding(rig, new_claim)
    worker._revoke_host_binding(old_claim, "dispatch_lease_lost")
    with rig.sessions() as session:
        binding = session.get(InboundHostBinding, new_binding)
        assert binding.state == "running"
        assert binding.grant_ciphertext is not None
        job = session.get(InboundMediaJob, binding.job_id)
        assert job.read_token_hash is not None
        assert job.read_claim_token == new_claim.claim_token


def test_failed_http_200_revokes_binding_without_success_or_session_end_hook(rig):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": "failed"}}],
                    "hermes": {"failed": True},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Hermes-Session-Id", "synthetic-hermes-session")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with HermesClient(
            f"http://127.0.0.1:{server.server_port}", "synthetic-key", "test-model"
        ) as client:

            class Dispatcher:
                def dispatch_record(self, record):
                    client.chat("attachment_id only", hermes_thread_id="synthetic-hermes-session")
                    pytest.fail("failed HTTP 200 became a business success")

            worker, claim, binding_id = prepare(rig, Dispatcher())
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(worker.process_claim, claim)
                await_revoked(rig, binding_id)
                acknowledge_closed(rig, binding_id)
                result = future.result(timeout=3)
            assert result.status is HermesDispatchStatus.UNCERTAIN
            assert result.error_code == "hermes_response_error"
            assert len(requests) == 1
            with rig.sessions() as session:
                assert session.scalar(select(HermesDispatchResponse)) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
