"""One candidate archive-only owner; durable, account-scoped incremental journal.

Uses MessageStore/normalizer, never ingestion/Admission/Dispatch/Delivery. The
production archive stays read-only; this candidate owns only its new live suffix.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from contextlib import suppress
from datetime import UTC, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from cf_agent_gateway.adapters.wechat.message_event import wechat_message_to_event
from cf_agent_gateway.adapters.wechat.normalizer import normalize_wechat_message
from cf_agent_gateway.admin.auth import enforce_admin_request_body_limit, require_admin_role
from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.management_console.live_models import (
    ArchiveControl,
    ArchiveObservation,
    LiveObservation,
)
from cf_agent_gateway.management_console.models import DirectorySource
from cf_agent_gateway.management_console.service import ConsoleService, iso, now
from cf_agent_gateway.management_console.upstream import SourceError
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.profile_b.service import ManagementError
from cf_agent_gateway.task.model.models import HermesDispatchRecord

STATUS_TTL = 20
WINDOW_LIMIT = 200
OFFLINE = {"logged_out", "app_not_running", "session_expired", "qr_pending", "phone_confirm"}


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def stamp(value):
    return value.replace(tzinfo=value.tzinfo or UTC)


def observation(value=None, observed_at=None, at=None):
    at = at or now()
    expired = not observed_at or at > stamp(observed_at) + timedelta(seconds=STATUS_TTL)
    result = {**(value or {}), "observed_at": iso(observed_at), "ttl_seconds": STATUS_TTL}
    result["expires_at"] = (
        iso(stamp(observed_at) + timedelta(seconds=STATUS_TTL)) if observed_at else None
    )
    if expired:
        result.update(state="unknown", reason="check_expired" if observed_at else "not_checked")
    result["stale"] = expired
    return result


def cursor_for(source, account, value):
    scope = hashlib.sha256((source + "\0" + account).encode()).hexdigest()[:24]
    return base64.urlsafe_b64encode(json.dumps([scope, value]).encode()).decode().rstrip("=")


def cursor_value(cursor, source, account):
    try:
        scope, value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if cursor_for(source, account, value) != cursor or type(value) is not int or value < 0:
            raise ValueError
        return value
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ManagementError("increment_cursor_scope_invalid", 422) from None


class LiveRuntime:
    def __init__(self, config, factory):
        self.config, self.factory = config, factory
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        self.owner_file = None
        self.last_auth = {}
        self.control = None
        self.login_operations = {}
        self.system_checked = 0
        self.system_cached = {"state": "unknown", "reason": "not_checked"}

    def start(self):
        # A second uvicorn/process cannot become a competing collector.
        # Held for the runtime lifetime; stop() closes only after its worker exits.
        self.owner_file = open(self.config.runtime / "archive-owner.lock", "a+b")  # noqa: SIM115
        try:
            if __import__("os").name == "nt":
                import msvcrt

                self.owner_file.seek(0)
                self.owner_file.write(b"0")
                self.owner_file.flush()
                self.owner_file.seek(0)
                msvcrt.locking(self.owner_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_file.close()
            self.owner_file = None
            return False
        self.thread = threading.Thread(target=self.run, name="candidate-archive-only", daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(30)
        if self.owner_file and not (self.thread and self.thread.is_alive()):
            self.owner_file.close()
            self.owner_file = None

    def run(self):
        while not self.stop_event.is_set():
            for source in self.config.sources:
                try:
                    self.tick(source)
                except Exception:
                    # Never log source errors/URLs/message bodies/QRs or secrets.
                    with suppress(Exception), self.factory() as session:
                        row = session.get(ArchiveControl, source)
                        if row:
                            row.last_error, row.last_check_at = "collector_internal_error", now()
                            session.commit()
            if self.control:
                with suppress(Exception):
                    self.control.probe()
            with suppress(Exception):
                self.probe_system()
            self.stop_event.wait(2)

    def probe_system(self):
        if time.monotonic() - self.system_checked < 15:
            return
        sources = {}
        for source in self.config.sources:
            try:
                with self.factory() as candidate:
                    saved = candidate.get(DirectorySource, source)
                    account = saved.account_id if saved else None
                    with self.config.archive(source, candidate) as archive:
                        if not archive or not account:
                            sources[source] = {
                                "state": "unknown",
                                "reason": "archive_not_configured",
                            }
                            continue
                        dispatch = archive.execute(
                            select(HermesDispatchRecord.status, func.count())
                            .join(Message, Message.id == HermesDispatchRecord.message_id)
                            .where(Message.source_account_id == account)
                            .group_by(HermesDispatchRecord.status)
                        ).all()
                        delivery = archive.execute(
                            select(DeliveryOutboxRecord.status, func.count())
                            .where(DeliveryOutboxRecord.account_id == account)
                            .group_by(DeliveryOutboxRecord.status)
                        ).all()
                        sources[source] = {
                            "state": "available",
                            "source": "read_only_message_archive",
                            "dispatch": {str(k): n for k, n in dispatch},
                            "delivery": {str(k): n for k, n in delivery},
                        }
            except Exception:
                sources[source] = {"state": "unknown", "reason": "archive_statistics_check_failed"}
        self.system_cached = {
            "state": "available",
            "observed_at": iso(now()),
            "ttl_seconds": 30,
            "sources": sources,
        }
        self.system_checked = time.monotonic()

    def probe(self, source, force=False):
        previous = self.last_auth.get(source, 0)
        if not force and time.monotonic() - previous < 5:
            return
        reader = self.config.reader(source)
        observed = now()
        try:
            value = reader.read("api/status/auth")
            raw = value.get("status") if isinstance(value, dict) else None
            account = value.get("loggedInUser") if isinstance(value, dict) else None
            with self.factory() as session:
                saved = session.get(DirectorySource, source)
                expected = saved.account_id if saved else None
            valid_account = isinstance(account, str) and 0 < len(account) <= 255
            state = (
                "online"
                if raw == "logged_in" and valid_account
                else "offline"
                if raw in OFFLINE
                else "unknown"
            )
            reason = (
                raw
                if isinstance(raw, str) and raw in OFFLINE | {"logged_in", "unknown"}
                else "source_status_unrecognized"
            )
            if expected and account and expected != account:
                state, reason = "unknown", "source_account_mismatch"
            result = {
                "state": state,
                "reason": reason,
                "source": "native_auth_fsm_process_check",
                "account_matches": valid_account and (not expected or expected == account),
            }
        except SourceError as error:
            result = {
                "state": "unknown",
                "reason": str(error),
                "source": "native_auth_fsm_process_check",
            }
        finally:
            reader.close()
        with self.factory() as session:
            row = session.get(LiveObservation, "wechat:" + source)
            if row is None:
                row = LiveObservation(key="wechat:" + source)
                session.add(row)
            row.value, row.observed_at = result, observed
            session.commit()
        self.last_auth[source] = time.monotonic()

    def status(self, source):
        with self.factory() as session:
            row = session.get(LiveObservation, "wechat:" + source)
            bot = observation(row.value if row else None, row.observed_at if row else None)
            control = session.get(ArchiveControl, source)
            collection = {
                "state": "disabled",
                "enabled": False,
                "mode": "archive_only",
                "dispatch": "unchanged_not_controlled",
                "delivery": "unchanged_not_controlled",
            }
            if control:
                stale = not control.last_check_at or now() > stamp(
                    control.last_check_at
                ) + timedelta(seconds=20)
                collection.update(
                    enabled=control.enabled,
                    state="disabled"
                    if not control.enabled
                    else "stale"
                    if stale
                    else "blocked"
                    if control.last_error
                    else "running",
                    last_check_at=iso(control.last_check_at),
                    last_success_at=iso(control.last_success_at),
                    last_message_at=iso(control.last_message_at),
                    last_error=control.last_error,
                    enabled_at=iso(control.enabled_at),
                    baselined_chats=len(control.baselined_chats or {}),
                )
            collection.update(
                data_source="candidate_same_message_schema",
                production_archive="read_only",
                coverage=f"启用时的可见窗口仅记录基线；此后采集近期非隐藏会话每群/私聊最新{WINDOW_LIMIT}条。停采、上游窗口和新发现旧会话可能有缺口；不回放历史。",
                owner="single_candidate_process",
            )
            return {"bot": bot, "collection": collection}

    def tick(self, source):
        with self.lock:
            self.probe(source)
            with self.factory() as session:
                control = session.get(ArchiveControl, source)
                if not control or not control.enabled:
                    return
                if self.config.mode != "demo":
                    try:
                        if not self.control:
                            raise ManagementError("archive_owner_not_verified", 409)
                        self.control.ensure_archive_owner(source)
                    except ManagementError as error:
                        control.last_check_at, control.last_error = now(), error.code
                        session.commit()
                        return
                if self.status(source)["bot"]["state"] != "online":
                    control.last_check_at, control.last_error = now(), "wechat_not_verified_online"
                    session.commit()
                    return
                reader = self.config.reader(source)
                try:
                    account = reader.account()
                    if account != control.account_id:
                        raise SourceError("source_account_mismatch")
                    chats, _ = reader.pages("api/chats", 50)
                    windows = []
                    for chat in chats:
                        chat_id = chat.get("username") or chat.get("id")
                        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 255:
                            raise SourceError("source_chat_identity_invalid")
                        rows = reader.messages(chat_id, WINDOW_LIMIT)
                        readable = now()
                        normalized = [
                            normalize_wechat_message(
                                row, source_account_id=account, conversation_name=chat.get("name")
                            )
                            for row in rows
                        ]
                        if any(row.conversation_id != chat_id for row in normalized):
                            raise SourceError("source_conversation_mismatch")
                        windows.append((chat, chat_id, normalized, readable))
                    if reader.account() != account:
                        raise SourceError("source_account_changed_during_collect")
                    initial = control.last_success_at is None
                    for chat, chat_id, rows, readable in windows:
                        self.persist_window(
                            session, control, chat, chat_id, rows, readable, initial
                        )
                    control.last_check_at = control.last_success_at = now()
                    control.last_error = None
                    session.commit()
                except (SourceError, ValueError, TypeError):
                    session.rollback()
                    control = session.get(ArchiveControl, source)
                    control.last_check_at, control.last_error = now(), "archive_source_check_failed"
                    session.commit()
                finally:
                    reader.close()

    def persist_window(self, session, control, chat, chat_id, rows, readable, initial=False):
        svc = ConsoleService(session, self.config)
        source = svc.source(control.source_id)
        entry = svc.find_entry(source.id, control.account_id, chat_id)
        if entry is None:
            entry = svc.upsert(
                source,
                {
                    "username": chat_id,
                    "nick_name": chat.get("name"),
                    "contact_type": "chatroom" if chat_id.endswith("@chatroom") else "individual",
                },
                "chats",
            )
        known_chat = chat_id in (control.baselined_chats or {})
        initial_baseline = initial
        for normalized in rows:
            existing = session.scalar(
                select(ArchiveObservation).where(
                    ArchiveObservation.source_id == source.id,
                    ArchiveObservation.account_id == control.account_id,
                    ArchiveObservation.event_id == normalized.event_id,
                )
            )
            if existing:
                continue
            baseline = initial_baseline or (
                not known_chat and stamp(normalized.timestamp) < stamp(control.enabled_at)
            )
            if not baseline and self.config.mode != "demo":
                with self.config.archive(source.id, session) as history:
                    if history and history.scalar(
                        select(Message.id).where(Message.event_id == normalized.event_id).limit(1)
                    ):
                        baseline = True
            message = None
            if not baseline:
                event = wechat_message_to_event(normalized)
                if self.config.mode == "demo":
                    event = event.model_copy(update={"source": "wechat-demo"})
                # Persist only; this deliberately has no ingestion service dependency.
                message, _ = MessageStore(session).create(event)
                control = session.get(ArchiveControl, source.id)
            session.add(
                ArchiveObservation(
                    source_id=source.id,
                    account_id=control.account_id,
                    event_id=normalized.event_id,
                    conversation_id=chat_id,
                    message_id=message.id if message else None,
                    upstream_readable_at=readable,
                    archived_at=now() if message else None,
                )
            )
            if message:
                control.last_message_at = now()
                if entry.last_at is None or stamp(normalized.timestamp) >= stamp(entry.last_at):
                    entry.last_at, entry.last_message = (
                        normalized.timestamp,
                        normalized.content[:500],
                    )
            # Store and its committed journal are separate on purpose: notification
            # can never precede persistence. A crash rediscovers the same event;
            # MessageStore returns it idempotently and repairs the missing journal.
            session.commit()
        control.baselined_chats = {**(control.baselined_chats or {}), chat_id: True}
        session.commit()

    def changes(self, source, after, limit):
        with self.factory() as session:
            saved = ConsoleService(session, self.config).source(source)
            account = saved.account_id or ""
            base = [
                ArchiveObservation.source_id == source,
                ArchiveObservation.account_id == account,
            ]
            high = session.scalar(select(func.max(ArchiveObservation.id)).where(*base)) or 0
            position = high if not after else cursor_value(after, source, account)
            if position > high:
                raise ManagementError("increment_cursor_ahead", 409)
            rows = list(
                session.scalars(
                    select(ArchiveObservation)
                    .where(*base, ArchiveObservation.id > position)
                    .order_by(ArchiveObservation.id)
                    .limit(limit)
                )
            )
            svc = ConsoleService(session, self.config)
            changes = []
            for row in rows:
                if row.message_id is None:
                    continue
                entry = svc.find_entry(source, account, row.conversation_id)
                if entry:
                    changes.append(
                        {
                            "sequence": row.id,
                            "conversation_id": "d:" + entry.id,
                            "entry": svc.entry_view(entry),
                            "message": {
                                **svc.message_view(session, entry, "message", str(row.message_id)),
                                "id": ("m:" if self.config.mode == "demo" else "live:")
                                + str(row.message_id),
                            },
                            "upstream_readable_at": iso(row.upstream_readable_at),
                            "archived_at": iso(row.archived_at),
                        }
                    )
            return {
                "source": source,
                "cursor": cursor_for(source, account, rows[-1].id if rows else position),
                "items": changes,
                "has_more": bool(rows and rows[-1].id < high),
                **self.status(source),
            }


def install_live_routes(app, config):
    runtime = LiveRuntime(config, lambda: app.state.database_session_factory())
    app.state.console_live = runtime
    router = APIRouter(
        prefix="/admin/console/live",
        dependencies=[Depends(require_admin_role), Depends(enforce_admin_request_body_limit)],
    )

    def valid(source):
        if source not in config.sources:
            raise HTTPException(404, "source_not_registered")

    @router.get("/{source}")
    def changes(
        source: str, after: str = Query("", max_length=256), limit: int = Query(100, ge=1, le=200)
    ):
        valid(source)
        return runtime.changes(source, after, limit)

    @router.post("/{source}/check")
    def check(source: str):
        valid(source)
        runtime.probe(source, force=True)
        return runtime.status(source)

    @router.post("/{source}/collection/{action}")
    def collection(
        source: str,
        action: str,
        body: EmptyInput,
        request: Request,
        session: Annotated[Session, Depends(get_database_session)],
    ):
        valid(source)
        if action not in {"start", "stop"}:
            raise HTTPException(404, "operation_not_allowed")
        with runtime.lock:
            svc = ConsoleService(
                session, config, actor=getattr(request.state, "admin_subject", "admin")
            )
            saved = svc.source(source)
            if not saved.account_id:
                raise ManagementError("sync_source_before_collection", 409)
            if action == "start" and config.mode != "demo":
                if not runtime.control:
                    raise ManagementError("archive_owner_not_verified", 409)
                runtime.control.ensure_archive_owner(source)
            row = session.get(ArchiveControl, source)
            if row and row.account_id != saved.account_id:
                raise ManagementError("source_account_mismatch", 409)
            if row is None:
                row = ArchiveControl(
                    source_id=source,
                    account_id=saved.account_id,
                    enabled_at=now(),
                    enabled=False,
                    baselined_chats={},
                )
                session.add(row)
            row.enabled = action == "start"
            svc.profiles().audit("archive_only." + action, source, "saved_no_ai")
            session.commit()
        return runtime.status(source)

    @router.get("/system/snapshot")
    def system():
        with app.state.database_session_factory() as session:
            session.execute(text("SELECT 1"))
        return {
            "observed_at": iso(now()),
            "ttl_seconds": 20,
            "gateway": {
                "management_api": "running",
                "candidate_database": "connected",
                "revision": __import__("os").environ.get("CANDIDATE_REVISION", "development"),
                "admission": "not_started_by_candidate",
                "dispatch": "not_started_by_candidate",
                "delivery": "not_started_by_candidate",
                "production_permissions": "not_verified_not_migrated",
            },
            "archive_tasks": runtime.system_cached,
            "sources": {key: runtime.status(key) for key in config.sources},
        }

    app.include_router(router)
    return runtime
