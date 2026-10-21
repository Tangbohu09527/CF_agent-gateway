from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import String, and_, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.exc import SQLAlchemyError

from cf_agent_gateway.access.policy_models import UserAccessPolicy
from cf_agent_gateway.artifact.models import Artifact
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryReceipt
from cf_agent_gateway.identity.models import EnterpriseIdentity, SourceIdentityMapping
from cf_agent_gateway.management_console.models import DirectoryEntry, DirectorySource
from cf_agent_gateway.management_console.upstream import SourceError
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.profile_b.models import DerivedProfile, EmployeeSettings, RoomSettings
from cf_agent_gateway.profile_b.schemas import EmployeeInput, EmployeeUpdate, RoomInput, RoomUpdate
from cf_agent_gateway.profile_b.service import ManagementError, Service
from cf_agent_gateway.response.models import ResponsePartRecord, ResponseRecord

ARCHIVE_COVERAGE = (
    "仅已持久化档案；历史停采、self跳过、上游窗口及未归档消息可能存在缺口。"
    "阅读不清微信未读、不下载原件、不触发AI。"
)
GROUP_COVERAGE = "已发现群（非隐藏近期会话及已归档群），不是微信加入的全部群；成员资料未提供。"


def now():
    return datetime.now(UTC)


def iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat()


