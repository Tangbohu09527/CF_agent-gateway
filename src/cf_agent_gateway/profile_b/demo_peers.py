"""Loopback deterministic model for the isolated demo. Never calls a paid provider."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "cf-profile-b-deterministic"


class ModelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=18761):
        super().__init__(("127.0.0.1", port), ModelHandler)
        self.calls = []
        self.guard = threading.Lock()

    @property
    def config(self):
        return {
            "provider": "custom",
            "default": MODEL,
            "api_mode": "chat_completions",
            "base_url": f"http://127.0.0.1:{self.server_port}/v1",
            "context_length": 131072,
            "api_key": "synthetic-loopback-only",
        }


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/evidence":
            with self.server.guard:
                self.send_json({"model": "deterministic substitute", "calls": self.server.calls})
        elif self.path.endswith("/models"):
            self.send_json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        elif self.path == "/models-dev.json":
            self.send_json(
                {
                    "custom": {
                        "id": "custom",
                        "name": "Synthetic test",
                        "models": {
                            MODEL: {
                                "id": MODEL,
                                "tool_call": True,
                                "limit": {"context": 131072, "output": 4096},
                            }
                        },
                    }
                }
            )
        else:
            self.send_json({})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        messages = body.get("messages", [])
        system = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") in {"system", "developer"}
        )
        users = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        tool_messages = [m for m in messages[last_user + 1 :] if m.get("role") == "tool"]
        evidence = {
            "soul_markers": sorted(set(re.findall(r"CF_SOUL_[A-Z0-9_]+", system))),
            "history_markers": sorted(set(re.findall(r"CF_MSG_[A-Z0-9_]+", users))),
            "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "profile_anchors": [
                line
                for line in system.splitlines()
                if "profile" in line.lower()
                and ("home" in line.lower() or "active" in line.lower())
            ],
            "user_turns": sum(m.get("role") == "user" for m in messages),
            "model": body.get("model"),
            "substitute": True,
            "tool_read_markers": sorted(
                set(
                    re.findall(
                        r"CF_FILE_[A-Z0-9_]+",
                        "\n".join(str(m.get("content", "")) for m in tool_messages),
                    )
                )
            ),
        }
        with self.server.guard:
            self.server.calls.append(evidence)
        text = json.dumps(evidence, ensure_ascii=False)
        envelope = {
            "id": f"cf-{time.time_ns()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 40, "completion_tokens": 30, "total_tokens": 70},
        }
        tools = {item.get("function", {}).get("name") for item in body.get("tools", [])}
        tool_call = None
        if (
            "CF_MSG_READ_ONLY" in str(messages[last_user].get("content", ""))
            and not tool_messages
            and "read_file" in tools
        ):
            tool_call = {
                "id": "cf-read-" + str(time.time_ns()),
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "profile-readonly.txt"}),
                },
            }
            envelope["choices"][0].update(
                message={"role": "assistant", "content": None, "tool_calls": [tool_call]},
                finish_reason="tool_calls",
            )
        if not body.get("stream"):
            self.send_json(envelope)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        deltas = [({"role": "assistant", "content": text}, None), ({}, "stop")]
        if tool_call:
            deltas = [
                ({"role": "assistant", "tool_calls": [{"index": 0, **tool_call}]}, None),
                ({}, "tool_calls"),
            ]
        for delta, reason in deltas:
            chunk = {
                "id": envelope["id"],
                "object": "chat.completion.chunk",
                "created": envelope["created"],
                "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
