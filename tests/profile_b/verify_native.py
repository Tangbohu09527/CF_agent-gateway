"""Additional real-component evidence after browser.cjs; no official code injection."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml
from fastapi.testclient import TestClient

from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.profile_b.app import install_management
from cf_agent_gateway.profile_b.hermes import Connection, HermesNode


class FaultProxy(BaseHTTPRequestHandler):
    """Short-lived network fault fixture; all successful writes reach official Hermes."""

    def log_message(self, *_):
        pass

    def forward(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.command == "PUT" and self.path.startswith("/api/config"):
            self.send_response(503)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        with httpx.Client(trust_env=False, timeout=30) as client:
            r = client.request(
                self.command,
                self.server.target + self.path,
                content=body,
                headers={
                    "Authorization": self.headers.get("Authorization", ""),
                    "Content-Type": "application/json",
                },
            )
        self.send_response(r.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    do_GET = do_POST = do_PUT = forward


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    keys = json.loads((runtime / "demo-secrets.json").read_text(encoding="utf-8"))
    original = Connection(
        **json.loads((runtime / "connections.json").read_text(encoding="utf-8"))["isolated-hermes"]
    )
    node = HermesNode(original)
    api = httpx.Client(
        base_url="http://127.0.0.1:18760/admin/profile-b/",
        trust_env=False,
        headers={"Authorization": "Bearer " + keys["admin"]},
        timeout=120,
    )

    def call(path, method="GET", body=None):
        r = api.request(method, path, json=body)
        assert r.status_code == 200, (path, r.status_code, r.text)
        return r.json()

    s = call("state")
    assert len(s["derived"]) == 4
    home = runtime / "hermes-home"
    disk = []
    for binding in s["derived"]:
        profile = home / "profiles" / binding["actual_profile"]
        template = next(t for t in s["templates"] if t["id"] == binding["template_id"])
        assert (profile / "SOUL.md").read_text(encoding="utf-8") == template["soul"]
        config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
        assert config["display"]["language"] == template["language"]
        assert not (profile / "auth.json").exists()
        assert not (profile / "vault.json.enc").exists()
        env_lines = (profile / ".env").read_text(encoding="utf-8").splitlines()
        assert not [
            line for line in env_lines if line.strip() and not line.lstrip().startswith("#")
        ]
        jobs = profile / "cron" / "jobs.json"
        assert not jobs.exists() or not json.loads(jobs.read_text(encoding="utf-8")).get("jobs")
        with sqlite3.connect(f"file:{(profile / 'state.db').as_posix()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT session_id, role, content FROM messages WHERE role='user'"
            ).fetchall()
            sessions = db.execute(
                "SELECT id,profile_name,system_prompt_hash FROM sessions"
            ).fetchall()
        expected = next(x for x in s["sessions"] if x["derived_id"] == binding["id"])
        assert all(row[0] == expected["hermes_session_id"] for row in rows)
        assert all(row[1] == binding["actual_profile"] for row in sessions)
        disk.append(
            {
                "profile": binding["actual_profile"],
                "session_id": expected["hermes_session_id"],
                "user_messages": [r[2] for r in rows],
                "db_sha256": hashlib.sha256((profile / "state.db").read_bytes()).hexdigest(),
                "sessions": sessions,
                "credential_files_empty_or_absent": True,
                "no_cloned_cron": True,
            }
        )
    expected_markers = [
        {"CF_MSG_A1", "CF_MSG_A2", "CF_MSG_A3"},
        {"CF_MSG_B1", "CF_MSG_B2"},
        {"CF_MSG_GA1", "CF_MSG_GA2"},
        {"CF_MSG_GB1"},
    ]
    assert {frozenset(d["user_messages"]) for d in disk} == {frozenset(m) for m in expected_markers}
    employee = next(e for e in s["employees"] if e["sender_id"] == "test-a")
    binding = next(d for d in s["derived"] if d["identity_id"] == employee["id"])
    profile = home / "profiles" / binding["actual_profile"]
    memory = profile / "memories" / "profile-b-sentinel.txt"
    memory.write_text("CF_MEMORY_PRESERVE_A", encoding="utf-8")
    (profile / "profile-readonly.txt").write_text("CF_FILE_PROFILE_A_READ_ONLY", encoding="utf-8")
    node.request(
        "PUT",
        "/api/config",
        json={"profile": binding["actual_profile"], "config": {"agent": {"max_turns": 4}}},
    )
    # A real template change forces a sync rather than a ready/idempotent shortcut.
    template = next(t for t in s["templates"] if t["id"] == binding["template_id"])
    edited = {k: template[k] for k in ("name", "kind", "soul", "language")}
    edited.update(
        soul="CF_SOUL_EMP_V3\n原有历史与本地非受管配置保留。", expected_version=template["version"]
    )
    call("templates/" + template["id"], "PUT", edited)
    for e in s["employees"]:
        result = call("profiles/ensure", "POST", {"kind": "employee", "subject_id": e["id"]})
        assert result["status"] == "ready"
    config = node.request("GET", "/api/config", params={"profile": binding["actual_profile"]})
    assert config["agent"]["max_turns"] == 4
    assert memory.read_text(encoding="utf-8") == "CF_MEMORY_PRESERVE_A"
    # Official control new session on an already Gateway-created profile, never pre-create profiles.
    fresh = node.turn(
        binding["actual_profile"],
        None,
        "control:" + str(uuid.uuid4()),
        "CF_MSG_NEW_SESSION",
        lambda *_: None,
    )
    assert json.loads(fresh["text"])["soul_markers"] == ["CF_SOUL_EMP_V3"]
    assert fresh["session_id"] != next(
        x["session_id"] for x in disk if x["profile"] == binding["actual_profile"]
    )
    tool = call(
        "test-messages",
        "POST",
        {
            "account_id": "isolated-bot",
            "sender_id": "test-a",
            "conversation_id": "test-a",
            "kind": "private",
            "mentioned": True,
            "event_id": "tool-" + str(uuid.uuid4()),
            "text": "CF_MSG_READ_ONLY",
        },
    )
    assert tool["session_id"] == next(
        x["session_id"] for x in disk if x["profile"] == binding["actual_profile"]
    )
    assert json.loads(tool["text"])["tool_read_markers"] == ["CF_FILE_PROFILE_A_READ_ONLY"]
    assert json.loads(tool["text"])["soul_markers"] == ["CF_SOUL_EMP_V1"]
    with sqlite3.connect(f"file:{(profile / 'state.db').as_posix()}?mode=ro", uri=True) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE session_id=? AND role='tool' AND tool_name='read_file'",
                (tool["session_id"],),
            ).fetchone()[0]
            >= 1
        )
    with node.rpc() as rpc:
        rpc.call(
            "session.close",
            {"session_id": fresh["runtime_id"], "profile": binding["actual_profile"]},
        )

    # Real partial creation: HTTP fault after official create, before config; resume same binding.
    fixture_connections = {"native": original}
    fixture = install_management(
        create_app(
            Settings(
                database=DatabaseSettings(
                    url="sqlite:///" + (runtime / "failure-matrix.db").as_posix()
                )
            )
        ),
        fixture_connections,
    )
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), FaultProxy)
    proxy.target = original.url
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    import os

    os.environ["CF_AGENT_GATEWAY_ADMIN_TOKEN"] = keys["admin"]
    failures = {}
    try:
        with TestClient(fixture, headers={"Authorization": "Bearer " + keys["admin"]}) as test:

            def manage(path, body):
                r = test.post("/admin/profile-b/" + path, json=body)
                assert r.status_code == 200, r.text
                return r.json()

            nid = manage("nodes", {"name": "真实故障注入节点", "connection_ref": "native"})["id"]
            assert manage("nodes/" + nid + "/check", {})["status"] == "ready"
            tid = manage(
                "templates",
                {
                    "name": "故障恢复模板",
                    "kind": "employee",
                    "soul": "CF_SOUL_RECOVERY",
                    "language": "zh-CN",
                },
            )["id"]
            gid = manage(
                "groups",
                {"name": "故障恢复组", "kind": "employee", "template_id": tid, "node_id": nid},
            )["id"]
            eid = manage(
                "employees",
                {
                    "employee_id": "fault-" + uuid.uuid4().hex[:8],
                    "name": "故障恢复测试员工",
                    "account_id": "isolated-bot",
                    "sender_id": "fault-sender",
                    "bot_allowed": True,
                    "group_allowed": False,
                    "group_id": gid,
                },
            )["id"]
            fixture_connections["native"] = Connection(
                url=f"http://127.0.0.1:{proxy.server_port}",
                token=original.token,
                initial_config=original.initial_config,
            )
            failed = manage("profiles/ensure", {"kind": "employee", "subject_id": eid})
            assert failed["status"] == "create_failed" and failed["error"] == "hermes_http_503"
            partial = test.get("/admin/profile-b/state").json()["derived"][0]
            assert partial["applied_version"] is None
            actual = next(p for p in node.profiles() if p["name"] == partial["actual_profile"])
            fixture_connections["native"] = original
            resumed = manage("profiles/ensure", {"kind": "employee", "subject_id": eid})
            assert resumed["id"] == failed["id"] and resumed["status"] == "ready"
            assert len([p for p in node.profiles() if p["name"] == actual["name"]]) == 1
            # Unknown pre-existing fixture must be refused, never counted as a managed creation.
            collision_id = manage(
                "employees",
                {
                    "employee_id": "collision-" + uuid.uuid4().hex[:8],
                    "name": "同名冲突测试",
                    "account_id": "isolated-bot",
                    "sender_id": "collision-sender",
                    "bot_allowed": True,
                    "group_id": gid,
                },
            )["id"]
            collision_name = (
                "cf-e-" + hashlib.sha256(("employee:" + collision_id).encode()).hexdigest()[:24]
            )
            node.request(
                "POST",
                "/api/profiles",
                json={
                    "name": collision_name,
                    "description": "unknown-owner-fixture",
                    "no_skills": True,
                },
            )
            conflict = manage("profiles/ensure", {"kind": "employee", "subject_id": collision_id})
            assert (
                conflict["status"] == "create_failed"
                and conflict["error"] == "unknown_profile_name_collision"
            )
            assert (
                next(p for p in node.profiles() if p["name"] == collision_name)["description"]
                == "unknown-owner-fixture"
            )
            # Real sync failure keeps the previously applied version and the failed target distinct.
            r = test.put(
                "/admin/profile-b/templates/" + tid,
                json={
                    "name": "故障恢复模板",
                    "kind": "employee",
                    "soul": "CF_SOUL_RECOVERY_V2",
                    "language": "en",
                    "expected_version": 1,
                },
            )
            assert r.status_code == 200
            fixture_connections["native"] = Connection(
                url=f"http://127.0.0.1:{proxy.server_port}",
                token=original.token,
                initial_config=original.initial_config,
            )
            sync_failed = manage("profiles/ensure", {"kind": "employee", "subject_id": eid})
            assert sync_failed["status"] == "sync_failed"
            failed_binding = next(
                d
                for d in test.get("/admin/profile-b/state").json()["derived"]
                if d["id"] == resumed["id"]
            )
            assert failed_binding["applied_version"] == 1 and failed_binding["desired_version"] == 2
            fixture_connections["native"] = original
            sync_resumed = manage("profiles/ensure", {"kind": "employee", "subject_id": eid})
            assert sync_resumed["status"] == "ready"
            failures = {
                "partial_create": failed,
                "safe_resume": resumed,
                "unknown_collision": conflict,
                "sync_failure": sync_failed,
                "failed_target_version": failed_binding["desired_version"],
                "previous_applied_version": failed_binding["applied_version"],
                "sync_resume": sync_resumed,
                "fixture_profiles_are_not_part_of_four_subject_topology": True,
            }
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)
    result = {
        "passed": True,
        "four_profile_disk_evidence": disk,
        "new_session_soul": json.loads(fresh["text"])["soul_markers"],
        "new_session_id": fresh["session_id"],
        "old_session_after_sync": tool["session_id"],
        "old_session_soul": json.loads(tool["text"])["soul_markers"],
        "official_read_file": json.loads(tool["text"])["tool_read_markers"],
        "unmanaged_agent_max_turns_retained": 4,
        "memory_sentinel_retained": True,
        "failures": failures,
        "file_authorization_scope": (
            "local official read_file only; FileBrowser task identity/MCP return gap remains"
        ),
        "state": call("state"),
    }
    (runtime / "native-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "Native profile disk, new/old session, official read_file, "
        "and real network failure/recovery evidence passed."
    )
    api.close()


if __name__ == "__main__":
    main()