def time_value(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def page_result(items, total, offset, limit, **extra):
    return {"items": items, "total": total, "offset": offset, "limit": limit, **extra}


class ConsoleService:
    def __init__(self, session, config, *, actor="console-admin"):
        self.s, self.config, self.actor = session, config, actor
        self.platform = "wechat-demo" if config.mode == "demo" else "wechat"

    def source(self, key):
        if key not in self.config.sources:
            raise ManagementError("source_not_registered", 404)
        source = self.s.get(DirectorySource, key)
        if source is None:
            raise ManagementError("source_not_initialized", 503)
        return source

    def profiles(self):
        accounts = frozenset(self.s.scalars(select(DirectorySource.account_id))) - {None}
        return Service(
            self.s, self.config.hermes_connections, self.actor, accounts, source=self.platform
        )

    def source_view(self, source):
        return {
            "id": source.id,
            "label": source.label,
            "account_id": source.account_id,
            "bot_name": source.bot_name,
            "avatar_url": None,
            "status": source.status,
            "bot_status": source.sync_details.get("bot_status", "unknown"),
            "last_sync_at": iso(source.last_sync_at),
            "last_error": source.last_error,
            "coverage": source.coverage,
            "sync_details": source.sync_details,
            "capabilities": {
                "contacts": True,
                "sync": True,
                "all_groups": False,
                "members": False,
                "send": False,
                "clear_unread": False,
            },
        }

    def sync(self, key):
        source = self.source(key)
        reader = self.config.reader(key)
        try:
            account, contacts, chats, proof = reader.snapshot()
            source.account_id = account
            for raw in contacts:
                self.upsert(source, raw, "contacts")
            for raw in chats:
                username = raw.get("username") or raw.get("id")
                existing = self.find_entry(source.id, account, username)
                values = {
                    "username": username,
                    "nick_name": raw.get("name", username),
                    "remark": raw.get("remark"),
                    "contact_type": "chatroom"
                    if raw.get("isGroup", raw.get("is_group"))
                    or str(username).endswith("@chatroom")
                    else "individual",
                }
                entry = existing or self.upsert(source, values, "chats")
                if existing and entry.discovery == "contacts":
                    entry.discovery = "both"
                entry.last_message = raw.get("lastMessagePreview", raw.get("last_message_preview"))
                entry.last_at = time_value(raw.get("lastActivityAt", raw.get("last_activity_at")))
                entry.unread_count = raw.get("unreadCount", raw.get("unread_count"))
            source.bot_name = next(
                (r.get("nick_name") for r in contacts if r.get("username") == account), account
            )
            offline_cache = proof.get("bot_status") == "app_not_running"
            source.status = (
                "offline_cached" if offline_cache else "synced" if contacts else "empty_unverified"
            )
            source.last_sync_at, source.last_error = now(), None
            source.coverage = (
                (
                    "微信进程离线，以下为已有资料库中的缓存资料；不表示当前在线。"
                    if offline_cache
                    else ""
                )
                + "按offset扫描至空页；短页仍继续。上游空数组也可能表示资料库未就绪，"
                "不能证明全部好友完整。" + GROUP_COVERAGE
            )
            source.sync_details = {
                "pagination": proof,
                "bot_status": proof.get("bot_status", "unknown"),
                "contact_rows": len(contacts),
                "recent_chat_rows": len(chats),
                "missing_rows_retained": True,
            }
            self.sync_archive_metadata(source)
            self.profiles().audit("directory.sync", key, "ok")
            self.s.commit()
        except (SourceError, SQLAlchemyError, TypeError, ValueError) as error:
            self.s.rollback()
            source = self.source(key)
            code = str(error) if isinstance(error, SourceError) else "source_data_invalid"
            source.status = "offline" if code == "bot_offline" else "error"
            source.last_error = code
            self.profiles().audit("directory.sync", key, code)
            self.s.commit()
            raise ManagementError(code, 503) from None
        finally:
            reader.close()
        return self.source_view(source)

    def find_entry(self, source, account, username):
        return self.s.scalar(
            select(DirectoryEntry).where(
                DirectoryEntry.source_id == source,
                DirectoryEntry.account_id == account,
                DirectoryEntry.username == username,
            )
        )

    def upsert(self, source, raw, discovery):
        username = raw.get("username")
        if not isinstance(username, str) or not username or len(username) > 255:
            raise SourceError("source_contact_identity_invalid")
        entry = self.find_entry(source.id, source.account_id, username)
        if entry is None:
            entry = DirectoryEntry(
                id=str(uuid4()),
                source_id=source.id,
                account_id=source.account_id,
                username=username,
            )
            self.s.add(entry)
        kind = raw.get("contact_type", "individual")
        if kind not in {"individual", "official", "openim", "system", "chatroom"}:
            kind = "unknown"
        entry.nick_name = str(raw.get("nick_name") or "")[:512]
        entry.remark = raw.get("remark")
        entry.alias = raw.get("alias")
        entry.small_head_url = raw.get("small_head_url")
        entry.contact_type, entry.discovery, entry.updated_at = kind, discovery, now()
        self.s.flush()
        return entry

    def sync_archive_metadata(self, source):
        with self.config.archive(source.id, self.s) as archive:
            if archive is None:
                source.sync_details = {**source.sync_details, "archive": "not_configured"}
                return
            conversations = archive.scalars(
                select(Conversation).where(
                    Conversation.source == self.platform,
                    Conversation.source_account_id == source.account_id,
                )
            )
            for conv in conversations:
                entry = self.find_entry(source.id, source.account_id, conv.conversation_id)
                if entry is None:
                    entry = self.upsert(
                        source,
                        {
                            "username": conv.conversation_id,
                            "nick_name": conv.conversation_name or "",
                            "contact_type": "chatroom"
                            if conv.conversation_type == "group"
                            else "individual",
                        },
                        "archive",
                    )
                last = archive.scalar(
                    select(Message)
                    .where(
                        Message.source == conv.source,
                        Message.source_account_id == conv.source_account_id,
                        Message.conversation_id == conv.conversation_id,
                    )
                    .order_by(Message.occurred_at.desc(), Message.id.desc())
                    .limit(1)
                )
                if last and (
                    entry.last_at is None
                    or last.occurred_at.replace(tzinfo=UTC) > entry.last_at.replace(tzinfo=UTC)
                ):
                    entry.last_at, entry.last_message = last.occurred_at, last.content[:500]
            source.sync_details = {**source.sync_details, "archive": "configured_read_only"}

    def entry(self, key):
        entry = self.s.get(DirectoryEntry, key.removeprefix("d:"))
        if entry is None:
            raise ManagementError("contact_not_found", 404)
        source = self.source(entry.source_id)
        if source.account_id != entry.account_id:
            raise ManagementError("source_account_binding_changed", 409)
        return entry

    def employee(self, entry):
        mapping = self.s.scalar(
            select(SourceIdentityMapping).where(
                SourceIdentityMapping.platform == self.platform,
                SourceIdentityMapping.account_id == entry.account_id,
                SourceIdentityMapping.sender_id == entry.username,
            )
        )
        if not mapping:
            return None
        identity = self.s.get(EnterpriseIdentity, mapping.enterprise_identity_id)
        settings = self.s.get(EmployeeSettings, identity.id)
        policy = self.s.scalar(
            select(UserAccessPolicy).where(UserAccessPolicy.enterprise_identity_id == identity.id)
        )
        return {
            "id": identity.id,
            "employee_id": identity.employee_id,
            "name": identity.display_name,
            "enabled": identity.status.value == "active",
            "bot_allowed": bool(policy and policy.enabled),
            "group_allowed": bool(settings and settings.group_allowed),
            "group_id": settings.group_id if settings else None,
        }

    def entry_view(self, entry):
        employee = self.employee(entry) if entry.contact_type != "chatroom" else None
        conv = self.s.scalar(
            select(Conversation).where(
                Conversation.source == self.platform,
                Conversation.source_account_id == entry.account_id,
                Conversation.conversation_id == entry.username,
            )
        )
        room = self.s.get(RoomSettings, conv.id) if conv else None
        derived = self.s.scalar(
            select(DerivedProfile).where(
                DerivedProfile.identity_id == employee["id"]
                if employee
                else DerivedProfile.conversation_id == (conv.id if room else -1)
            )
        )
        return {
            "id": entry.id,
            "username": entry.username,
            "account_id": entry.account_id,
            "nick_name": entry.nick_name,
            "remark": entry.remark,
            "alias": entry.alias,
            "avatar_url": f"/admin/console/contacts/{entry.id}/avatar"
            if entry.small_head_url or self.config.mode == "demo"
            else None,
            "contact_type": entry.contact_type,
            "display_name": entry.remark or entry.nick_name or entry.username,
            "updated_at": iso(entry.updated_at),
            "employee": employee,
            "room": {
                "id": str(conv.id),
                "name": conv.conversation_name,
                "group_id": room.group_id,
                "enabled": room.enabled,
            }
            if room
            else None,
            "derived": {
                "id": derived.id,
                "actual_profile": derived.actual_profile,
                "status": derived.status,
                "applied_version": derived.applied_version,
                "desired_version": derived.desired_version,
            }
            if derived
            else None,
            "conversation_id": "d:" + entry.id,
            "last_at": iso(entry.last_at),
            "last_message": entry.last_message,
            "discovery": entry.discovery,
            "members": {"supported": False, "reason": "上游未提供群成员接口", "items": []},
        }

    def entries(
        self,
        source_id,
        *,
        q="",
        kind="individual",
        linked="all",
        authorized="all",
        group_id=None,
        offset=0,
        limit=30,
    ):
        source = self.source(source_id)
        query = select(DirectoryEntry).where(
            DirectoryEntry.source_id == source.id, DirectoryEntry.account_id == source.account_id
        )
        if kind != "all":
            query = query.where(DirectoryEntry.contact_type == kind)
        if kind != "chatroom":
            query = query.where(DirectoryEntry.discovery.in_(["contacts", "both"]))
        if q:
            query = query.where(
                or_(
                    *(
                        func.lower(column).contains(q.lower(), autoescape=True)
                        for column in (
                            DirectoryEntry.username,
                            DirectoryEntry.nick_name,
                            DirectoryEntry.remark,
                            DirectoryEntry.alias,
                        )
                    )
                )
            )
        mapping_filter = and_(
            SourceIdentityMapping.platform == self.platform,
            SourceIdentityMapping.account_id == DirectoryEntry.account_id,
            SourceIdentityMapping.sender_id == DirectoryEntry.username,
        )
        if linked != "all":
            bound = exists(select(SourceIdentityMapping.id).where(mapping_filter))
            query = query.where(bound if linked == "yes" else ~bound)
        if authorized != "all":
            allowed = exists(
                select(SourceIdentityMapping.id)
                .join(
                    UserAccessPolicy,
                    UserAccessPolicy.enterprise_identity_id
                    == SourceIdentityMapping.enterprise_identity_id,
                )
                .where(mapping_filter, UserAccessPolicy.enabled.is_(True))
            )
            query = query.where(allowed if authorized == "yes" else ~allowed)
        if group_id:
            query = query.where(
                exists(
                    select(SourceIdentityMapping.id)
                    .join(
                        EmployeeSettings,
                        EmployeeSettings.identity_id
                        == SourceIdentityMapping.enterprise_identity_id,
                    )
                    .where(mapping_filter, EmployeeSettings.group_id == group_id)
                )
            )
        total = self.s.scalar(select(func.count()).select_from(query.subquery()))
        items = self.s.scalars(
            query.order_by(DirectoryEntry.remark, DirectoryEntry.nick_name, DirectoryEntry.id)
            .offset(offset)
            .limit(limit)
        )
        return page_result(
            [self.entry_view(e) for e in items], total, offset, limit, sync=self.source_view(source)
        )

    def enroll(self, key, body):
        entry = self.entry(key)
        if entry.contact_type not in {"individual", "openim"}:
            raise ManagementError("contact_kind_not_employee", 422)
        service = self.profiles()
        employee = self.employee(entry)
        selected = body.existing_identity_id
        if employee and selected and employee["id"] != selected:
            raise ManagementError("source_identity_already_bound")
        if not employee and selected:
            service.get(EnterpriseIdentity, selected)
            self.s.add(
                SourceIdentityMapping(
                    id=str(uuid4()),
                    platform=self.platform,
                    account_id=entry.account_id,
                    sender_id=entry.username,
                    enterprise_identity_id=selected,
                    enabled=True,
                )
            )
            if not self.s.get(EmployeeSettings, selected):
                self.s.add(EmployeeSettings(identity_id=selected))
            service.flush()
            employee = {"id": selected}
        values = {
            field: getattr(body, field)
            for field in ("name", "enabled", "bot_allowed", "group_allowed", "group_id")
        }
        if employee:
            identity_id = service.save_employee(EmployeeUpdate(**values), employee["id"])
        else:
            identity_id = service.save_employee(
                EmployeeInput(
                    **values,
                    employee_id=body.employee_id or "EMP-" + uuid4().hex[:12],
                    account_id=entry.account_id,
                    sender_id=entry.username,
                )
            )
        return {"id": identity_id, "contact": self.entry_view(entry), "candidate_only": True}

    def set_room(self, key, body):
        entry = self.entry(key)
        if entry.contact_type != "chatroom":
            raise ManagementError("contact_is_not_group", 422)
        view = self.entry_view(entry)
        values = {"name": view["display_name"], "enabled": body.enabled, "group_id": body.group_id}
        if view["room"]:
            key = self.profiles().save_room(RoomUpdate(**values), view["room"]["id"])
        else:
            key = self.profiles().save_room(
                RoomInput(**values, account_id=entry.account_id, conversation_id=entry.username)
            )
        return {"id": key, "contact": self.entry_view(entry), "candidate_only": True}

    def conversations(self, source_id, q="", offset=0, limit=30):
        source = self.source(source_id)
        query = select(DirectoryEntry).where(
            DirectoryEntry.source_id == source.id,
            DirectoryEntry.account_id == source.account_id,
            or_(
                DirectoryEntry.last_at.is_not(None),
                DirectoryEntry.discovery.in_(["chats", "both", "archive"]),
            ),
        )
        if q:
            query = query.where(
                or_(
                    *(
                        func.lower(column).contains(q.lower(), autoescape=True)
                        for column in (
                            DirectoryEntry.username,
                            DirectoryEntry.nick_name,
                            DirectoryEntry.remark,
                        )
                    )
                )
            )
        total = self.s.scalar(select(func.count()).select_from(query.subquery()))
        entries = self.s.scalars(
            query.order_by(DirectoryEntry.last_at.desc(), DirectoryEntry.id)
            .offset(offset)
            .limit(limit)
        )
        items = []
        for entry in entries:
            view = self.entry_view(entry)
            items.append(
                {
                    "id": "d:" + entry.id,
                    "contact_id": entry.id,
                    "display_name": view["display_name"],
                    "avatar_url": view["avatar_url"],
                    "kind": "group" if entry.contact_type == "chatroom" else "private",
                    "username": entry.username,
                    "last_message": entry.last_message,
                    "last_at": iso(entry.last_at),
                    "unread_count": entry.unread_count,
                    "unread_source": "微信近期会话快照（网页阅读不清零）",
                    "archive_status": "coverage_gaps_possible",
                }
            )
        return page_result(items, total, offset, limit, coverage=ARCHIVE_COVERAGE)

    def messages(
        self, key, source_id, *, q="", sender="", date_from=None, date_to=None, offset=0, limit=40
    ):
        values = dict(q=q, sender=sender, date_from=date_from, date_to=date_to)
        if self.config.mode == "demo":
            return self.archive_messages(key, source_id, offset=offset, limit=limit, **values)
        historical = self.archive_messages(key, source_id, offset=0, limit=offset + limit, **values)
        suffix = self.archive_messages(
            key,
            source_id,
            offset=0,
            limit=offset + limit,
            local=True,
            exclude_events=self.history_overlap(key, source_id),
            **values,
        )
        # The candidate archive owns only newly observed messages; formal history
        # remains a read-only partition. Preserve physical identity when reconciling.
        combined = {x.get("event_id") or x["id"]: x for x in historical["items"]}
        combined.update({x.get("event_id") or x["id"]: x for x in suffix["items"]})
        # Match each partition's stable at DESC / kind ASC / id ASC ordering,
        # including multiple distinct messages in the same second.
        items = sorted(
            combined.values(),
            key=lambda x: (
                x["id"].startswith("r:"),
                x["id"].split(":", 1)[-1],
                x["id"],
            ),
        )
        items.sort(key=lambda x: x["occurred_at"], reverse=True)
        senders = {
            x["id"]: x
            for x in historical.get("sender_options", []) + suffix.get("sender_options", [])
        }
        total = historical["total"] + suffix["total"]
        return {
            **historical,
            "items": items[offset : offset + limit],
            "total": total,
            "offset": offset,
            "limit": limit,
            "sender_options": list(senders.values()),
            "status": "ok" if total else historical["status"],
            "coverage": ARCHIVE_COVERAGE + " 候选仅归档增量与正式只读历史合并；不改变正式派发。",
        }

    def history_overlap(self, key, source_id):
        """Compare stable event identities, never message bodies or display names.

        The normal owner guard keeps partitions disjoint. If formal collection
        resumes later, count and paginate duplicates across the whole partition,
        rather than subtracting only the currently displayed page.
        """
        entry = self.entry(key)
        rows = self.s.scalars(
            select(Message.event_id).where(
                Message.source == self.platform,
                Message.source_account_id == entry.account_id,
                Message.conversation_id == entry.username,
            )
        ).yield_per(400)
        overlap = []
        with self.config.archive(source_id, self.s) as archive:
            if archive is None:
                return overlap
            batch = []
            for event in rows:
                batch.append(event)
                if len(batch) == 400:
                    overlap.extend(
                        archive.scalars(
                            select(Message.event_id).where(
                                Message.source == self.platform,
                                Message.source_account_id == entry.account_id,
                                Message.conversation_id == entry.username,
                                Message.event_id.in_(batch),
                            )
                        )
                    )
                    batch = []
            if batch:
                overlap.extend(
                    archive.scalars(
                        select(Message.event_id).where(
                            Message.source == self.platform,
                            Message.source_account_id == entry.account_id,
                            Message.conversation_id == entry.username,
                            Message.event_id.in_(batch),
                        )
                    )
                )
        return overlap

    def archive_messages(
        self,
        key,
        source_id,
        *,
        q="",
        sender="",
        date_from=None,
        date_to=None,
        offset=0,
        limit=40,
        local=False,
        exclude_events=(),
    ):
        entry = self.entry(key)
        if entry.source_id != source_id:
            raise ManagementError("source_conversation_binding_mismatch", 403)
        if self.source(source_id).last_sync_at is None:
            return page_result([], 0, offset, limit, coverage=ARCHIVE_COVERAGE, status="not_synced")
        context = nullcontext(self.s) if local else self.config.archive(source_id, self.s)
        with context as archive:
            if archive is None:
                return page_result(
                    [], 0, offset, limit, coverage=ARCHIVE_COVERAGE, status="archive_not_configured"
                )
            conditions = [
                Message.source == self.platform,
                Message.source_account_id == entry.account_id,
                Message.conversation_id == entry.username,
            ]
            if exclude_events:
                # The candidate runtime uses SQLite. One JSON parameter avoids
                # SQLite's bind-variable ceiling after a long overlap interval.
                if archive.bind.dialect.name == "sqlite":
                    excluded = func.json_each(json.dumps(exclude_events)).table_valued("value")
                    conditions.append(Message.event_id.not_in(select(excluded.c.value)))
                else:
                    conditions.append(Message.event_id.not_in(exclude_events))
            sender_rows = archive.execute(
                select(Message.sender_id, func.max(Message.sender_name))
                .where(*conditions, Message.sender_id.is_not(None))
                .group_by(Message.sender_id)
                .order_by(Message.sender_id)
                .limit(501)
            ).all()
            sender_names = {
                row.username: row.remark or row.nick_name
                for row in self.s.scalars(
                    select(DirectoryEntry).where(
                        DirectoryEntry.source_id == source_id,
                        DirectoryEntry.account_id == entry.account_id,
                        DirectoryEntry.username.in_([row[0] for row in sender_rows[:500]]),
                    )
                )
            }
            sender_options = [
                {"id": identity, "name": sender_names.get(identity) or name or identity}
                for identity, name in sender_rows[:500]
            ]
            has_responses = archive.scalar(
                select(ResponseRecord.response_id)
                .join(Message, Message.id == ResponseRecord.message_id)
                .where(*conditions)
                .limit(1)
            )
            if has_responses and entry.account_id not in {row["id"] for row in sender_options}:
                sender_options.append({"id": entry.account_id, "name": "Bot（已归档回复）"})
            # Dedup only by a persisted provider receipt and stable account/conversation.
            echo = exists(
                select(DeliveryReceipt.delivery_id)
                .join(DeliveryOutboxRecord, DeliveryOutboxRecord.id == DeliveryReceipt.delivery_id)
                .where(
                    DeliveryOutboxRecord.account_id == Message.source_account_id,
                    DeliveryOutboxRecord.conversation_id == Message.conversation_id,
                    DeliveryReceipt.provider_message_id.is_not(None),
                    DeliveryReceipt.provider_message_id.in_(
                        [
                            Message.source_message_id,
                            Message.source_server_id,
                            Message.source_local_id,
                        ]
                    ),
                )
            )
            message_query = select(
                literal("message").label("kind"),
                cast(Message.id, String).label("id"),
                Message.occurred_at.label("at"),
            ).where(*conditions, ~(Message.is_self.is_(True) & echo))
            response_query = (
                select(
                    literal("response").label("kind"),
                    ResponseRecord.response_id.label("id"),
                    ResponseRecord.created_at.label("at"),
                )
                .join(Message, Message.id == ResponseRecord.message_id)
                .where(*conditions)
            )
            if q:
                message_query = message_query.where(Message.content.contains(q, autoescape=True))
                response_query = response_query.where(
                    exists(
                        select(ResponsePartRecord.response_id).where(
                            ResponsePartRecord.response_id == ResponseRecord.response_id,
                            ResponsePartRecord.text.contains(q, autoescape=True),
                        )
                    )
                )
            if sender:
                message_query = message_query.where(Message.sender_id == sender)
                if sender != entry.account_id:
                    response_query = response_query.where(literal(False))
            try:
                for value, end in ((date_from, False), (date_to, True)):
                    if value:
                        day = date.fromisoformat(value) + (
                            timedelta(days=1) if end else timedelta()
                        )
                        moment = datetime.combine(
                            day, datetime.min.time(), ZoneInfo("Asia/Shanghai")
                        ).astimezone(UTC)
                        message_query = message_query.where(
                            Message.occurred_at < moment if end else Message.occurred_at >= moment
                        )
                        response_query = response_query.where(
                            ResponseRecord.created_at < moment
                            if end
                            else ResponseRecord.created_at >= moment
                        )
            except ValueError:
                raise ManagementError("invalid_date_filter", 422) from None
            if date_from and date_to and date_from > date_to:
                raise ManagementError("invalid_date_range", 422)
            merged = union_all(message_query, response_query).subquery()
            total = archive.scalar(select(func.count()).select_from(merged))
            rows = archive.execute(
                select(merged)
                .order_by(merged.c.at.desc(), merged.c.kind, merged.c.id)
                .offset(offset)
                .limit(limit)
            ).all()
            items = [
                self.message_view(archive, entry, kind, identity) for kind, identity, _ in rows
            ]
            if local:
                for item in items:
                    item["id"] = "live:" + item["id"].removeprefix("m:")
            return page_result(
                items,
                total,
                offset,
                limit,
                coverage=ARCHIVE_COVERAGE,
                sender_options=sender_options,
                sender_options_partial=len(sender_rows) > 500,
                status="ok"
                if total
                else (
                    "no_filter_results"
                    if q or sender
                    else "no_records_in_range"
                    if date_from or date_to
                    else "no_archived_records"
                ),
            )

    def message_view(self, archive, entry, kind, identity):
        if kind == "message":
            message = archive.get(Message, int(identity))
            sender = self.find_entry(entry.source_id, entry.account_id, message.sender_id)
            attachments = [
                {
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "file_size": a.file_size,
                    "status": "metadata_only",
                }
                for a in message.attachments
            ]
            return {
                "id": "m:" + identity,
                "event_id": message.event_id,
                "content": message.content,
                "message_type": message.message_type,
                "sender_id": message.sender_id,
                "sender_name": (sender.remark or sender.nick_name)
                if sender
                else message.sender_name or message.sender_id or "系统",
                "avatar_url": self.entry_view(sender)["avatar_url"] if sender else None,
                "direction": message.direction,
                "occurred_at": iso(message.occurred_at),
                "delivery_status": None,
                "reply": message.reply_context,
                "attachments": attachments,
                "media_status": "原件未加载；阅读不自动下载",
            }
        response = archive.get(ResponseRecord, identity)
        delivery = archive.scalar(
            select(DeliveryOutboxRecord).where(
                DeliveryOutboxRecord.response_id == response.response_id,
                DeliveryOutboxRecord.account_id == entry.account_id,
                DeliveryOutboxRecord.conversation_id == entry.username,
            )
        )
        state = delivery.status.value if delivery else "uncertain"
        attachments = []
        for part in response.parts:
            if part.artifact_id:
                artifact = archive.get(Artifact, part.artifact_id)
                if artifact and artifact.response_id == response.response_id:
                    attachments.append(
                        {
                            "filename": artifact.filename,
                            "mime_type": artifact.mime_type,
                            "file_size": artifact.size,
                            "status": "metadata_only",
                            "artifact_status": artifact.status.value,
                        }
                    )
        return {
            "id": "r:" + identity,
            "content": "\n".join(p.text or "[文件产物：仅元数据]" for p in response.parts),
            "message_type": "text",
            "sender_id": entry.account_id,
            "sender_name": "Bot",
            "avatar_url": None,
            "direction": "assistant",
            "occurred_at": iso(response.created_at),
            "delivery_status": state,
            "reply": None,
            "attachments": attachments,
            "media_status": "不自动取得原件",
        }
