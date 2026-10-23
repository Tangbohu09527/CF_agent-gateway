"""Regressions for the observed saved/current-account boundary and failures."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_console import Harness
from test_live import begin, raw

from cf_agent_gateway.admission.models import MessageAdmissionOutcome
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
    private_canary = "private-message-canary-not-for-diagnostics"
    h.peer.messages["wxid_demo_0000"] = [raw(909, content=private_canary)]
    h.app.state.console_live.tick("native")
    state = h.get("/live/native", after=cursor)
    assert len(state["items"]) == 1
    assert state["collection"]["state"] == "partial"
    assert state["collection"]["data_quality"]["failed_read_conversations"] == 1
    trace = state["diagnostics"]
    assert trace["stages"]["upstream"]["state"] == "partial"
    assert trace["incidents"][-1]["reason"] == "source_messages_connection_failed"
    assert "synthetic-native-secret" not in str(trace)
    assert private_canary not in str(trace)


def test_committed_message_processing_uses_journal_and_does_not_invent_ai(h):
    cursor = begin(h)
    private_canary = "private-message-body-never-in-processing"
    h.peer.messages["wxid_demo_0000"] = [raw(910, content=private_canary)]
    h.app.state.console_live.tick("native")
    item = h.get("/live/native", after=cursor)["items"][0]
    trace = item["message"]["processing"]
    stages = {stage["stage"]: stage for stage in trace["stages"]}
    assert stages["increment"]["sequence"] == item["sequence"]
    assert stages["upstream"]["at"] == item["upstream_readable_at"]
    assert stages["archive"]["at"] == item["archived_at"]
    assert stages["admission"]["state"] == "disabled"
    assert stages["hermes"]["state"] == "disabled"
    assert stages["delivery"]["state"] == "disabled"
    assert stages["authorization"]["state"] == "unverified"
    assert stages["file_tools"]["state"] == "unverified"
    assert private_canary not in str(trace)
    assert "synthetic-native-secret" not in str(trace)


def test_database_failure_is_visible_and_recovers_without_resetting_baseline(h, monkeypatch):
    cursor = begin(h)
    live = h.app.state.console_live
    h.peer.messages["wxid_demo_0000"] = [raw(911)]

    def fail_commit(*args, **kwargs):
        raise OperationalError("synthetic insert", {}, Exception("private-database-canary"))

    with monkeypatch.context() as patch:
        patch.setattr(live, "persist_window", fail_commit)
        live.tick("native")
    failed = h.get("/live/native", after=cursor)
    assert failed["items"] == []
    assert failed["collection"]["last_error"] == "candidate_commit_failed"
    assert failed["diagnostics"]["stages"]["persistence"]["state"] == "error"
    assert "private-database-canary" not in str(failed)
    live.tick("native")
    recovered = h.get("/live/native", after=cursor)
    assert len(recovered["items"]) == 1
    incident = next(
        row for row in recovered["diagnostics"]["incidents"] if row["stage"] == "persistence"
    )
    assert incident["recovered_at"] is not None
    assert recovered["collection"]["last_error"] is None


def test_existing_pending_business_evidence_is_not_labelled_archive_only(h):
    cursor = begin(h)
    h.peer.messages["wxid_demo_0000"] = [raw(912)]
    h.app.state.console_live.tick("native")
    item = h.get("/live/native", after=cursor)["items"][0]
    with h.app.state.database_session_factory() as session:
        session.add(
            MessageAdmissionOutcome(
                message_id=int(item["message"]["id"].split(":")[1]),
                state="pending",
                evidence_origin="runtime",
                requested_scope=frozenset(),
                requested_skill_ids=frozenset(),
                risk_level="low",
            )
        )
        session.commit()
    trace = h.get("/live/native", after=cursor)["items"][0]["message"]["processing"]
    stages = {stage["stage"]: stage for stage in trace["stages"]}
    assert trace["evidence"] == "read_only_message_archive"
    assert stages["admission"]["state"] == "pending"
    assert stages["authorization"]["state"] == "unverified"
    assert stages["hermes"]["state"] == "unverified"


def test_all_upstream_requests_fail_reports_blocked_and_preserves_last_success(h, monkeypatch):
    cursor = begin(h)
    before = h.get("/live/native")["collection"]["last_success_at"]

    def timeout(*args, **kwargs):
        raise SourceError("source_messages_connection_failed")

    with monkeypatch.context() as patch:
        patch.setattr(NativeReader, "messages", timeout)
        h.app.state.console_live.tick("native")
    failed = h.get("/live/native", after=cursor)
    assert failed["collection"]["state"] == "blocked"
    assert failed["collection"]["last_success_at"] == before
    assert failed["diagnostics"]["stages"]["upstream"]["state"] == "error"
    assert failed["collection"]["last_error"] == "source_messages_all_failed"
    h.app.state.console_live.tick("native")
    assert h.get("/live/native")["collection"]["state"] == "running"
