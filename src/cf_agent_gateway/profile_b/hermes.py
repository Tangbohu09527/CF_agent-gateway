"""Authenticated official Hermes REST + JSON-RPC client; no shell or agent tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
from websockets.sync.client import connect


class NodeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Connection:
    url: str
    token: str = field(repr=False)
    # Operator-owned policy, never supplied by a browser, template or model.
    initial_config: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (
                parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            )
            or len(self.token) < 24
        ):
            raise ValueError("node requires HTTPS or loopback HTTP and a strong credential")


class HermesNode:
    def __init__(self, connection: Connection):
        self.connection = connection

    def request(self, method: str, path: str, **kwargs):
        try:
            with httpx.Client(
                base_url=self.connection.url.rstrip("/"),
                headers={"Authorization": "Bearer " + self.connection.token},
                timeout=25,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.request(method, path, **kwargs)
            if response.status_code >= 400:
                raise NodeError(f"hermes_http_{response.status_code}")
            result = response.json()
            if not isinstance(result, dict):
                raise NodeError("node_invalid_json_object")
            return result
        except (httpx.HTTPError, ValueError) as error:
            raise NodeError("node_unreachable_or_invalid_response") from error

    def check(self):
        profiles = self.request("GET", "/api/profiles")
        schema = self.request("GET", "/api/config/schema")
        if not isinstance(profiles.get("profiles"), list) or not schema.get("fields"):
            raise NodeError("unsupported_hermes_api")
        with self.rpc() as rpc:
            rpc.call("gateway.ping", {})
        return {"rest": True, "rpc": True, "profile_create": True, "soul": True}

    def profiles(self):
        return self.request("GET", "/api/profiles")["profiles"]

    def ensure_created(self, name: str, owner: str, *, initialize: bool = True):
        existing = next((p for p in self.profiles() if p["name"] == name), None)
        if existing is not None:
            if existing.get("description") != owner:
                raise NodeError("unknown_profile_name_collision")
        else:
            # No cloning of memory, history, credentials or cron.
            self.request(
                "POST",
                "/api/profiles",
                json={
                    "name": name,
                    "description": owner,
                    "no_skills": True,
                    "clone_from_default": False,
                    "clone_all": False,
                    "clone_channels": False,
                },
            )
            existing = next((p for p in self.profiles() if p["name"] == name), None)
            if existing is None or existing.get("description") != owner:
                raise NodeError("profile_creation_unconfirmed")
        # Idempotent provisioning also resumes a create whose later config write failed.
        if initialize and self.connection.initial_config:
            self.request(
                "PUT",
                "/api/config",
                json={
                    "profile": name,
                    "config": self.connection.initial_config,
                },
            )

    def apply(self, name: str, soul: str, language: str):
        self.request("PUT", f"/api/profiles/{name}/soul", json={"content": soul})
        self.request(
            "PUT",
            "/api/config",
            json={
                "profile": name,
                "config": {"display": {"language": language}},
            },
        )
        actual_soul = self.request("GET", f"/api/profiles/{name}/soul")
        config = self.request("GET", "/api/config", params={"profile": name})
        if (
            actual_soul.get("content") != soul
            or config.get("display", {}).get("language") != language
        ):
            raise NodeError("managed_fields_readback_mismatch")

    def rpc(self):
        url = (
            self.connection.url.rstrip("/")
            .replace("https://", "wss://")
            .replace("http://", "ws://")
        )
        return Rpc(url + "/api/ws?" + urlencode({"token": self.connection.token}))

    def turn(self, profile: str, stored_id: str | None, key: str, text: str, persist_session):
        with self.rpc() as rpc:
            if stored_id:
                session = rpc.call(
                    "session.resume",
                    {
                        "profile": profile,
                        "session_id": stored_id,
                        "omit_messages": True,
                        "close_on_disconnect": False,
                    },
                )
            else:
                session = rpc.call(
                    "session.create",
                    {
                        "profile": profile,
                        "idempotency_key": key,
                        "source": "cf-profile-b-test",
                        "close_on_disconnect": False,
                        "follow_profile_config": True,
                    },
                )
            actual_profile = session.get("info", {}).get("profile_name")
            if actual_profile != profile:
                raise NodeError("hermes_profile_route_mismatch")
            stored = session.get("stored_session_id") or session.get("resumed")
            runtime = session["session_id"]
            if not stored or (stored_id and stored != stored_id):
                raise NodeError("hermes_session_identity_missing")
            persist_session(stored, runtime)
            ack = rpc.call(
                "prompt.submit", {"session_id": runtime, "profile": profile, "text": text}
            )
            if ack.get("status") != "streaming" or not ack.get("user_row_id"):
                raise NodeError("hermes_turn_not_exclusively_accepted")
            while True:
                event = rpc.receive()
                params = event.get("params", {})
                if params.get("type") == "message.complete" and params.get("session_id") == runtime:
                    payload = params.get("payload", {})
                    if (
                        payload.get("error")
                        or payload.get("failure_reason")
                        or payload.get("status") in {"error", "failed"}
                    ):
                        raise NodeError("hermes_turn_failed")
                    receipt = payload.get("persisted_turn") or {}
                    if receipt.get("user_row_id") != ack["user_row_id"]:
                        raise NodeError("hermes_turn_receipt_mismatch")
                    return {
                        "profile": actual_profile,
                        "session_id": stored,
                        "runtime_id": runtime,
                        "text": payload.get("text", ""),
                        "persisted_turn": payload.get("persisted_turn"),
                    }


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.sequence = 0
        self.events = []

    def __enter__(self):
        try:
            self.ws = connect(
                self.url, open_timeout=20, close_timeout=5, proxy=None, max_size=8_388_608
            )
            return self
        except Exception as error:
            raise NodeError("hermes_rpc_unavailable") from error

    def __exit__(self, *_):
        self.ws.close()

    def receive(self):
        if self.events:
            return self.events.pop(0)
        return self._receive_wire()

    def _receive_wire(self):
        try:
            return json.loads(self.ws.recv(timeout=90))
        except Exception as error:
            raise NodeError("hermes_rpc_timeout_or_closed") from error

    def call(self, method: str, params: dict):
        self.sequence += 1
        rid = f"cf-{self.sequence}"
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        while True:
            response = self._receive_wire()
            if response.get("id") != rid:
                self.events.append(response)
                continue
            if "error" in response:
                raise NodeError("hermes_rpc_" + str(response["error"].get("code", "error")))
            return response.get("result", {})
