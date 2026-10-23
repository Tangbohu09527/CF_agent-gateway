"""Real candidate HTTP/SQLite; native WeChat is explicitly a synthetic HTTP peer."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from cf_agent_gateway.access.policy_models import UserAccessPolicy
from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.delivery.models import (
    DeliveryAttempt,
    DeliveryOutboxRecord,
    DeliveryReceipt,
    DeliveryStatus,
)
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.identity.models import EnterpriseIdentity, SourceIdentityMapping
from cf_agent_gateway.management_console.app import install_console
from cf_agent_gateway.management_console.auth import password_record
from cf_agent_gateway.management_console.avatars import avatar_host, fetch_avatar
from cf_agent_gateway.management_console.config import ConsoleConfig
from cf_agent_gateway.management_console.demo import DemoPeer, seed_archive
from cf_agent_gateway.management_console.models import DirectorySource
from cf_agent_gateway.management_console.upstream import NativeReader, SourceConnection, SourceError
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.profile_b.models import DerivedProfile
from cf_agent_gateway.response.models import ResponsePartRecord, ResponseRecord
from cf_agent_gateway.task.model.models import HermesDispatchRecord
from cf_agent_gateway.workspace.models import AIThread, EmployeeWorkspace

PREFIX = "/admin/console"
PASSWORD = "synthetic-browser-password-2026"


class Harness:
    def __init__(self, root):
        self.root = root
        self.peer = DemoPeer(0, "synthetic-native-secret")
        self.peer.thread.start()
        self.config = ConsoleConfig(
            root,
            "demo",
            {
                "native": SourceConnection(
                    "合成测试 Bot",
                    f"http://127.0.0.1:{self.peer.server_port}/",
                    self.peer.token,
                )
            },
            password_record("测试管理员", PASSWORD),
        )
        self.settings = Settings(
            database=DatabaseSettings(url="sqlite:///" + (root / "candidate.db").as_posix())
        )
        self.client = None
        self.restart()

    def restart(self):
        if self.client:
            self.client.__exit__(None, None, None)
        self.app = install_console(create_app(self.settings), self.config)
        parent = self.app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with parent(app):
                with app.state.database_session_factory() as session:
                    if not session.get(DirectorySource, "native"):
                        session.add(
                            DirectorySource(
                                id="native",
                                label="合成测试 Bot",
                                status="not_synced",
                                coverage="尚未同步",
                                sync_details={},
                            )
                        )
                        session.commit()
                    seed_archive(session)
                yield

        self.app.router.lifespan_context = lifespan
        self.client = TestClient(self.app)
        self.client.__enter__()
        login = self.client.post(
            PREFIX + "/login", json={"username": "测试管理员", "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        self.client.headers["X-CSRF-Token"] = login.json()["csrf_token"]

    def get(self, path, **params):
        response = self.client.get(PREFIX + path, params={"source": "native", **params})
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path, body=None, expected=200):
        response = self.client.post(PREFIX + path, json=body)
        assert response.status_code == expected, response.text
        return response.json()

    def sync(self):
        return self.post("/sources/native/sync")

    def contact(self, username="wxid_demo_0000"):
        rows = self.get("/contacts", q=username)["items"]
        return next(r for r in rows if r["username"] == username)

    def enroll(self, contact, **values):
        return self.post(
            f"/contacts/{contact['id']}/employee",
            {
                "name": "企业员工甲",
                "enabled": True,
                "bot_allowed": True,
                "group_allowed": False,
                "group_id": None,
                **values,
            },
        )

    def close(self):
        self.client.__exit__(None, None, None)
        self.peer.shutdown()
        self.peer.server_close()
        self.peer.thread.join(5)
        for engine in self.config._archives.values():
            engine.dispose()


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def test_complete_filtered_native_pagination_and_nonrecent_friends(h):
    result = h.sync()
    assert result["sync_details"]["pagination"]["contacts"] == [
        {"offset": 0, "returned": 197},
        {"offset": 200, "returned": 52},
        {"offset": 400, "returned": 0},
    ]
    assert result["sync_details"]["pagination"]["chats"][-1] == {"offset": 100, "returned": 0}
    assert h.get("/contacts")["total"] == 247
    pages = [h.get("/contacts", offset=n, limit=100)["items"] for n in (0, 100, 200)]
    assert len({r["id"] for page in pages for r in page}) == 247
    assert h.contact("wxid_demo_0246")["last_at"] is None
    assert h.get("/contacts", kind="official")["total"] == 1
    assert h.get("/contacts", kind="openim")["total"] == 1
    assert h.get("/conversations")["total"] == 57
    assert h.get("/rooms")["total"] == 53
    assert result["capabilities"]["all_groups"] is False


def test_enroll_from_contact_idempotent_rename_sync_and_restart_preserve_policy(h):
    h.sync()
    original = h.contact()
    identity = h.enroll(original)["id"]
    assert h.enroll(original)["id"] == identity
    h.peer.contacts[0].update(nick_name="改了昵称", remark="新备注", small_head_url=None)
    h.sync()
    current = h.contact()
    assert current["id"] == original["id"]
    assert current["display_name"] == "新备注"
    assert current["employee"]["name"] == "企业员工甲"
    assert current["employee"]["group_id"] is None
    assert h.contact("wxid_demo_0002")["employee"] is None  # identical nickname does not bind
    h.restart()
    assert h.contact()["employee"]["id"] == identity
    with h.app.state.database_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceIdentityMapping)) == 1
        assert session.scalar(select(func.count()).select_from(DerivedProfile)) == 0
        policy = session.scalar(select(UserAccessPolicy))
        assert not policy.permission_scope and not policy.allowed_skills


@pytest.mark.parametrize("failure,status", [("offline", "offline"), ("fail", "error")])
def test_sync_failure_keeps_employee_and_cache(h, failure, status):
    h.sync()
    original = h.contact()
    identity = h.enroll(original)["id"]
    setattr(h.peer, failure, True)
    h.post("/sources/native/sync", expected=503)
    assert h.get("/state")["sources"][0]["status"] == status
    assert h.contact()["employee"]["id"] == identity
    assert h.contact()["employee"]["bot_allowed"] is True
    assert h.get("/contacts")["total"] == 247


def test_not_synced_empty_unverified_and_absent_contact_retained(h):
    assert h.get("/state")["sources"][0]["status"] == "not_synced"
    assert h.get("/contacts")["total"] == 0
    h.sync()
    contact = h.contact()
    h.enroll(contact)
    h.peer.contacts = []
    result = h.sync()
    assert result["status"] == "empty_unverified"
    assert h.contact()["employee"]["bot_allowed"]
    assert h.get("/contacts")["total"] == 247


def test_stopped_app_can_read_stored_directory_but_never_claims_online(h):
    h.peer.app_not_running = True
    result = h.sync()
    assert result["status"] == "offline_cached"
    assert result["bot_status"] == "app_not_running"
    assert "缓存资料" in result["coverage"]
    assert h.get("/contacts")["total"] == 247
    assert h.contact()["remark"] == "林溪 · 运营"
    assert h.contact()["nick_name"] == "林溪"
    assert h.contact()["avatar_url"]


def test_account_switch_never_reuses_contact_binding(h):
    h.sync()
    original = h.contact()
    h.enroll(original)
    h.peer.account = "demo-bot-b"
    h.post("/sources/native/sync", expected=503)
    assert h.contact()["id"] == original["id"]
    h.config.sources["native-b"] = replace(h.config.sources["native"], label="另一个合成账号")
    with h.app.state.database_session_factory() as session:
        session.add(DirectorySource(id="native-b", label="另一个合成账号", sync_details={}))
        session.commit()
    h.post("/sources/native-b/sync")
    new = h.get("/contacts", source="native-b", q=original["username"])["items"][0]
    assert new["id"] != original["id"]
    assert new["employee"] is None
    response = h.client.get(PREFIX + "/contacts/" + original["id"])
    assert response.status_code == 200


def test_link_existing_identity_preserves_identity_and_group_selection(h):
    h.sync()
    contact = h.contact()
    with h.app.state.database_session_factory() as session:
        session.add(EnterpriseIdentity(id="existing-identity", display_name="原员工"))
        session.commit()
    enrolled = h.enroll(
        contact, existing_identity_id="existing-identity", group_id="demo-employee-group"
    )
    assert enrolled["id"] == "existing-identity"
    assert (
        h.get("/contacts", linked="yes", authorized="yes", group_id="demo-employee-group")["total"]
        == 1
    )
    h.enroll(contact, bot_allowed=False, enabled=False)
    assert not h.contact()["employee"]["bot_allowed"]
    assert not h.contact()["employee"]["enabled"]


def test_same_named_groups_stable_and_configure_without_typing_ids(h):
    h.sync()
    groups = h.get("/rooms", q="商品运营群")["items"]
    assert len(groups) == 2
    for room in groups:
        result = h.post(
            f"/rooms/{room['id']}/settings", {"enabled": True, "group_id": "demo-room-group"}
        )
        assert result["contact"]["room"]["group_id"] == "demo-room-group"
        assert result["contact"]["members"]["supported"] is False
    assert groups[0]["username"] != groups[1]["username"]
    h.post(
        f"/rooms/{groups[0]['id']}/settings",
        {"enabled": True, "group_id": "demo-employee-group"},
        expected=422,
    )
    h.post(
        f"/contacts/{groups[0]['id']}/employee",
        {
            "name": "invalid",
            "enabled": True,
            "bot_allowed": True,
            "group_allowed": True,
            "group_id": None,
        },
        expected=422,
    )


def test_login_csrf_origin_logout_and_no_secret_in_responses(h):
    h.sync()
    anonymous = TestClient(h.app)
    assert anonymous.get(PREFIX + "/state").status_code == 401
    assert (
        anonymous.post(
            PREFIX + "/login", json={"username": "测试管理员", "password": "bad"}
        ).status_code
        == 401
    )
    saved = h.client.headers.pop("X-CSRF-Token")
    assert h.client.post(PREFIX + "/sources/native/sync").status_code == 403
    h.client.headers["X-CSRF-Token"] = saved
    assert (
        h.client.post(
            PREFIX + "/sources/native/sync", headers={"Origin": "https://other.invalid"}
        ).status_code
        == 403
    )
    serialized = h.client.get(PREFIX + "/state").text
    assert PASSWORD not in serialized and h.peer.token not in serialized
    assert (
        h.client.get(PREFIX + "/state", headers={"Authorization": "Bearer invalid"}).status_code
        == 401
    )
    h.post("/logout")
    assert h.client.get(PREFIX + "/state").status_code == 401


def test_demo_and_real_modes_cannot_share_runtime(h):
    from cf_agent_gateway.management_console.runtime import build_config

    (h.root / "console-mode.json").write_text('{"mode":"demo"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="separate runtime"):
        build_config(h.root, "read_only", 0)


def test_bulk_prevalidation_and_candidate_group_permissions(h):
    h.sync()
    first, second = h.contact(), h.contact("wxid_demo_0001")
    h.enroll(first)
    h.post(
        "/contacts/bulk", {"ids": [first["id"], second["id"]], "bot_allowed": False}, expected=422
    )
    assert h.contact()["employee"]["bot_allowed"]
    h.enroll(second)
    result = h.post(
        "/contacts/bulk",
        {
            "ids": [first["id"], second["id"]],
            "group_allowed": True,
            "group_id": "demo-employee-group",
        },
    )
    assert result == {"updated": 2, "failures": [], "candidate_only": True}
    assert h.contact()["employee"]["group_allowed"]
    with h.app.state.database_session_factory() as session:
        for policy in session.scalars(select(UserAccessPolicy)):
            assert not policy.permission_scope and not policy.allowed_skills


@pytest.mark.parametrize(
    "status,content_type,data",
    [
        (302, "image/png", b"png"),
        (200, "text/html", b"<script>"),
        (200, "image/png", b"x" * 262145),
    ],
    ids=["redirect", "wrong-content-type", "oversize"],
)
def test_avatar_rejects_redirect_type_and_oversize(h, monkeypatch, status, content_type, data):
    class Response:
        def getheader(self, *args):
            return content_type

        def read(self, size):
            return data[:size]

    Response.status = status

    class Connection:
        def __init__(self, *args):
            pass

        def request(self, method, path, headers):
            assert method == "GET" and "Authorization" not in headers

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("1.1.1.1", 443))])
    monkeypatch.setattr("cf_agent_gateway.management_console.avatars.PinnedHTTPS", Connection)
    with pytest.raises(ValueError):
        fetch_avatar("https://wx.qlogo.cn/a", h.root / "avatars")


def test_archive_message_paging_beijing_dates_reply_files_and_filters(h):
    h.sync()
    chat = h.contact()["conversation_id"]
    first = h.get(f"/conversations/{chat}/messages")
    assert first["total"] == 95 and len(first["items"]) == 40
    names = {row["id"]: row["name"] for row in first["sender_options"]}
    assert names["wxid_demo_0001"] == "周颖 · 商品"
    selected = h.get(f"/conversations/{chat}/messages", sender="wxid_demo_0001")
    assert selected["items"] and all(
        row["sender_id"] == "wxid_demo_0001" for row in selected["items"]
    )
    second = h.get(f"/conversations/{chat}/messages", offset=40)
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    assert any(
        r["attachments"] and r["attachments"][0]["filename"] == "产品手册（中文 空格）.pdf"
        for r in first["items"]
    )
    assert any(r["reply"] for r in first["items"])
    assert any(r["message_type"] == "image" for r in first["items"])
    september = h.get(
        f"/conversations/{chat}/messages", date_from="2026-09-30", date_to="2026-09-30"
    )
    assert september["total"] == 1  # UTC15:50 is Sep30 23:50 Beijing; next record is Oct1
    assert (
        h.get(f"/conversations/{chat}/messages", q="does-not-exist")["status"]
        == "no_filter_results"
    )
    assert (
        h.get(f"/conversations/{chat}/messages", date_from="2025-01-01", date_to="2025-01-31")[
            "status"
        ]
        == "no_records_in_range"
    )
    assert (
        h.client.get(
            PREFIX + f"/conversations/{chat}/messages",
            params={"source": "native", "date_from": "bad"},
        ).status_code
        == 422
    )
    assert (
        h.client.get(
            PREFIX + f"/conversations/{chat}/messages", params={"source": "wrong"}
        ).status_code
        == 403
    )


def test_no_native_mutations_model_tasks_or_downloads_when_reading(h):
    h.sync()
    before = len(h.peer.calls)
    h.get("/contacts")
    h.get("/rooms")
    h.get("/conversations")
    h.get(f"/conversations/{h.contact()['conversation_id']}/messages")
    assert len(h.peer.calls) == before
    assert {r["path"] for r in h.peer.calls} <= {"/api/status/auth", "/api/contacts", "/api/chats"}
    assert all(r["method"] == "GET" for r in h.peer.calls)
    with h.app.state.database_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(HermesDispatchRecord)) == 0
    response = h.client.post(
        "/admin/profile-b/test-messages",
        json={
            "account_id": "demo-bot-a",
            "sender_id": "wxid_demo_0000",
            "conversation_id": "wxid_demo_0000",
            "kind": "private",
            "text": "never execute",
            "event_id": "never-execute-candidate",
        },
    )
    assert response.status_code == 403


def test_unreachable_node_does_not_create_success(h):
    h.sync()
    identity = h.enroll(h.contact(), group_id="demo-employee-group")["id"]
    response = h.client.post(
        "/admin/profile-b/profiles/ensure", json={"kind": "employee", "subject_id": identity}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "node_capability_not_verified"
    state = h.get("/state")["profile"]
    assert not any(row["status"] == "ready" for row in state["derived"])


def test_readonly_external_archive_cannot_write(h):
    config = ConsoleConfig(
        h.root,
        "read_only",
        {"native": replace(h.config.sources["native"], archive_url=h.settings.database.url)},
        h.config.admin,
    )
    with config.archive("native", None) as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 380
        with pytest.raises(OperationalError, match="readonly"):
            session.execute(text("DELETE FROM messages"))
    for engine in config._archives.values():
        engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a",
        "https://qlogo.cn.evil.invalid/a",
        "file:///x",
        "http://user:password@qlogo.cn/a",
        "https://qlogo.cn:8080/a",
    ],
)
def test_avatar_rejects_arbitrary_sources(url):
    with pytest.raises(ValueError):
        avatar_host(url)


def test_avatar_dns_private_address_blocked(h, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="not_public"):
        fetch_avatar("https://wx.qlogo.cn/a", h.root / "avatars")


def test_native_paging_contract_and_nonread_routes_rejected():
    reader = NativeReader(
        SourceConnection("test", "http://native.invalid", "synthetic"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[{"username": "same-page"}])
        ),
    )
    with pytest.raises(SourceError, match="not_advancing"):
        reader.pages("api/contacts", 200)
    with pytest.raises(SourceError, match="not_allowed"):
        reader.read("api/messages/send")
    reader.close()


def test_response_receipt_dedup_and_failed_delivery_are_distinct(h):
    h.sync()
    with h.app.state.database_session_factory() as s:
        identity = EnterpriseIdentity(id="delivery-identity", display_name="合成员工")
        s.add(identity)
        s.flush()
        s.add(EmployeeWorkspace(id="delivery-workspace", enterprise_identity_id=identity.id))
        s.flush()
        s.add(
            AIThread(
                id="delivery-thread",
                workspace_id="delivery-workspace",
                thread_type="private",
                thread_key="synthetic-thread",
            )
        )
        s.flush()
        origin = s.scalar(
            select(Message).where(
                Message.conversation_id == "wxid_demo_0000", Message.is_self.is_(False)
            )
        )
        for i, status in enumerate((DeliveryStatus.DELIVERED, DeliveryStatus.FAILED)):
            response = ResponseRecord(
                response_id=f"response-{i}",
                idempotency_key=f"response-{i}",
                message_id=origin.id + i,
                workspace_id="delivery-workspace",
                ai_thread_id="delivery-thread",
                content_sha256="0" * 64,
                part_count=1,
                created_at=datetime(2026, 10, 21, tzinfo=UTC),
            )
            s.add(response)
            s.flush()
            s.add(
                ResponsePartRecord(
                    response_id=response.response_id,
                    ordinal=0,
                    part_type="text",
                    text=f"合成回复{i}",
                )
            )
            outbox = DeliveryOutboxRecord(
                response_id=response.response_id,
                idempotency_key=f"delivery-{i}",
                channel="wechat",
                account_id="demo-bot-a",
                conversation_id="wxid_demo_0000",
                target_key=str(i) * 64,
                status=status,
            )
            s.add(outbox)
            s.flush()
            if i == 0:
                echo = s.scalar(
                    select(Message).where(
                        Message.conversation_id == "wxid_demo_0000", Message.is_self.is_(True)
                    )
                )
                attempt = DeliveryAttempt(
                    delivery_id=outbox.id,
                    part_ordinal=0,
                    attempt_number=1,
                    provider_idempotency_key="synthetic",
                    status="delivered",
                )
                s.add(attempt)
                s.flush()
                s.add(
                    DeliveryReceipt(
                        delivery_id=outbox.id,
                        part_ordinal=0,
                        response_id=response.response_id,
                        attempt_id=attempt.id,
                        provider_message_id=echo.source_message_id,
                        receipt_payload={},
                    )
                )
                echo_id = echo.id
        s.commit()
    data = h.get(f"/conversations/{h.contact()['conversation_id']}/messages", limit=100)
    assert data["total"] == 96  # 95 archived, minus one echoed self, plus two Responses
    assert not any(row["id"] == f"m:{echo_id}" for row in data["items"])
    assert {row["delivery_status"] for row in data["items"] if row["id"].startswith("r:")} == {
        "delivered",
        "failed",
    }
