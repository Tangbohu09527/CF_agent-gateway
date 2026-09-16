"""Local-only regressions for minute-scale Hermes work; no model or WeChat calls."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import httpx
import pytest
from sqlalchemy import select
from test_dispatch_worker_runtime import create_thread, database_factory, enqueue_message
from test_hermes_dispatch import create_dispatch_resources, create_follow_up_admission
from test_response_delivery_runtime import RecordingSender, create_domain

from cf_agent_gateway.config import Settings, WorkerSettings, load_settings
from cf_agent_gateway.delivery import ChannelDeliveryWorker, DeliveryOutboxRecord, DeliveryStatus
from cf_agent_gateway.hermes import (
    HERMES_SESSION_HEADER,
    HermesClient,
    HermesDispatchResponse,
    HermesDispatchResponseStore,
    HermesDispatchService,
    HermesDispatchWorker,
)
from cf_agent_gateway.hermes.errors import HermesExecutionTimeoutError
from cf_agent_gateway.hermes_timeouts import HermesTimeoutSettings
from cf_agent_gateway.response import ResponsePersistenceProcessor, ResponseRecord
from cf_agent_gateway.runtime import dispatch_worker
from cf_agent_gateway.runtime.heartbeat import HeartbeatPublisher, resident_heartbeat
from cf_agent_gateway.task.model import (
    HermesDispatchRecord,
    HermesDispatchRecordStore,
    HermesDispatchStateConflictError,
    HermesDispatchStatus,
)
from cf_agent_gateway.workspace.models import AIThread
from cf_agent_gateway.workspace.store import WorkspaceStore


def response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={HERMES_SESSION_HEADER: request.headers[HERMES_SESSION_HEADER]},
        json={"choices": [{"message": {"role": "assistant", "content": "real tool result"}}]},
    )


@pytest.mark.parametrize("elapsed", [31, 185, 590])
def test_long_response_is_persisted_and_delivered_through_outboxes(tmp_path, elapsed):
    factory, engine = database_factory(tmp_path)
    calls = []

    async def handler(request):
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 600.0,
            "write": 15.0,
            "pool": 5.0,
        }
        # Move only this worker call's loop clock; never wait real minutes.
        loop = asyncio.get_running_loop()
        original = loop.time
        loop.time = lambda: original() + elapsed
        await asyncio.sleep(0)
        calls.append(request)
        return response(request)

    try:
        with factory() as session:
            _, _, _, admission = create_domain(session)
            record, _ = HermesDispatchRecordStore(session).enqueue(admission)
        with HermesClient(
            "http://hermes.test", "test-key", "hermes-agent", transport=httpx.MockTransport(handler)
        ) as client:
            worker = dispatch_worker.build_dispatch_worker(
                Settings(worker=WorkerSettings(enabled=True)),
                session_factory=factory,
                hermes_client=client,
                sender_factory=None,
            )
            result = worker.run_once()
            assert result.status is HermesDispatchStatus.SUCCESS
            assert worker.run_once() is None
        with factory() as session:
            stored = session.scalar(select(HermesDispatchResponse))
            assert stored.dispatch_record_id == record.id
            assert stored.assistant_content == "real tool result"
            assert session.scalar(select(ResponseRecord)) is not None
            assert session.scalar(select(DeliveryOutboxRecord)).status is DeliveryStatus.QUEUED
            sender = RecordingSender()
            delivered = ChannelDeliveryWorker(session, lambda **kwargs: sender).run_once()
            assert delivered.status is DeliveryStatus.DELIVERED
            assert sender.calls == [("text", "wxid-alice", "real tool result")]
        assert len(calls) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure", ["budget", "read", "disconnect"])
def test_interrupted_call_never_replays_or_creates_a_result(tmp_path, failure):
    factory, engine = database_factory(tmp_path)
    calls = 0
    cancelled = []

    async def handler(request):
        nonlocal calls
        calls += 1
        if failure == "read":
            raise httpx.ReadTimeout("sensitive upstream text", request=request)
        if failure == "disconnect":
            raise httpx.ReadError("sensitive upstream text", request=request)
        loop = asyncio.get_running_loop()
        original = loop.time
        loop.time = lambda: original() + 601
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    try:
        with factory() as session:
            resources = create_dispatch_resources(session)
            first, _ = HermesDispatchRecordStore(session).enqueue(resources.admission)
            HermesDispatchRecordStore(session).enqueue(
                create_follow_up_admission(session, resources)
            )
        with HermesClient(
            "http://hermes.test", "test-key", "hermes-agent", transport=httpx.MockTransport(handler)
        ) as client:
            worker = dispatch_worker.build_dispatch_worker(
                Settings(),
                session_factory=factory,
                hermes_client=client,
                sender_factory=None,
            )
            result = worker.run_once()
            assert result.status is HermesDispatchStatus.UNCERTAIN
            assert worker.run_once() is None
            assert worker.claim_once(now=datetime.now(UTC) + timedelta(hours=2)) is None
        with factory() as session:
            assert session.get(HermesDispatchRecord, first.id).attempt_count == 1
            assert session.scalar(select(HermesDispatchResponse)) is None
            assert session.scalar(select(ResponseRecord)) is None
            assert session.scalar(select(DeliveryOutboxRecord)) is None
        assert calls == 1
        assert cancelled == ([True] if failure == "budget" else [])
    finally:
        engine.dispose()


def test_deadline_cancels_trickling_body_and_closes_stream():
    closed = []
    cancelled = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            loop = asyncio.get_running_loop()
            original = loop.time
            elapsed = 0
            loop.time = lambda: original() + elapsed
            try:
                while True:
                    elapsed += 20  # Each gap is below read=30, total exceeds execution=60.
                    yield b" "
                    await asyncio.sleep(0.001)
            finally:
                cancelled.append(True)

        async def aclose(self):
            closed.append(True)

    async def handler(request):
        return httpx.Response(200, stream=Body())

    with (
        HermesClient(
            "http://hermes.test",
            "test-key",
            "hermes-agent",
            timeouts=HermesTimeoutSettings(read_seconds=30, execution_seconds=60),
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(HermesExecutionTimeoutError),
    ):
        client.chat("test")
    assert cancelled == [True]
    assert closed == [True]


def test_deadline_closes_real_local_socket_before_return():
    # A loopback HTTP fixture only: no Hermes, LAN host or external service.
    async def scenario():
        disconnected = asyncio.Event()

        async def serve(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":")[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                assert await reader.read() == b""
                disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            with HermesClient(
                f"http://127.0.0.1:{port}",
                "local-test-key",
                "test-model",
                timeouts=HermesTimeoutSettings(execution_seconds=0.1),
            ) as client:
                with pytest.raises(HermesExecutionTimeoutError):
                    await asyncio.to_thread(client.chat, "local fixture")
                await asyncio.wait_for(disconnected.wait(), 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("field", list(asdict(HermesTimeoutSettings())))
@pytest.mark.parametrize("value", [0, -1, True, None, "600", float("nan"), float("inf"), 3601])
def test_invalid_timeout_settings_rejected(field, value):
    with pytest.raises(ValueError, match="hermes.timeouts"):
        HermesTimeoutSettings(**{field: value})


@pytest.mark.parametrize("raw", ["null", "[]", "{read_second: 300}", "{read_seconds: null}"])
def test_yaml_rejects_invalid_timeouts(tmp_path, raw):
    path = tmp_path / "settings.yaml"
    path.write_text(f"hermes:\n  timeouts: {raw}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(path)


def test_yaml_settings_reach_real_production_constructor(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    configured = dict(
        connect_seconds=7, read_seconds=730, write_seconds=19, pool_seconds=8, execution_seconds=750
    )
    path.write_text(
        json.dumps(
            {
                "database": {"url": "sqlite+pysqlite:///:memory:"},
                "worker": {"enabled": True},
                "hermes": {
                    "enabled": True,
                    "base_url": "http://hermes.test",
                    "timeouts": configured,
                },
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(path)
    seen = []

    class Client(HermesClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            seen.append(asdict(self._timeouts))

    stop = Event()
    stop.set()  # Construct production dependencies, never dispatch anything.
    dispatch_worker.run_dispatch_worker(
        settings,
        stop_event=stop,
        hermes_client_factory=Client,
        environment_reader=lambda name: "test-key",
    )
    assert seen == [configured]


def test_long_call_releases_db_and_renews_lease_during_shutdown(tmp_path, monkeypatch):
    factory, engine = database_factory(tmp_path)
    entered = Event()
    release = Event()
    renewed = Event()
    heartbeat_advanced = Event()
    other_finished = Event()
    sessions = []
    call_count = 0
    renew_count = 0
    original_renew = HermesDispatchRecordStore.renew_lease

    def renew(store, *args, **kwargs):
        nonlocal renew_count
        result = original_renew(store, *args, **kwargs)
        if args[0] == first.id:
            renew_count += 1
            if renew_count >= 4:
                renewed.set()
        return result

    monkeypatch.setattr(HermesDispatchRecordStore, "renew_lease", renew)

    async def handler(request):
        nonlocal call_count
        call_count += 1
        content = json.loads(request.content)["messages"][0]["content"]
        if content == "question-other":
            other_finished.set()
            return response(request)
        assert not sessions[0].in_transaction()
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return response(request)

    class Heartbeat:
        count = 0

        def write(self, state, **kwargs):
            if state == "running":
                self.count += 1
                if self.count >= 4:
                    heartbeat_advanced.set()

    try:
        with factory() as session:
            resources = create_dispatch_resources(session)
            first, _ = HermesDispatchRecordStore(session).enqueue(resources.admission)
            following, _ = HermesDispatchRecordStore(session).enqueue(
                create_follow_up_admission(session, resources)
            )
        with HermesClient(
            "http://hermes.test", "test-key", "hermes-agent", transport=httpx.MockTransport(handler)
        ) as client:

            def dispatcher(session):
                sessions.append(session)
                return HermesDispatchService(session, client)

            worker = HermesDispatchWorker(
                factory,
                dispatcher,
                lease_seconds=1,
                retry_limit=3,
                response_processor_factory=ResponsePersistenceProcessor,
            )
            stop = Event()
            heartbeat = HeartbeatPublisher(Heartbeat(), interval_seconds=0.05)
            with (
                ThreadPoolExecutor(max_workers=1) as executor,
                resident_heartbeat(heartbeat, stop_event=stop, phase="dispatching", concurrency=2),
            ):
                running = executor.submit(
                    worker.run, stop_event=stop, concurrency=2, idle_poll_seconds=0.01
                )
                try:
                    assert entered.wait(3)
                    # A separate DB session can write and dispatch while the first waits.
                    with factory() as session:
                        seed = create_thread(session, "other")
                        WorkspaceStore(session).ensure_source_binding(
                            ai_thread_id=seed.thread_id,
                            platform="test",
                            account_id=seed.source_account_id,
                            physical_conversation_id=seed.conversation_id,
                            sender_id="sender-other",
                        )
                        other = enqueue_message(session, seed, "other")
                    assert other_finished.wait(3)
                    stop.set()  # Stop claims; the active call must drain with heartbeats.
                    assert renewed.wait(3)
                    assert heartbeat_advanced.wait(3)
                    assert not running.done()
                    assert worker.claim_once() is None
                    with factory() as session:
                        record = session.get(HermesDispatchRecord, first.id)
                        assert record.status is HermesDispatchStatus.RUNNING
                        assert record.attempt_count == 1
                        assert session.get(HermesDispatchRecord, following.id).attempt_count == 0
                finally:
                    release.set()
                running.result(timeout=3)
            assert call_count == 2
            with factory() as session:
                assert (
                    session.get(HermesDispatchRecord, other.id).status
                    is HermesDispatchStatus.SUCCESS
                )
                assert (
                    session.get(HermesDispatchRecord, first.id).status
                    is HermesDispatchStatus.SUCCESS
                )
    finally:
        release.set()
        engine.dispose()


def test_stale_completion_cannot_rotate_session_or_save_result(tmp_path):
    factory, engine = database_factory(tmp_path)
    try:
        with factory() as session:
            resources = create_dispatch_resources(session)
            record, _ = HermesDispatchRecordStore(session).enqueue(resources.admission)
            HermesDispatchRecordStore(session).claim(record.id, claim_token="owner")
            session.get(AIThread, resources.thread.id).hermes_thread_id = "before"
            session.commit()
        from cf_agent_gateway.hermes.models import HermesDispatchOutcome

        outcome = HermesDispatchOutcome(
            message_id=record.message_id,
            workspace_id=record.workspace_id,
            ai_thread_id=record.ai_thread_id,
            assistant_content="result",
            requested_hermes_thread_id="before",
            next_hermes_thread_id="after",
        )
        with factory() as session:
            with pytest.raises(HermesDispatchStateConflictError):
                HermesDispatchResponseStore(session).complete_success(
                    record.id, claim_token="stale", outcome=outcome
                )
            assert session.get(AIThread, record.ai_thread_id).hermes_thread_id == "before"
            assert session.scalar(select(HermesDispatchResponse)) is None
            with pytest.raises(HermesDispatchStateConflictError):
                HermesDispatchResponseStore(session).complete_success(
                    record.id,
                    claim_token="owner",
                    outcome=replace(outcome, requested_hermes_thread_id="wrong"),
                )
            assert (
                session.get(HermesDispatchRecord, record.id).status is HermesDispatchStatus.RUNNING
            )
            assert session.scalar(select(HermesDispatchResponse)) is None
            HermesDispatchResponseStore(session).complete_success(
                record.id, claim_token="owner", outcome=outcome
            )
            session.expire_all()
            assert session.get(AIThread, record.ai_thread_id).hermes_thread_id == "after"
            assert (
                session.get(HermesDispatchRecord, record.id).status is HermesDispatchStatus.SUCCESS
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("field", ["connect_seconds", "write_seconds", "pool_seconds"])
def test_transport_timeout_upper_limit(field):
    with pytest.raises(ValueError):
        HermesTimeoutSettings(**{field: 120.01})


def test_examples_and_clean_install_generation_use_validated_defaults():
    import ast

    root = Path(__file__).resolve().parents[1]
    expected = HermesTimeoutSettings()
    for name in ("config/config.yaml", "config/production.yaml"):
        assert load_settings(root / name).hermes.timeouts == expected
    # Inspect the generated literal without running installation or recovery.
    tree = ast.parse((root / "deploy/prepare-clean-host.py").read_text(encoding="utf-8"))
    generated = [
        ast.literal_eval(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "timeouts"
    ]
    assert generated == [asdict(expected)]
