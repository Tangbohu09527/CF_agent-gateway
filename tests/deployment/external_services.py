"""Explicit A-layer external substitutes; never a real WeChat/Hermes acceptance."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import secrets
import struct
import subprocess
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ACCOUNT = "wxid_clean_device_gateway"
SENDER = "wxid_clean_device_operator"
CHAT = SENDER
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ExternalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, kind: str, token_file: Path, *, host: str = "0.0.0.0"):
        super().__init__((host, port), Handler)
        self.kind = kind
        self.token = token_file.read_text().strip()
        self.auth = "logged_out"
        self.mode = "normal"
        self.account: object = ACCOUNT
        self.account_present = True
        self.allow_login = False
        self.account_messages: dict[str, list[dict[str, object]]] = {}
        self.account_sequences: dict[str, int] = {}
        self.account_chats: dict[str, list[str]] = {}
        self.conversation_polls: dict[str, int] = {}
        self.account_polls: dict[str, int] = {}
        self.auth_requests = 0
        self.deliveries: list[dict[str, object]] = []
        self.calls: list[dict[str, object]] = []
        self.cache: dict[str, tuple[str, dict[str, object]]] = {}
        self.polls = 0
        self.lock = threading.Lock()

    @property
    def account_key(self) -> str:
        return json.dumps(self.account, sort_keys=True)

    @property
    def messages(self) -> list[dict[str, object]]:
        return self.account_messages.setdefault(self.account_key, [])

    @property
    def chats(self) -> list[str]:
        return self.account_chats.setdefault(self.account_key, [CHAT])

    def control(self, payload: dict[str, object]) -> None:
        with self.lock:
            if "mode" in payload:
                self.mode = str(payload["mode"])
            if "auth" in payload:
                self.auth = str(payload["auth"])
            if "account" in payload:
                self.account = payload["account"]
                self.account_present = True
            if "account_present" in payload:
                self.account_present = bool(payload["account_present"])
            if "allow_login" in payload:
                self.allow_login = bool(payload["allow_login"])
            conversation = str(payload.get("conversation_id", CHAT))
            if "conversation_id" in payload and conversation not in self.chats:
                self.chats.append(conversation)
            if "message" in payload:
                key = self.account_key
                sequence = int(payload.get("local_id", self.account_sequences.get(key, 0) + 1))
                assert sequence > 0
                self.account_sequences[key] = max(sequence, self.account_sequences.get(key, 0))
                self.messages.append(
                    {
                        "localId": sequence,
                        "serverId": 10000 + sequence,
                        "chatId": conversation,
                        "sender": SENDER,
                        "senderName": "Synthetic operator",
                        "type": 1,
                        "content": str(payload["message"]),
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ExternalServer

    def log_message(self, *_args: object) -> None:
        pass  # Authorization, messages and QR payloads never enter HTTP logs.

    def respond(self, status: int, body: object, session: str | None = None) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if session:
            self.send_header("X-Hermes-Session-Id", session)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(encoded)

    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.server.token}"

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if self.server.kind == "wechat" and path == "/health":
            self.respond(200, {"status": "ok", "synthetic": True})
            return
        if not self.authorized():
            self.respond(401, {"error": "unauthorized"})
            return
        if (
            self.server.kind == "wechat"
            and path.startswith("/api/")
            and self.headers.get("X-Session-Id") != "default"
        ):
            self.respond(400, {"error": "session required"})
            return
        if path == "/__test/state":
            self.respond(
                200,
                {
                    "synthetic": True,
                    "kind": self.server.kind,
                    "auth": self.server.auth,
                    "account": self.server.account,
                    "account_present": self.server.account_present,
                    "auth_requests": self.server.auth_requests,
                    "polls": self.server.polls,
                    "account_polls": self.server.account_polls,
                    "conversation_polls": self.server.conversation_polls,
                    "messages": self.server.messages,
                    "chats": self.server.chats,
                    "calls": self.server.calls,
                    "deliveries": self.server.deliveries,
                },
            )
        elif path == "/api/ws/login" and self.server.kind == "wechat":
            self.websocket()
        elif path == "/api/status/auth" and self.server.kind == "wechat":
            self.server.auth_requests += 1
            if self.server.mode == "reject_auth":
                self.respond(401, {"error": "synthetic invalid credential"})
                return
            body = {"status": self.server.auth}
            if self.server.auth == "logged_in" and self.server.account_present:
                body["loggedInUser"] = self.server.account
            self.respond(200, body)
        elif path == "/api/chats" and self.server.kind == "wechat":
            self.respond(
                200,
                {
                    "chats": [
                        {"chatId": chat, "name": "Synthetic operator"} for chat in self.server.chats
                    ]
                },
            )
        elif path.startswith("/api/messages/") and self.server.kind == "wechat":
            conversation = unquote(path.removeprefix("/api/messages/"))
            if conversation not in self.server.chats:
                self.respond(404, {"error": "unknown conversation"})
                return
            self.server.polls += 1
            key = self.server.account_key
            self.server.account_polls[key] = self.server.account_polls.get(key, 0) + 1
            chat_key = json.dumps([self.server.account, conversation])
            self.server.conversation_polls[chat_key] = (
                self.server.conversation_polls.get(chat_key, 0) + 1
            )
            self.respond(
                200,
                {
                    "messages": [
                        item for item in self.server.messages if item["chatId"] == conversation
                    ]
                },
            )
        else:
            self.respond(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self.authorized():
            self.respond(401, {"error": "unauthorized"})
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(body) if body else {}
        if (
            self.server.kind == "wechat"
            and path.startswith("/api/")
            and self.headers.get("X-Session-Id") != "default"
        ):
            self.respond(400, {"error": "session required"})
            return
        if path == "/__test/control":
            self.server.control(payload)
            self.respond(200, {"synthetic": True})
        elif path == "/api/status/login" and self.server.kind == "wechat":
            if parse_qs(urlsplit(self.path).query).get("newAccount") != ["true"]:
                self.respond(400, {"error": "fresh login required"})
                return
            self.respond(200, {"success": False, "state": {"status": "qr_pending"}})
        elif path == "/api/messages/send" and self.server.kind == "wechat":
            self.server.deliveries.append(payload)
            self.respond(200, {"success": True, "localId": len(self.server.deliveries)})
        elif path == "/v1/chat/completions" and self.server.kind == "hermes":
            self.completion(payload)
        else:
            self.respond(404, {"error": "not_found"})

    def completion(self, payload: dict[str, object]) -> None:
        if self.server.mode == "reject_auth":
            self.respond(401, {"error": "synthetic invalid credential"})
            return
        if self.server.mode == "timeout":
            time.sleep(4)
        session = self.headers.get("X-Hermes-Session-Id")
        key = self.headers.get("Idempotency-Key")
        required = {
            "model",
            "messages",
            "profile_reference",
            "profile_revision",
            "thread_id",
            "session_metadata",
        }
        if not session or not key or not required.issubset(payload):
            self.respond(422, {"error": "missing Gateway V2 request contract fields"})
            return
        with self.server.lock:
            cached = self.server.cache.get(key)
            if cached:
                self.respond(200, cached[1], cached[0])
                return
            content = payload["messages"][0]["content"]  # type: ignore[index]
            response = {
                "response_id": "synthetic-" + secrets.token_hex(12),
                "parts": [{"type": "text", "text": f"Synthetic reply: {content}"}],
            }
            self.server.calls.append(
                {"session": session, "idempotency_key": key, "request": payload}
            )
            self.server.cache[key] = (session, response)
        self.respond(200, response, session)

    def websocket(self) -> None:
        if self.headers.get("X-Session-Id") != "default":
            self.respond(400, {"error": "session required"})
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode()).digest())
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept.decode())
        self.end_headers()
        for event in [
            {"type": "qr", "qrData": "synthetic://clean-device-fresh-qr"},
            {"type": "phone_confirm"},
            {"type": "login_success", "userId": self.server.account},
        ]:
            if event["type"] == "phone_confirm":
                deadline = time.monotonic() + 180
                while not self.server.allow_login:
                    if time.monotonic() >= deadline:
                        self.close_connection = True
                        return
                    time.sleep(0.1)
            if event["type"] == "login_success":
                self.server.auth = "logged_in"
            encoded = json.dumps(event).encode()
            header = (
                bytes((0x81, len(encoded)))
                if len(encoded) < 126
                else (bytes((0x81, 126)) + struct.pack("!H", len(encoded)))
            )
            self.wfile.write(header + encoded)
            self.wfile.flush()
            time.sleep(0.1)
        self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("wechat", "hermes"))
    parser.add_argument("--port", type=int, default=6174)
    parser.add_argument("--token-file", type=Path, default=Path("/data/auth-token"))
    args = parser.parse_args()
    if args.kind == "wechat":
        # Explicit substitute for the external application's process, checked by
        # the unmodified WeChat management script through real /proc and Docker.
        subprocess.Popen(["/usr/bin/wechat", "infinity"])
    ExternalServer(args.port, args.kind, args.token_file).serve_forever()


if __name__ == "__main__":
    main()
