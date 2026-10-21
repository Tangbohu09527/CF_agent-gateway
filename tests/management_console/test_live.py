"""Real SQLite/API and native HTTP peer; never a real WeChat login or model."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_console import Harness

from cf_agent_gateway.management_console.config import ConsoleConfig
from cf_agent_gateway.management_console.live import observation
from cf_agent_gateway.management_console.live_models import ArchiveControl, ArchiveObservation
from cf_agent_gateway.management_console.service import ConsoleService
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model.models import HermesDispatchRecord


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path)
    value.sync()
    yield value
    value.close()


def raw(local, **values):
    return {
        "localId": local,
        "serverId": "server-" + str(local),
        "chatId": "wxid_demo_0000",
        "sender": "wxid_demo_0000",
        "senderName": "合成发言者",
        "type": 1,
        "content": "普通未授权消息 " + str(local),
        "timestamp": datetime.now(UTC).isoformat(),
        "isSelf": False,
        **values,
    }


def begin(h):
    h.post("/live/native/collection/start", {})
    h.app.state.console_live.tick("native")
    return h.get("/live/native")["cursor"]


def test_archive_only_baseline_self_ordinary_duplicate_late_and_commit_cursor(h):
    h.peer.messages["wxid_demo_0000"] = [raw(10)]
    cursor = begin(h)
    h.peer.messages["wxid_demo_0000"].extend(
        [
            raw(11),
            raw(12, isSelf=True, sender=h.peer.account),
            raw(9, timestamp="2025-10-01T00:00:00Z"),
        ]
    )
    h.app.state.console_live.tick("native")
    delta = h.get("/live/native", after=cursor, limit=2)
    assert len(delta["items"]) == 2 and delta["has_more"]
    later = h.get("/live/native", after=delta["cursor"])
    assert len(later["items"]) == 1
    records = delta["items"] + later["items"]
    assert len({r["message"]["event_id"] for r in records}) == 3
    assert any(r["message"]["direction"] == "outbound" for r in records)
    assert any(r["message"]["occurred_at"].startswith("2025") for r in records)
    assert all(r["upstream_readable_at"] <= r["archived_at"] for r in records)
    h.app.state.console_live.tick("native")
    assert h.get("/live/native", after=later["cursor"])["items"] == []
    with h.app.state.database_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(ArchiveObservation)
                .where(ArchiveObservation.message_id.is_not(None))
            )
            == 3
        )
        assert not session.scalar(select(Message.id).where(Message.source_server_id == "server-10"))


def test_restart_persists_collection_and_recovers_incremental_cursor(h):
    cursor = begin(h)
    h.peer.messages["wxid_demo_0000"] = [raw(1)]
    h.app.state.console_live.tick("native")
    first = h.get("/live/native", after=cursor)
    h.restart()
    after = h.get("/live/native", after=cursor)
    assert after["items"] == first["items"]
    assert after["collection"]["enabled"]
    h.peer.messages["wxid_demo_0000"].append(raw(2))
    h.app.state.console_live.tick("native")
    assert len(h.get("/live/native", after=first["cursor"])["items"]) == 1


def test_cross_source_cursor_account_change_offline_and_authentication(h):
    cursor = begin(h)
    live = h.app.state.console_live
    h.peer.offline = True
    live.probe("native", True)
    assert h.get("/live/native")["bot"]["state"] == "offline"
    h.peer.offline = False
    h.peer.fail = True
    live.probe("native", True)
    assert h.get("/live/native")["bot"]["state"] == "unknown"
    h.peer.fail = False
    h.peer.account = "other-bot"
    live.probe("native", True)
    assert h.get("/live/native")["bot"]["reason"] == "source_account_mismatch"
    h.peer.messages["wxid_demo_0000"] = [raw(1)]
    live.tick("native")
    assert h.get("/live/native", after=cursor)["items"] == []
    bad = h.client.get("/admin/console/live/native", params={"after": "not-a-cursor"})
    assert bad.status_code == 422
    h.post("/logout", {})
    assert h.client.get("/admin/console/live/native").status_code == 401


def test_status_expires_green_to_unknown_and_silent_bot_not_offline(h):
    h.app.state.console_live.probe("native", True)
    assert h.get("/live/native")["bot"]["state"] == "online"
    old = datetime.now(UTC) - timedelta(seconds=21)
    assert observation({"state": "online"}, old)["state"] == "unknown"
    assert observation({"state": "offline"}, old)["state"] == "unknown"
    assert h.get("/live/native")["collection"]["state"] == "disabled"
    assert not any(x["method"] != "GET" for x in h.peer.calls)


def test_collection_management_csrf_no_unverified_login_or_fake_controller(h):
    h.client.headers.pop("X-CSRF-Token")
    assert h.client.post("/admin/console/live/native/collection/start", json={}).status_code == 403
    assert h.client.post("/admin/console/services/native/login", json={}).status_code == 403
    h.client.headers["X-CSRF-Token"] = h.get("/me")["csrf_token"]
    status = h.get("/services/native")
    assert not status["capabilities"]["login"]
    assert status["controller"]["state"] == "unknown"
    assert h.client.post("/admin/console/services/native/start-service", json={}).status_code == 503
    h.peer.offline = True
    assert h.client.post("/admin/console/services/native/login", json={}).status_code == 409
    assert not any(x["path"].startswith("/api/ws") for x in h.peer.calls)
    with h.app.state.database_session_factory() as session:
        assert session.get(ArchiveControl, "native") is None


def test_formal_overlap_pagination_deduplicates_whole_partition(h):
    # Same persisted SQLite opened independently in enforced read-only mode:
    # every message overlaps, including records outside the requested page.
    config = ConsoleConfig(
        h.root,
        "read_only",
        {"native": replace(h.config.sources["native"], archive_url=h.settings.database.url)},
        h.config.admin,
    )
    try:
        with h.app.state.database_session_factory() as session:
            svc = ConsoleService(session, config)
            svc.platform = "wechat-demo"
            key = h.contact()["conversation_id"]
            first = svc.messages(key, "native", offset=0, limit=10)
            second = svc.messages(key, "native", offset=10, limit=10)
            assert first["total"] == second["total"] == 95
            assert not {m["event_id"] for m in first["items"]} & {
                m["event_id"] for m in second["items"]
            }
    finally:
        for engine in config._archives.values():
            engine.dispose()


def test_invalid_old_identity_is_visible_gap_and_does_not_stop_new_archive(h):
    h.peer.messages["wxid_demo_0000"] = [raw(31, sender="", type=49, isSelf=None)]
    cursor = begin(h)
    state = h.get("/live/native")
    assert state["collection"]["state"] == "partial"
    assert state["collection"]["data_quality"]["skipped_invalid_rows"] == 1
    h.peer.messages["wxid_demo_0000"].append(raw(32))
    h.app.state.console_live.tick("native")
    delta = h.get("/live/native", after=cursor)
    assert len(delta["items"]) == 1
    assert delta["items"][0]["message"]["content"].endswith("32")
    # If upstream repairs the old malformed baseline, it remains historical,
    # rather than becoming a new task/message replay after a restart.
    h.peer.messages["wxid_demo_0000"][0] = raw(31)
    h.restart()
    h.app.state.console_live.tick("native")
    assert not h.get("/live/native", after=delta["cursor"])["items"]
    with h.app.state.database_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0
