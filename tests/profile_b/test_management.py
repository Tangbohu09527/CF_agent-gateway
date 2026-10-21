"""Management contract tests with real SQLite migrations and an explicit Hermes unit double.

These tests do not exercise official Hermes or provide Profile A acceptance evidence.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Barrier, Event, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from cf_agent_gateway.access.policy_models import UserAccessPolicy
from cf_agent_gateway.admin.auth import get_authenticated_roles
from cf_agent_gateway.agent_profile.models import AgentProfile
from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.migration import get_schema_version
from cf_agent_gateway.profile_b import service as management_service
from cf_agent_gateway.profile_b.app import install_management
from cf_agent_gateway.profile_b.hermes import Connection, NodeError
from cf_agent_gateway.profile_b.models import DerivedProfile, TemplateVersion

PREFIX = "/admin/profile-b"
ADMIN_TOKEN = "synthetic-profile-b-unit-admin-token"
NODE_TOKEN = "synthetic-profile-b-unit-node-token"


@dataclass
class HermesUnitState:
    profiles: dict = field(default_factory=dict)
    ensure_calls: list = field(default_factory=list)
    apply_calls: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    fail_create: bool = False
    fail_apply: bool = False
    fail_turn: bool = False
    block_create: bool = False
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)


class HermesUnitDouble:
    """No network, official agent, file tool, or protocol implementation is used here."""

    def __init__(self, connection, state):
        self.connection, self.state = connection, state

    def check(self):
        if self.connection.url.endswith(":2"):
            raise NodeError("unit_node_unreachable")
        return {"rest": True, "rpc": True, "profile_create": True, "soul": True}

    def ensure_created(self, name, owner, *, initialize=True):
        with self.state.lock:
            self.state.ensure_calls.append((name, owner, initialize))
        if self.state.block_create:
            self.state.entered.set()
            if not self.state.release.wait(10):
                raise NodeError("unit_create_timeout")
        if self.state.fail_create:
            raise NodeError("unit_create_failed")
        existing = self.state.profiles.get(name)
        if existing and existing["owner"] != owner:
            raise NodeError("unknown_profile_name_collision")
        self.state.profiles.setdefault(name, {"owner": owner})

    def apply(self, name, soul, language):
        self.state.apply_calls.append((name, soul, language))
        if self.state.fail_apply:
            raise NodeError("unit_apply_failed")
        self.state.profiles[name].update(soul=soul, language=language)

    def turn(self, profile, stored_id, key, text, persist_session):
        if self.state.fail_turn:
            raise NodeError("unit_turn_uncertain")
        stored = stored_id or "unit-session-" + profile
        runtime = "unit-runtime-" + profile
        persist_session(stored, runtime)
        self.state.turns.append((profile, stored, key, text))
        row = len(self.state.turns) * 2
        return {
            "profile": profile,
            "session_id": stored,
            "runtime_id": runtime,
            "text": "explicit-unit-double-response",
            "persisted_turn": {
                "complete": True,
                "row_ids": [row - 1, row],
                "user_row_id": row - 1,
                "final_assistant_row_id": row,
            },
        }


class Harness:
    def __init__(self, settings, connections, unit):
        self.settings, self.connections, self.unit = settings, connections, unit
        self.event_sequence = 0
        self.client = None
        self.restart()

    def restart(self):
        if self.client is not None:
            self.client.__exit__(None, None, None)
        self.app = install_management(create_app(self.settings), self.connections)
        self.client = TestClient(
            self.app,
            headers={"Authorization": "Bearer " + ADMIN_TOKEN},
            raise_server_exceptions=False,
        )
        self.client.__enter__()

    def close(self):
        self.client.__exit__(None, None, None)

    def request(self, method, path, payload=None, status=200):
        response = self.client.request(method, PREFIX + path, json=payload)
        assert response.status_code == status, response.text
        return response.json()

    def state(self):
        return self.request("GET", "/state")

    def employee(self, name="甲", **changes):
        payload = {
            "employee_id": "employee-" + str(len(self.state()["employees"]) + 1),
            "name": name,
            "account_id": "isolated-bot",
            "sender_id": "sender-" + str(len(self.state()["employees"]) + 1),
            **changes,
        }
        return self.request("POST", "/employees", payload)["id"]

    def edit_employee(self, key, **changes):
        item = next(item for item in self.state()["employees"] if item["id"] == key)
        body = {
            field: item[field]
            for field in (
                "name",
                "enabled",
                "bot_allowed",
                "group_allowed",
                "group_id",
            )
        }
        return self.request("PUT", "/employees/" + key, {**body, **changes})

    def template(self, kind="employee", name=None):
        return self.request(
            "POST",
            "/templates",
            {
                "name": name or "模板-" + kind,
                "kind": kind,
                "soul": "仅使用隔离测试数据。版本一。",
                "language": "zh-CN",
            },
        )["id"]

    def node(self, connection_ref="unit-good", checked=True):
        key = self.request(
            "POST",
            "/nodes",
            {
                "name": connection_ref,
                "connection_ref": connection_ref,
            },
        )["id"]
        if checked:
            self.request("POST", "/nodes/" + key + "/check")
        return key

    def group(self, kind="employee", template=None, node=None, name=None):
        return self.request(
            "POST",
            "/groups",
            {
                "name": name or "分组-" + kind,
                "kind": kind,
                "template_id": template or self.template(kind),
                "node_id": node or self.node(),
            },
        )["id"]

    def room(self, physical="room-a", group=None):
        return self.request(
            "POST",
            "/rooms",
            {
                "name": physical,
                "account_id": "isolated-bot",
                "conversation_id": physical,
                "group_id": group,
            },
        )["id"]

    def derive(self, subject, kind="employee", status=200):
        return self.request(
            "POST",
            "/profiles/ensure",
            {
                "kind": kind,
                "subject_id": subject,
            },
            status=status,
        )

    def send(self, sender="sender-1", conversation=None, kind="private", **changes):
        status = changes.pop("status", 200)
        self.event_sequence += 1
        return self.request(
            "POST",
            "/test-messages",
            {
                "account_id": "isolated-bot",
                "sender_id": sender,
                "conversation_id": conversation or sender,
                "kind": kind,
                "event_id": "event-" + str(self.event_sequence),
                "text": "仅供单元测试的合成正文",
                **changes,
            },
            status=status,
        )


@pytest.fixture
def management(tmp_path, monkeypatch):
    """Each test migrates its own file database; only the Hermes client is substituted."""
    settings = Settings(
        database=DatabaseSettings(url="sqlite:///" + (tmp_path / "management.db").as_posix())
    )
    monkeypatch.setenv(settings.api.admin_token_env, ADMIN_TOKEN)
    monkeypatch.delenv("CF_GATEWAY_STARTUP_MIGRATION_MODE", raising=False)
    unit = HermesUnitState()
    monkeypatch.setattr(
        management_service, "HermesNode", lambda connection: HermesUnitDouble(connection, unit)
    )
    connections = {
        "unit-good": Connection("http://127.0.0.1:1", NODE_TOKEN),
        "unit-down": Connection("http://127.0.0.1:2", NODE_TOKEN),
        "unit-second": Connection("http://127.0.0.1:3", NODE_TOKEN),
    }
    harness = Harness(settings, connections, unit)
    try:
        yield harness
    finally:
        unit.release.set()
        harness.close()


def test_management_authentication_and_audits_do_not_expose_credentials(management):
    h = management
    assert get_schema_version(h.app.state.database_engine) == "20261021_01"
    for headers in ({}, {"Authorization": "Bearer incorrect"}, {"X-Role": "admin"}):
        request = h.client.build_request("GET", PREFIX + "/state", headers=headers)
        if "Authorization" not in headers:
            request.headers.pop("authorization", None)
        response = h.client.send(request)
        assert response.status_code == 401
    request = h.client.build_request("POST", PREFIX + "/employees", json={})
    request.headers.pop("authorization")
    assert h.client.send(request).status_code == 401
    assert h.state()["employees"] == []
    h.app.dependency_overrides[get_authenticated_roles] = lambda: frozenset({"employee"})
    assert h.client.get(PREFIX + "/state").status_code == 403
    h.app.dependency_overrides.clear()
    h.employee()
    state = h.state()
    assert state["audits"][0]["actor"].startswith("admin:")
    assert state["audits"][0]["request_id"]
    assert ADMIN_TOKEN not in str(state)
    assert NODE_TOKEN not in str(state)
    assert "127.0.0.1:1" not in str(state)


def test_bot_permission_and_ungrouped_identity_do_not_dispatch(management):
    h = management
    employee = h.employee()
    assert h.send(status=403)["detail"] == "employee_bot_permission_denied"
    h.edit_employee(employee, bot_allowed=True)
    assert h.send(status=409)["detail"] == "authorized_but_ungrouped"
    assert h.derive(employee, status=409)["detail"] == "authorized_but_ungrouped"
    assert h.state()["derived"] == []
    assert h.unit.ensure_calls == h.unit.turns == []
    group = h.group()
    h.edit_employee(employee, group_id=group)
    h.derive(employee)
    assert h.send()["profile"] == h.state()["derived"][0]["actual_profile"]
    h.edit_employee(employee, bot_allowed=False)
    assert h.send(status=403)["detail"] == "employee_bot_permission_denied"
    h.edit_employee(employee, bot_allowed=True, enabled=False)
    assert h.send(status=403)["detail"] == "employee_bot_permission_denied"
    assert len(h.unit.turns) == 1


def test_four_subjects_keep_distinct_profiles_and_physical_sessions(management):
    h = management
    node = h.node()
    employee_group = h.group(node=node)
    room_group = h.group("group", node=node)
    a = h.employee("同名员工", bot_allowed=True, group_allowed=True, group_id=employee_group)
    b = h.employee("同名员工", bot_allowed=True, group_allowed=True, group_id=employee_group)
    room_a, room_b = h.room("room-a", room_group), h.room("room-b", room_group)
    for subject, kind in ((a, "employee"), (b, "employee"), (room_a, "group"), (room_b, "group")):
        h.derive(subject, kind)
    assert len({item["actual_profile"] for item in h.state()["derived"]}) == 4
    assert len(h.unit.profiles) == 4
    private_a, private_b = h.send(), h.send("sender-2")
    private_a_again = h.send()
    group_a = h.send(conversation="room-a", kind="group")
    group_a_other_sender = h.send("sender-2", "room-a", "group")
    group_b = h.send(conversation="room-b", kind="group")
    assert private_a["session_id"] == private_a_again["session_id"]
    assert private_a["thread_id"] == private_a_again["thread_id"]
    assert group_a["session_id"] == group_a_other_sender["session_id"]
    assert group_a["thread_id"] == group_a_other_sender["thread_id"]
    assert len({result["profile"] for result in (private_a, private_b, group_a, group_b)}) == 4
    assert len({result["session_id"] for result in (private_a, private_b, group_a, group_b)}) == 4
    h.edit_employee(b, group_allowed=False)
    assert (
        h.send("sender-2", "room-a", "group", status=403)["detail"]
        == "employee_group_permission_denied"
    )
    assert h.send("sender-2")["profile"] == private_b["profile"]
    assert h.send(conversation="room-a", kind="group")["profile"] == group_a["profile"]
    assert len(h.state()["sessions"]) == 4


def test_source_binding_event_scope_and_untrusted_fields_are_rejected(management):
    h = management
    group = h.group()
    employee = h.employee(bot_allowed=True, group_allowed=True, group_id=group)
    h.derive(employee)
    original = h.send(event_id="stable-event")
    assert h.send(event_id="stable-event")["replayed"] is True
    assert len(h.unit.turns) == 1
    assert (
        h.send(event_id="stable-event", text="changed", status=409)["detail"]
        == "event_id_scope_conflict"
    )
    assert h.send(account_id="other-bot", status=403)["detail"] == "source_account_not_registered"
    assert h.send("unknown-sender", status=403)["detail"] == "source_identity_unresolved"
    assert (
        h.send(conversation="other-person", status=403)["detail"]
        == "private_source_binding_mismatch"
    )
    assert (
        h.send(conversation="unknown-room", kind="group", status=403)["detail"]
        == "conversation_source_binding_mismatch"
    )
    assert (
        h.send(conversation="sender-1", kind="group", status=403)["detail"]
        == "conversation_source_binding_mismatch"
    )
    assert (
        h.send(conversation="unknown-room", kind="group", mentioned=False, status=403)["detail"]
        == "bot_not_mentioned"
    )
    for field_name, value in (
        ("profile", original["profile"]),
        ("task_id", original["task_id"]),
        ("node_id", "arbitrary"),
    ):
        h.send(**{field_name: value}, status=422)
    assert len(h.unit.turns) == 1


def test_template_cas_allowlist_and_regroup_preserve_file_policy(management):
    h = management
    node, template = h.node(), h.template()
    group = h.group(template=template, node=node)
    employee = h.employee(bot_allowed=True, group_id=group)
    with h.app.state.database_session_factory() as session:
        policy = session.scalar(
            select(UserAccessPolicy).where(UserAccessPolicy.enterprise_identity_id == employee)
        )
        policy.permission_scope = frozenset({"catalog:test-read"})
        policy.allowed_skills = frozenset({"synthetic-read"})
        session.commit()
    h.derive(employee)
    assert h.unit.ensure_calls[-1][2] is True
    first_message = h.send()
    update = {
        "name": "模板-employee",
        "kind": "employee",
        "soul": "隔离版本二",
        "language": "en",
        "expected_version": 1,
    }
    h.request("PUT", "/templates/" + template, update)
    h.request("PUT", "/templates/" + template, update, status=409)
    h.request("PUT", "/templates/" + template, {**update, "kind": "group"}, status=422)
    for field_name in ("mcp_servers", "config", "path", "permission_scope"):
        h.request(
            "POST", "/templates", {**update, "name": field_name, field_name: "unsafe"}, status=422
        )
    binding = h.state()["derived"][0]
    assert (binding["status"], binding["applied_version"], binding["desired_version"]) == (
        "sync_pending",
        1,
        2,
    )
    h.derive(employee)
    assert h.unit.ensure_calls[-1][2] is False
    assert h.state()["sessions"][0]["effective_status"] == "existing_session_pending"
    assert h.send()["session_id"] == first_message["session_id"]
    replacement = h.template(name="新模板")
    h.request(
        "PUT",
        "/groups/" + group,
        {
            "name": "调组模板",
            "kind": "employee",
            "template_id": replacement,
            "node_id": node,
        },
    )
    h.derive(employee)
    assert h.unit.ensure_calls[-1][2] is False
    h.edit_employee(employee, group_allowed=True)
    with h.app.state.database_session_factory() as session:
        policy = session.scalar(
            select(UserAccessPolicy).where(UserAccessPolicy.enterprise_identity_id == employee)
        )
        assert policy.permission_scope == frozenset({"catalog:test-read"})
        assert policy.allowed_skills == frozenset({"synthetic-read"})
        assert (
            session.scalar(
                select(func.count())
                .select_from(TemplateVersion)
                .where(TemplateVersion.template_id == template)
            )
            == 2
        )
    assert h.state()["templates"][0]["version"] == 2


def test_kind_constraints_prevent_employee_room_and_template_mixups(management):
    h = management
    node = h.node()
    employee_group = h.group(node=node)
    room_group = h.group("group", node=node)
    employee = h.employee()
    room = h.room()
    h.request(
        "PUT",
        "/employees/" + employee,
        {
            "name": "甲",
            "enabled": True,
            "bot_allowed": True,
            "group_allowed": True,
            "group_id": room_group,
        },
        status=422,
    )
    h.request(
        "PUT",
        "/rooms/" + room,
        {
            "name": "群",
            "enabled": True,
            "group_id": employee_group,
        },
        status=422,
    )
    room_template = next(
        item["template_id"] for item in h.state()["groups"] if item["id"] == room_group
    )
    h.request(
        "POST",
        "/groups",
        {
            "name": "wrong-template",
            "kind": "employee",
            "template_id": room_template,
            "node_id": node,
        },
        status=422,
    )
    h.request(
        "POST",
        "/nodes",
        {
            "name": "unknown-reference",
            "connection_ref": "unregistered",
        },
        status=422,
    )
    h.request(
        "POST",
        "/nodes",
        {
            "name": "direct-url",
            "connection_ref": "unit-good",
            "url": "http://example.test",
        },
        status=422,
    )


def test_node_and_partial_sync_failures_never_mark_target_version_applied(management):
    h = management
    down = h.node("unit-down")
    assert h.state()["nodes"][0]["status"] == "unavailable"
    down_group = h.group(node=down)
    employee = h.employee(bot_allowed=True, group_id=down_group)
    assert h.derive(employee, status=409)["detail"] == "node_capability_not_verified"
    assert h.state()["derived"] == []
    good = h.node()
    template = h.state()["groups"][0]["template_id"]
    h.request(
        "PUT",
        "/groups/" + down_group,
        {
            "name": "ready-group",
            "kind": "employee",
            "template_id": template,
            "node_id": good,
        },
    )
    h.unit.fail_create = True
    binding_id = h.derive(employee)["id"]
    failed = h.state()["derived"][0]
    assert (failed["status"], failed["applied_version"], failed["last_error"]) == (
        "create_failed",
        None,
        "unit_create_failed",
    )
    h.unit.fail_create = False
    assert h.derive(employee)["id"] == binding_id
    h.request(
        "PUT",
        "/templates/" + template,
        {
            "name": "template-v2",
            "kind": "employee",
            "soul": "version-two",
            "language": "en",
            "expected_version": 1,
        },
    )
    h.unit.fail_apply = True
    h.derive(employee)
    failed = h.state()["derived"][0]
    assert (failed["status"], failed["applied_version"], failed["desired_version"]) == (
        "sync_failed",
        1,
        2,
    )
    h.send(status=409)
    assert h.unit.turns == []
    h.unit.fail_apply = False
    h.derive(employee)
    assert h.state()["derived"][0]["applied_version"] == 2
    assert len(h.unit.profiles) == 1
    assert any(item["result"] == "unit_apply_failed" for item in h.state()["audits"])


def test_malformed_room_identifier_is_a_validation_error_not_server_failure(management):
    h = management
    h.derive("not-a-numeric-room", "group", status=422)
    h.request(
        "PUT",
        "/rooms/not-a-numeric-room",
        {
            "name": "合成群",
            "enabled": True,
            "group_id": None,
        },
        status=422,
    )


def test_uncertain_dispatch_is_retained_and_not_automatically_reexecuted(management):
    h = management
    employee = h.employee(bot_allowed=True, group_id=h.group())
    h.derive(employee)
    h.unit.fail_turn = True
    assert h.send(event_id="uncertain", status=502)["detail"] == "unit_turn_uncertain"
    assert len(h.state()["in_flight"]) == 1
    assert h.state()["in_flight"][0]["status"] == "uncertain"
    h.unit.fail_turn = False
    assert (
        h.send(event_id="uncertain", status=409)["detail"]
        == "request_already_recorded_no_automatic_replay"
    )
    assert h.unit.turns == []


def test_cross_node_change_retains_original_binding_and_blocks_dispatch(management):
    h = management
    group = h.group()
    employee = h.employee(bot_allowed=True, group_id=group)
    h.derive(employee)
    original = h.state()["derived"][0]
    second = h.node("unit-second")
    h.request(
        "PUT",
        "/groups/" + group,
        {
            "name": "待迁移",
            "kind": "employee",
            "template_id": original["template_id"],
            "node_id": second,
        },
    )
    blocked = h.state()["derived"][0]
    assert blocked["status"] == "blocked_move"
    assert (blocked["node_id"], blocked["actual_profile"]) == (
        original["node_id"],
        original["actual_profile"],
    )
    h.derive(employee, status=409)
    h.send(status=409)
    assert len(h.unit.ensure_calls) == 1
    assert h.unit.turns == []


def test_restart_preserves_configuration_profile_and_conversation_binding(management):
    h = management
    employee = h.employee(bot_allowed=True, group_id=h.group())
    h.derive(employee)
    first = h.send()
    before = h.state()
    h.restart()
    after = h.state()
    for collection in ("employees", "groups", "templates", "nodes", "derived", "sessions"):
        assert after[collection] == before[collection]
    resumed = h.send()
    assert resumed["session_id"] == first["session_id"]
    assert resumed["thread_id"] == first["thread_id"]
    assert len(h.unit.ensure_calls) == 1


def test_repeated_create_while_node_operation_is_running_has_one_binding(management):
    h = management
    employee = h.employee(bot_allowed=True, group_id=h.group())
    h.unit.block_create = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            h.client.post,
            PREFIX + "/profiles/ensure",
            json={"kind": "employee", "subject_id": employee},
        )
        try:
            assert h.unit.entered.wait(5)
            duplicate = h.derive(employee, status=409)
            assert duplicate["detail"] == "operation_in_progress"
        finally:
            h.unit.release.set()
        assert first.result(timeout=10).status_code == 200
    binding_id = h.state()["derived"][0]["id"]
    assert h.derive(employee)["id"] == binding_id
    assert len(h.unit.ensure_calls) == len(h.unit.apply_calls) == 1
    assert len(h.state()["derived"]) == 1


def test_simultaneous_first_create_handles_database_unique_race(management):
    h = management
    employee = h.employee(bot_allowed=True, group_id=h.group())
    barrier, lock, readers = Barrier(2), Lock(), set()

    def both_read_before_insert(_connection, _cursor, statement, _parameters, context, _many):
        # Coordinate the real SQL read boundary, not the service implementation or its result.
        if (
            "FROM profile_b_derived" not in statement
            or "WHERE profile_b_derived.identity_id" not in statement
        ):
            return
        with lock:
            if len(readers) >= 2 or id(context) in readers:
                return
            readers.add(id(context))
        barrier.wait(timeout=10)

    engine = h.app.state.database_engine
    event.listen(engine, "after_cursor_execute", both_read_before_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    h.client.post,
                    PREFIX + "/profiles/ensure",
                    json={"kind": "employee", "subject_id": employee},
                )
                for _ in range(2)
            ]
            responses = [future.result(timeout=15) for future in futures]
    finally:
        event.remove(engine, "after_cursor_execute", both_read_before_insert)
    assert all(response.status_code in {200, 409} for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert any(response.status_code == 200 for response in responses)
    assert len(h.state()["derived"]) == len(h.unit.profiles) == 1
    with h.app.state.database_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DerivedProfile)) == 1
        assert session.scalar(select(func.count()).select_from(AgentProfile)) == 1
