"""Fixed Unix controller and guarded login. No shell or Docker API in the Web."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from websockets.sync.client import connect

from cf_agent_gateway.admin.auth import enforce_admin_request_body_limit, require_admin_role
from cf_agent_gateway.management_console.live import EmptyInput, observation
from cf_agent_gateway.management_console.models import DirectorySource
from cf_agent_gateway.management_console.service import ConsoleService, iso, now
from cf_agent_gateway.profile_b.service import ManagementError


class FixedController:
    def __init__(self, path):
        self.path = path

    def request(self, action):
        if action not in {"status", "wechat/start", "wechat/app/start"}:
            raise ValueError("controller_action_not_allowed")
        if os.name == "nt":
            return {"state": "unavailable", "error": "management_channel_requires_linux"}
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(self.path)),
                base_url="http://controller",
                timeout=10,
                trust_env=False,
            ) as client:
                response = client.request(
                    "GET" if action == "status" else "POST",
                    "/" + action,
                    json=None if action == "status" else {},
                )
                if response.status_code != 200:
                    return {"state": "unavailable", "error": "controller_operation_failed"}
                value = response.json()
                return (
                    value
                    if isinstance(value, dict)
                    else {"state": "unavailable", "error": "controller_contract_invalid"}
                )
        except (httpx.HTTPError, ValueError):
            return {"state": "unavailable", "error": "management_channel_unavailable"}


class ServiceOperations:
    def __init__(self, app, config, live):
        self.app, self.config, self.live = app, config, live
        path = config.runtime / "operations.json"
        self.settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        socket = self.settings.get("controller_socket")
        self.controller = FixedController(socket) if socket else None
        self.operations = {}
        self.lock = threading.RLock()
        self.cached = {"state": "unknown", "reason": "not_checked"}
        self.checked = None

    def probe(self, force=False):
        if not force and self.checked and now() < self.checked + timedelta(seconds=8):
            return
        self.cached = (
            self.controller.request("status")
            if self.controller
            else {"state": "unavailable", "error": "management_channel_not_configured"}
        )
        self.checked = now()

    def ensure_archive_owner(self, source):
        self.probe(force=True)
        expected = self.settings.get("sources", {}).get(source, {}).get("archive_owner")
        if expected != "candidate_archive_only" or self.cached.get("state") != "available":
            raise ManagementError("archive_owner_not_verified", 409)
        poll = self.cached.get("workers", {}).get("poll")
        if poll not in {"exited", "created", "dead"}:
            raise ManagementError(
                "production_collector_running"
                if poll == "running"
                else "archive_owner_not_verified",
                409,
            )

    def login_guard_available(self, source):
        bot = self.live.status(source)["bot"]
        return (
            self.settings.get("sources", {}).get(source, {}).get("login_contract")
            == "expected-account-v1"
            and bot.get("login_identity_guard_verified") is True
            and bot.get("stale") is False
        )

    def status(self, source):
        setting = self.settings.get("sources", {}).get(source, {})
        with self.lock:
            operation = self.operations.get(source)
            public = {
                key: value
                for key, value in (operation or {}).items()
                if key not in {"qr", "thread", "deadline"}
            }
            if (
                operation
                and operation.get("qr")
                and operation.get("deadline", 0) > time.monotonic()
            ):
                public["qr_available"] = True
            return {
                "controller": observation(self.cached, self.checked),
                "login": public or {"state": "not_started"},
                "capabilities": {
                    "start_service": bool(
                        self.controller and setting.get("controls_wechat_service")
                    ),
                    "start_app": bool(
                        self.controller
                        and setting.get("controls_wechat_service")
                        and self.cached.get("app_start_supported")
                    ),
                    "login": self.login_guard_available(source),
                    "reason": None
                    if self.login_guard_available(source)
                    else "target_login_identity_guard_not_verified",
                },
            }

    def start(self, source, action):
        with self.lock:
            if not self.controller:
                raise HTTPException(503, "management_channel_unavailable")
            if not self.settings.get("sources", {}).get(source, {}).get("controls_wechat_service"):
                raise HTTPException(409, "source_controller_not_bound")
            self.live.probe(source, force=True)
            if self.live.status(source)["bot"]["state"] == "online":
                return {"state": "already_online_preserved", "changed": False}
            result = self.controller.request(
                "wechat/start" if action == "service" else "wechat/app/start"
            )
            self.checked = None
            self.probe()
            return result

    def login(self, source):
        with self.lock:
            self.live.probe(source, force=True)
            state = self.live.status(source)["bot"]
            if state["state"] == "online":
                return {"state": "already_online_preserved", "changed": False}
            if state["state"] != "offline" or state.get("reason") == "app_not_running":
                raise HTTPException(409, "wechat_start_or_check_required")
            with self.app.state.database_session_factory() as session:
                row = session.get(DirectorySource, source)
                account = row.account_id if row else None
            if not account:
                raise HTTPException(409, "expected_bot_account_required")
            if not self.login_guard_available(source):
                raise HTTPException(409, "target_login_identity_guard_not_verified")
            existing = self.operations.get(source)
            if (
                existing
                and existing.get("deadline", 0) > time.monotonic()
                and existing["state"]
                in {"connecting", "waiting_scan", "phone_confirm", "verifying"}
            ):
                return self.status(source)["login"]
            operation = {
                "id": str(uuid4()),
                "state": "connecting",
                "started_at": iso(now()),
                "expires_at": iso(now() + timedelta(seconds=180)),
                "deadline": time.monotonic() + 180,
                "qr": None,
            }
            self.operations[source] = operation
            thread = threading.Thread(
                target=self.run_login,
                args=(source, account, operation),
                daemon=True,
                name="guarded-wechat-login",
            )
            operation["thread"] = thread
            thread.start()
            return self.status(source)["login"]

    def run_login(self, source, account, operation):
        connection = self.config.sources[source]
        url = urlsplit(connection.url)
        query = urlencode({"timeoutMs": 180000, "newAccount": "false", "expectedAccount": account})
        address = urlunsplit(
            (
                "wss" if url.scheme == "https" else "ws",
                url.netloc,
                url.path.rstrip("/") + "/api/ws/login",
                query,
                "",
            )
        )
        try:
            with connect(
                address,
                additional_headers={
                    "Authorization": "Bearer " + connection.token,
                    "X-Session-Id": "default",
                },
                open_timeout=8,
                close_timeout=2,
                max_size=2 * 1024 * 1024,
                proxy=None,
            ) as socket:
                while (
                    time.monotonic() < operation["deadline"] and not self.live.stop_event.is_set()
                ):
                    try:
                        value = json.loads(
                            socket.recv(
                                timeout=min(3, max(0.1, operation["deadline"] - time.monotonic()))
                            )
                        )
                    except TimeoutError:
                        continue
                    event = value.get("type") or value.get("event")
                    if event == "qr":
                        qr = value.get("qrDataUrl")
                        if (
                            isinstance(qr, str)
                            and qr.startswith("data:image/png;base64,")
                            and len(qr) < 1500000
                        ):
                            operation.update(state="waiting_scan", qr=qr)
                    elif event == "phone_confirm":
                        operation.update(state="phone_confirm", qr=None)
                    elif event == "login_success":
                        operation.update(state="verifying", qr=None)
                        # A terminal string cannot authorize another account.
                        self.live.probe(source, force=True)
                        reader = self.config.reader(source)
                        try:
                            actual = reader.account()
                            reader.read("api/contacts", limit=1, offset=0)
                            if actual != account:
                                operation.update(state="failed", error="source_account_mismatch")
                            else:
                                operation.update(state="online", verified_at=iso(now()))
                        finally:
                            reader.close()
                        return
                    elif event in {"error", "login_timeout"}:
                        operation.update(
                            state="expired" if event == "login_timeout" else "failed",
                            error="login_timeout"
                            if event == "login_timeout"
                            else "guarded_login_failed",
                            qr=None,
                        )
                        return
                operation.update(state="expired", qr=None, error="login_timeout")
        except Exception:
            operation.update(state="failed", qr=None, error="login_channel_failed")


def install_service_routes(app, config, live):
    operations = ServiceOperations(app, config, live)
    live.control = operations
    router = APIRouter(
        prefix="/admin/console/services",
        dependencies=[Depends(require_admin_role), Depends(enforce_admin_request_body_limit)],
    )

    def valid(source):
        if source not in config.sources:
            raise HTTPException(404, "source_not_registered")

    @router.get("/{source}")
    def status(source: str):
        valid(source)
        return operations.status(source)

    @router.post("/{source}/{action}")
    def action(source: str, action: str, request: Request, body: EmptyInput):
        valid(source)
        if action not in {"start-service", "start-app", "login"}:
            raise HTTPException(404, "operation_not_allowed")
        try:
            result = (
                operations.login(source)
                if action == "login"
                else operations.start(source, "service" if action == "start-service" else "app")
            )
        except (HTTPException, ManagementError) as error:
            result = {
                "state": "rejected",
                "error": error.code if isinstance(error, ManagementError) else error.detail,
            }
            audit(source, action, request, result)
            raise
        audit(source, action, request, result)
        return result

    def audit(source, action, request, result):
        with app.state.database_session_factory() as session:
            ConsoleService(
                session, config, actor=getattr(request.state, "admin_subject", "admin")
            ).profiles().audit(
                "controlled." + action,
                source,
                result.get("error") or result.get("state", "unknown"),
            )
            session.commit()

    @router.get("/{source}/login/qr")
    def qr(source: str):
        valid(source)
        operation = operations.operations.get(source)
        if (
            not operation
            or operation.get("deadline", 0) <= time.monotonic()
            or not operation.get("qr")
        ):
            raise HTTPException(404, "qr_unavailable_or_expired")
        return {"data_url": operation["qr"], "expires_at": operation["expires_at"]}

    app.include_router(router)
    return operations
