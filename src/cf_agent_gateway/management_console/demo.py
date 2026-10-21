"""Synthetic native HTTP protocol peer and archived messages, never real WeChat."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy import select

from cf_agent_gateway.message.models import Attachment, Conversation, Message
from cf_agent_gateway.profile_b.models import ManagementGroup, Node, Template


class DemoPeer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, token):
        self.token = token
        self.calls = []
        self.account = "demo-bot-a"
        self.offline = False
        self.app_not_running = False
        self.fail = False
        self.controls_enabled = False
        self.messages = {}
        self.contacts = [
            {
                "username": f"wxid_demo_{i:04d}",
                "nick_name": f"同事 {i:03d}",
                "remark": None,
                "alias": f"cf_demo_{i:03d}",
                "small_head_url": "demo:synthetic",
                "contact_type": "individual",
            }
            for i in range(247)
        ]
        self.contacts[0].update(nick_name="林溪", remark="林溪 · 运营", alias="linxi_demo")
        self.contacts[1].update(nick_name="周颖", remark="周颖 · 商品")
        self.contacts[2].update(nick_name="林溪", remark="林溪 · 客服")
        self.contacts[3].update(
            nick_name='<img src=x onerror="window.__xss=true">',
            small_head_url="https://127.0.0.1/private",
        )
        self.contacts[4]["small_head_url"] = None
        self.contacts.extend(
            [
                {"username": "gh_demo_news", "nick_name": "演示公众号", "contact_type": "official"},
                {
                    "username": "demo_staff@openim",
                    "nick_name": "演示企业微信",
                    "contact_type": "openim",
                },
            ]
        )
        self.chats = [
            {
                "id": f"room_demo_{i:03d}@chatroom",
                "username": f"room_demo_{i:03d}@chatroom",
                "name": "商品运营群" if i < 2 else f"业务协作群 {i:02d}",
                "isGroup": True,
                "unreadCount": i % 4,
                "lastMessagePreview": "请查阅本月资料（合成消息）",
                "lastActivityAt": "2026-10-21T01:10:00Z",
            }
            for i in range(53)
        ]
        self.chats[:0] = [
            {
                "id": f"wxid_demo_{i:04d}",
                "username": f"wxid_demo_{i:04d}",
                "name": self.contacts[i]["nick_name"],
                "isGroup": False,
                "unreadCount": 2,
                "lastMessagePreview": "本周商品资料已整理（合成消息）",
                "lastActivityAt": "2026-10-21T01:15:00Z",
            }
            for i in range(4)
        ]
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                peer.calls.append({"method": "GET", "path": url.path, "query": url.query})
                if self.headers.get("Authorization") != "Bearer " + peer.token:
                    self.send_error(401)
                    return
                if peer.fail:
                    self.send_error(503)
                    return
                params = parse_qs(url.query)
                limit = int(params.get("limit", [200])[0])
                offset = int(params.get("offset", [0])[0])
                if url.path == "/api/status/auth":
                    value = {
                        "status": "logged_out"
                        if peer.offline
                        else "app_not_running"
                        if peer.app_not_running
                        else "logged_in",
                        "loggedInUser": peer.account,
                    }
                elif url.path == "/api/contacts":
                    # Simulate native SQL-page followed by system-account filtering.
                    rows = (
                        peer.contacts[:197] + [{"contact_type": "system"}] * 3 + peer.contacts[197:]
                    )
                    value = [
                        {
                            {
                                "nick_name": "nickName",
                                "small_head_url": "smallHeadUrl",
                                "contact_type": "contactType",
                            }.get(key, key): item
                            for key, item in row.items()
                        }
                        for row in rows[offset : offset + limit]
                        if row["contact_type"] != "system"
                    ]
                elif url.path == "/api/chats":
                    value = peer.chats[offset : offset + limit]
                elif url.path.startswith("/api/messages/"):
                    chat = unquote(url.path.removeprefix("/api/messages/"))
                    value = sorted(
                        peer.messages.get(chat, []), key=lambda m: int(m["localId"]), reverse=True
                    )[offset : offset + limit]
                else:
                    self.send_error(405)
                    return
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                peer.calls.append({"method": "POST", "path": self.path})
                if (
                    peer.controls_enabled
                    and self.path == "/_synthetic/state"
                    and self.headers.get("Authorization") == "Bearer " + peer.token
                ):
                    size = int(self.headers.get("Content-Length", "0"))
                    if 0 < size <= 1024:
                        state = json.loads(self.rfile.read(size))
                        if set(state) <= {"offline", "app_not_running", "fail"} and all(
                            type(value) is bool for value in state.values()
                        ):
                            for name, value in state.items():
                                setattr(peer, name, value)
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            self.wfile.write(b'{"synthetic":true}')
                            return
                if (
                    self.path == "/_synthetic/append"
                    and self.headers.get("Authorization") == "Bearer " + peer.token
                ):
                    size = int(self.headers.get("Content-Length", "0"))
                    if 0 < size <= 65536:
                        message = json.loads(self.rfile.read(size))
                        peer.messages.setdefault(message["chatId"], []).append(message)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(
                            json.dumps(
                                {"synthetic": True, "available_at": datetime.now(UTC).isoformat()}
                            ).encode()
                        )
                        return
                self.send_error(405)

        super().__init__(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)


def seed_archive(session):
    if session.scalar(select(Conversation.id).where(Conversation.source == "wechat-demo")):
        return
    for chat in [
        "wxid_demo_0000",
        "wxid_demo_0001",
        "room_demo_000@chatroom",
        "room_demo_001@chatroom",
    ]:
        group = chat.endswith("@chatroom")
        session.add(
            Conversation(
                source="wechat-demo",
                source_account_id="demo-bot-a",
                conversation_id=chat,
                conversation_type="group" if group else "private",
                conversation_name="商品运营群" if group else "林溪 · 运营",
            )
        )
        session.flush()
        for i in range(95):
            stamp = datetime(2026, 9, 30, 15, 50, tzinfo=UTC) + timedelta(hours=i * 5)
            content = f"合成档案 {i:03d}：商品资料核对完成，保留原文件名。"
            kind = "text"
            if i == 92:
                content, kind = "产品手册（中文 空格）.pdf", "file"
            elif i == 93:
                content, kind = "[图片] 商品主图（未加载原件）", "image"
            elif i == 94:
                content = "收到，已核对上月资料。<script>window.__xss=true</script>"
            message = Message(
                event_id=f"demo:{chat}:{i}",
                source="wechat-demo",
                source_account_id="demo-bot-a",
                source_message_id=f"demo-msg-{i}",
                conversation_id=chat,
                conversation_type="group" if group else "private",
                is_mentioned=False if group else None,
                is_self=i % 5 == 0,
                sender_type="human",
                sender_id="demo-bot-a" if i % 5 == 0 else f"wxid_demo_{i % 3:04d}",
                sender_name="演示Bot" if i % 5 == 0 else ["林溪", "周颖", "林溪"][i % 3],
                message_type=kind,
                content=content,
                timestamp=stamp,
                occurred_at=stamp,
                reply_context={"content": "请核对上月资料", "sender_name": "周颖"}
                if i == 94
                else None,
            )
            session.add(message)
            session.flush()
            if i == 92:
                session.add(
                    Attachment(
                        message_id=message.id,
                        filename=content,
                        file_type="pdf",
                        mime_type="application/pdf",
                        file_size=2048,
                        storage_path="",
                        hash="0" * 64,
                    )
                )
    session.add_all(
        [
            Node(
                id="demo-node",
                name="隔离验证节点（未连接）",
                connection_ref="isolated-hermes",
                status="unchecked",
                capabilities={},
            ),
            Template(
                id="demo-employee-template",
                name="员工基础模板",
                kind="employee",
                version=1,
                soul="合成演示：保持员工独立状态，不扩大文件权限。",
                language="zh-CN",
            ),
            Template(
                id="demo-group-template",
                name="群协作模板",
                kind="group",
                version=1,
                soul="合成演示：按发起员工权限处理群任务。",
                language="zh-CN",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            ManagementGroup(
                id="demo-employee-group",
                name="运营部",
                kind="employee",
                template_id="demo-employee-template",
                node_id="demo-node",
            ),
            ManagementGroup(
                id="demo-room-group",
                name="商品协作",
                kind="group",
                template_id="demo-group-template",
                node_id="demo-node",
            ),
        ]
    )
    session.commit()
