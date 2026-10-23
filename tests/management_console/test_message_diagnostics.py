"""Regressions for the observed saved/current-account boundary and failures."""

import pytest
from sqlalchemy import select
from test_console import Harness
from test_live import begin, raw

from cf_agent_gateway.management_console.models import DirectorySource
from cf_agent_gateway.management_console.upstream import NativeReader, SourceError


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path)
    value.sync()
    yield value
    value.close()


def test_sync_changed_active_account_preserves_old_source_and_directory(h):
    old = h.contact()
    old_account = h.peer.account
    h.peer.account = "new-scanned-account"
    result = h.post("/sources/native/sync", expected=503)
    assert result["detail"] == "source_account_mismatch"
    with h.app.state.database_session_factory() as session:
        assert session.scalar(select(DirectorySource.account_id)) == old_account
    assert h.contact()["id"] == old["id"]


def test_explicitly_unverified_identity_never_produces_green_or_archive(h, monkeypatch):
    original = NativeReader.read

    def unverified(self, endpoint, **params):
        value = original(self, endpoint, **params)
        if endpoint == "api/status/auth":
            value.update(status="logged_in", identityVerified=False)
        return value

    monkeypatch.setattr(NativeReader, "read", unverified)
    live = h.app.state.console_live
    live.probe("native", True)
    assert h.get("/live/native")["bot"]["state"] == "unknown"
    with pytest.raises(SourceError, match="source_identity_unverified"):
        h.config.reader("native").account()


def test_one_unreadable_chat_does_not_hide_healthy_chat_and_reports_stage(h, monkeypatch):
    cursor = begin(h)
    original = NativeReader.messages

    bad_chat = next(chat["id"] for chat in h.peer.chats if chat["isGroup"])

    # Keep the real peer response shape, changing only one route's availability.
    def fail_one(self, chat_id, limit=200):
        if chat_id == bad_chat:
            raise SourceError("source_messages_connection_failed")
        return original(self, chat_id, limit)

    monkeypatch.setattr(NativeReader, "messages", fail_one)
    h.peer.messages["wxid_demo_0000"] = [raw(909)]
    h.app.state.console_live.tick("native")
    state = h.get("/live/native", after=cursor)
    assert len(state["items"]) == 1
    assert state["collection"]["state"] == "partial"
    assert state["collection"]["data_quality"]["failed_read_conversations"] == 1
    trace = state["diagnostics"]
    assert trace["stages"]["upstream"]["state"] == "partial"
    assert trace["incidents"][-1]["reason"] == "source_messages_connection_failed"
    assert "synthetic-native-secret" not in str(trace)
    assert "909" not in str(trace)
