"""Explicit native DB failure must never masquerade as a healthy empty window."""

import httpx
import pytest
from sqlalchemy import func, select
from test_console import Harness
from test_live import begin, raw

from cf_agent_gateway.admission.models import MessageAdmissionOutcome
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.management_console.live_models import ArchiveControl, ArchiveObservation
from cf_agent_gateway.management_console.upstream import NativeReader, SourceConnection, SourceError
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model.models import HermesDispatchRecord


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path)
    value.sync()
    yield value
    value.close()


def test_native_message_database_error_is_allowlisted_and_redacted():
    responses = [
        (409, {"error": "active_message_database_unreadable", "detail": "private-db-canary"}),
        (409, {"error": "untrusted-error-with-private-db-canary"}),
        (200, []),
    ]

    def respond(request):
        status, value = responses.pop(0)
        return httpx.Response(status, json=value)

    reader = NativeReader(
        SourceConnection("synthetic", "http://synthetic.invalid/", "synthetic-only-secret"),
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(SourceError, match="^source_message_database_unreadable$") as error:
            reader.messages("synthetic-chat")
        assert "private-db-canary" not in str(error.value)
        with pytest.raises(SourceError, match="^source_messages_read_failed$"):
            reader.messages("synthetic-chat")
        assert reader.messages("synthetic-chat") == []
    finally:
        reader.close()


def archive_snapshot(h):
    with h.app.state.database_session_factory() as session:
        control = session.get(ArchiveControl, "native")
        return {
            "messages": session.scalar(select(func.count()).select_from(Message)),
            "observations": session.scalar(select(func.count()).select_from(ArchiveObservation)),
            "baseline": dict(control.baselined_chats),
            "last_success_at": control.last_success_at,
        }


def test_explicit_message_database_failure_preserves_data_then_recovers(h, monkeypatch):
    h.peer.messages["wxid_demo_0000"] = [raw(1200)]
    cursor = begin(h)
    h.peer.messages["wxid_demo_0000"].append(raw(1201))
    live = h.app.state.console_live
    live.tick("native")
    cursor = h.get("/live/native", after=cursor)["cursor"]
    before = archive_snapshot(h)
    h.peer.messages["wxid_demo_0000"].append(raw(1202))

    def unreadable(self, chat_id, limit=200):
        raise SourceError("source_message_database_unreadable")

    with monkeypatch.context() as patch:
        patch.setattr(NativeReader, "messages", unreadable)
        live.tick("native")
    failed = h.get("/live/native", after=cursor)
    assert failed["items"] == []
    assert failed["collection"]["state"] == "blocked"
    assert failed["collection"]["last_error"] == "source_message_database_unreadable"
    upstream = failed["diagnostics"]["stages"]["upstream"]
    assert upstream["state"] == "error"
    assert upstream["reason"] == "source_message_database_unreadable"
    assert archive_snapshot(h) == before

    live.tick("native")
    recovered = h.get("/live/native", after=cursor)
    assert len(recovered["items"]) == 1
    assert recovered["items"][0]["message"]["content"].endswith("1202")
    assert recovered["collection"]["last_error"] is None
    incident = next(
        value
        for value in recovered["diagnostics"]["incidents"]
        if value["reason"] == "source_message_database_unreadable"
    )
    assert incident["recovered_at"]
    live.tick("native")
    assert not h.get("/live/native", after=recovered["cursor"])["items"]
    with h.app.state.database_session_factory() as session:
        for model in (MessageAdmissionOutcome, HermesDispatchRecord, DeliveryOutboxRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_legitimate_empty_message_windows_remain_successful(h):
    begin(h)
    before = archive_snapshot(h)
    h.app.state.console_live.tick("native")
    result = h.get("/live/native")
    assert result["collection"]["state"] == "running"
    assert result["collection"]["last_error"] is None
    after = archive_snapshot(h)
    assert after["messages"] == before["messages"]
    assert after["observations"] == before["observations"]
    assert after["baseline"] == before["baseline"]
    assert after["last_success_at"] >= before["last_success_at"]


def test_explicit_database_failure_in_one_chat_keeps_healthy_archive(h, monkeypatch):
    cursor = begin(h)
    h.peer.messages["wxid_demo_0000"] = [raw(1203)]
    bad_chat = next(chat["id"] for chat in h.peer.chats if chat["isGroup"])
    original = NativeReader.messages

    def fail_one(self, chat_id, limit=200):
        if chat_id == bad_chat:
            raise SourceError("source_message_database_unreadable")
        return original(self, chat_id, limit)

    monkeypatch.setattr(NativeReader, "messages", fail_one)
    h.app.state.console_live.tick("native")
    result = h.get("/live/native", after=cursor)
    assert len(result["items"]) == 1
    assert result["collection"]["state"] == "partial"
    assert result["collection"]["data_quality"]["failed_read_conversations"] == 1
    assert result["diagnostics"]["stages"]["upstream"]["reason"] == (
        "source_message_database_unreadable"
    )
