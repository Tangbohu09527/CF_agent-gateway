"""Deterministic model peer: consumes the actual HTTP tool schemas/results.

No registered-tool shortcut: deferred tools must first be described by the
official bridge. Only a tool-produced return descriptor can authorize the next
model call's file_ref. This module has no Gateway/FileBridge imports.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOWNLOAD = "filebrowser_download_inbound"
READ = "filebrowser_read_inbound"
RETURN = "cf_return_current_chat"
MODEL = "cf-file-return-fixture"
TEXT = "Quarterly revenue is 1234 USD."


def objects(value):
    """Visit structured tool content, including deferred and multimodal output."""
    if isinstance(value, str):
        try:
            parsed, _ = json.JSONDecoder().raw_decode(value.lstrip())
        except ValueError:
            return
        yield from objects(parsed)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list, str)):
                yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def schemas(body, messages):
    visible = {
        item["function"]["name"]: item["function"]
        for item in body.get("tools", [])
        if item.get("type") == "function"
    }
    described = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        for item in objects(message.get("content")):
            tools = item.get("tools")
            if isinstance(tools, dict):
                for name, definition in tools.items():
                    if isinstance(definition, dict) and "parameters" in definition:
                        described[name] = {"name": name, **definition}
    return visible, {**described, **visible}


def verify_schema(name, definition):
    parameters = definition.get("parameters", {})
    properties = parameters.get("properties", {})
    assert parameters.get("type") == "object", "object schema missing"
    assert parameters.get("additionalProperties") is False, "open tool argument schema"
    required = set(parameters.get("required", []))
    if name == DOWNLOAD:
        assert required == {"attachment_id"}
        assert set(properties) == {"attachment_id"}
        assert properties["attachment_id"] == {"type": "integer", "minimum": 1}
    elif name == READ:
        assert required == {"attachment_id"}
        assert properties["attachment_id"]["type"] == "integer"
        assert properties["attachment_id"]["minimum"] == 1
        assert properties["prepare_return"]["type"] == "boolean"
    elif name == RETURN:
        assert required == {"file_ref", "kind"}
        assert properties["file_ref"]["type"] == "string"
        assert set(properties["kind"]["enum"]) == {"file", "image"}
        assert properties["filename"]["type"] == "string"
    else:
        raise AssertionError("unexpected model tool")
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


def tool_call(name, arguments, visible):
    if name not in visible:
        bridge = visible.get("tool_call", {}).get("parameters", {})
        assert bridge.get("properties", {}).get("calls", {}).get("type") == "array"
        name, arguments = "tool_call", {"calls": [{"name": name, "arguments": arguments}]}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_" + str(time.monotonic_ns()),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class Planner:
    def __init__(self, *, download_first=False):
        self.download_first = download_first
        self.download_verified = False
        self.download_first_steps = []
        self.schema_hashes = {}
        self.prepared = {}
        self.calls = []
        self.images = []
        self.pdf_text_seen = False
        self.repeated_descriptors_equal = False
        self.read_only_verified = False
        self.lease_entered = threading.Event()
        self.lease_release = threading.Event()

    def decide(self, body):
        messages = body.get("messages", [])
        self.calls.append(body)
        # Official vision may use the same configured HTTP model without tools.
        for message in messages:
            for item in objects(message.get("content")):
                image = item.get("image_url", {}).get("url")
                if isinstance(image, str) and image.startswith("data:image/"):
                    self.images.append(image)
        self.pdf_text_seen |= TEXT in json.dumps(messages)
        marker = -1
        for index, message in enumerate(messages):
            if message.get("role") == "user" and "file-return-case:" in str(message.get("content")):
                marker = index
        if marker < 0 or not body.get("tools"):
            return {"role": "assistant", "content": "Synthetic image has a red pixel."}
        text = str(messages[marker].get("content", ""))
        label = re.search(r"file-return-case:([a-z-]+)", text).group(1)
        if label in {"instruction", "followup"}:
            return {"role": "assistant", "content": "Ready to read and return original bytes."}
        recent = messages[marker + 1 :]
        visible, available = schemas(body, recent)
        wanted = (
            [RETURN] if label == "replay" else [READ] if label == "reuse-read" else [READ, RETURN]
        )
        if self.download_first:
            assert label == "pdf", "download-first probe is PDF-only"
            wanted.insert(0, DOWNLOAD)
        missing = [name for name in wanted if name not in available]
        if missing:
            describe = visible.get("tool_describe", {}).get("parameters", {})
            assert describe.get("properties", {}).get("names", {}).get("type") == "array", (
                "actual describe schema absent"
            )
            return tool_call("tool_describe", {"names": missing}, visible)
        for name in wanted:
            self.schema_hashes[name] = verify_schema(name, available[name])
        records = [
            obj
            for message in recent
            if message.get("role") == "tool"
            for obj in objects(message.get("content"))
        ]
        prepared = [
            item
            for item in records
            if set(("file_ref", "kind", "filename", "size_bytes", "sha256")) <= item.keys()
        ]
        # De-duplicate nested envelope traversals by their exact descriptor.
        prepared = [item for item in prepared if isinstance(item["file_ref"], str)]
        errors = [
            item for item in records if item.get("ok") is False or item.get("success") is False
        ]
        receipts = [
            item
            for item in records
            if item.get("status") == "pending_delivery"
            and item.get("success") is True
            and "artifact_id" in item
        ]
        if label == "identity" and any(
            item.get("verified") is True and item.get("return_preparation", {}).get("ok") is False
            for item in records
        ):
            return {"role": "assistant", "content": "Cross-identity preparation rejected."}
        if label == "lease" and any(item.get("success") is False for item in records):
            return {
                "role": "assistant",
                "content": "Revoked inbound source rejected at publication.",
            }
        if label == "replay":
            if errors:
                return {"role": "assistant", "content": "Old return reference rejected."}
            assert self.prepared, "replay has no actual earlier tool descriptor"
            value = list(self.prepared.values())[-1]
            return tool_call(
                RETURN, {key: value[key] for key in ("file_ref", "kind", "filename")}, visible
            )
        match = re.search(r'"attachment_id"\s*:\s*(\d+)', text)
        assert match, "real Gateway omitted attachment ID"
        attachment = int(match.group(1))
        if label == "reuse-read":
            reads = [
                item
                for item in records
                if item.get("ok") is True
                and item.get("verified") is True
                and item.get("format") == "pdf"
            ]
            if reads:
                assert not prepared and not receipts
                assert all("return_preparation" not in item for item in reads)
                assert TEXT in json.dumps(reads), "read-only result omitted original PDF text"
                self.read_only_verified = True
                return {
                    "role": "assistant",
                    "content": "Read existing PDF without a return capability.",
                }
            assert len(errors) <= 1, "read-only existing attachment failed"
            return tool_call(READ, {"attachment_id": attachment, "prepare_return": False}, visible)
        if self.download_first:
            downloads = [
                item
                for item in records
                if item.get("ok") is True and item.get("verified") is True and "handle" in item
            ]
            if not downloads:
                assert (
                    not errors and not prepared and not any("handle" in item for item in records)
                ), "actual first download failed"
                self.download_first_steps.append(DOWNLOAD)
                return tool_call(DOWNLOAD, {"attachment_id": attachment}, visible)
            value = downloads[-1]
            assert re.fullmatch(r"inbound:[0-9a-f]{48}", value["handle"])
            assert value.get("formal_archive") is False
            assert type(value.get("bytes_written")) is int and value["bytes_written"] > 0
            assert re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
            self.download_verified = True
            if not prepared:
                assert not errors, "read after verified download failed"
                self.download_first_steps.append(READ)
                return tool_call(
                    READ, {"attachment_id": attachment, "prepare_return": True}, visible
                )
        if not errors and not prepared:
            # Real active binding must reject an unrelated attachment, before the
            # successful read. This is not a fabricated transport response.
            return tool_call(
                READ, {"attachment_id": attachment + 100000, "prepare_return": True}, visible
            )
        if not prepared:
            assert len(errors) <= 1, "actual read did not yield a prepared descriptor"
            return tool_call(READ, {"attachment_id": attachment, "prepare_return": True}, visible)
        descriptor = {
            key: prepared[-1][key]
            for key in ("file_ref", "kind", "filename", "size_bytes", "sha256")
        }
        assert not descriptor["file_ref"].startswith(("inbound:", "/", "\\"))
        assert re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"])
        assert descriptor["kind"] == ("image" if label == "png" else "file")
        if self.download_first:
            assert descriptor["sha256"] == value["sha256"] and (
                descriptor["size_bytes"] == value["bytes_written"]
            ), "prepared bytes differ from the verified download"
        operation = (label, attachment)
        if operation not in self.prepared:
            self.prepared[operation] = descriptor
            return tool_call(READ, {"attachment_id": attachment, "prepare_return": True}, visible)
        assert self.prepared[operation] == descriptor, (
            "repeat changed the authorized working-copy descriptor"
        )
        self.repeated_descriptors_equal = True
        if label == "lease":
            self.lease_entered.set()
            assert self.lease_release.wait(15), "bounded lease fixture not released"
        if not receipts:
            if self.download_first:
                self.download_first_steps.append(RETURN)
            return tool_call(
                RETURN, {key: descriptor[key] for key in ("file_ref", "kind", "filename")}, visible
            )
        return {"role": "assistant", "content": "Read original bytes and handed them to Gateway."}


class ModelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, download_first=False):
        super().__init__(("127.0.0.1", 0), Handler)
        self.planner = Planner(download_first=download_first)
        self.errors = []

    @property
    def config(self):
        return {
            "provider": "custom",
            "default": MODEL,
            "api_mode": "chat_completions",
            "base_url": f"http://127.0.0.1:{self.server_port}/v1",
            "context_length": 131072,
            "supports_vision": True,
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.send_json(
            {
                "custom": {
                    "id": "custom",
                    "name": "Isolated fixture",
                    "models": {
                        MODEL: {
                            "id": MODEL,
                            "tool_call": True,
                            "attachment": True,
                            "modalities": {"input": ["text", "image"], "output": ["text"]},
                            "limit": {"context": 131072, "output": 4096},
                        }
                    },
                }
            }
        )

    def do_POST(self):
        try:
            size = int(self.headers["Content-Length"])
            assert 0 < size <= 16 * 1024 * 1024
            body = json.loads(self.rfile.read(size))
            message = self.server.planner.decide(body)
            finish = "tool_calls" if "tool_calls" in message else "stop"
            envelope = {
                "id": "chatcmpl-isolated",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40},
            }
            if not body.get("stream"):
                self.send_json(envelope)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [
                    {"index": index, **item} for index, item in enumerate(delta["tool_calls"])
                ]
            for value, reason in ((delta, None), ({}, finish)):
                chunk = {
                    "id": envelope["id"],
                    "object": "chat.completion.chunk",
                    "created": envelope["created"],
                    "model": MODEL,
                    "choices": [{"index": 0, "delta": value, "finish_reason": reason}],
                }
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as error:
            # Safe source line numbers, never model/tool bodies or exception text.
            self.server.errors.append(
                {
                    "class": type(error).__name__,
                    "lines": [
                        frame.lineno
                        for frame in traceback.extract_tb(error.__traceback__)
                        if frame.filename == __file__
                    ],
                }
            )
            self.send_json(
                {"error": {"type": "fixture_assertion", "message": "model contract failed"}}, 400
            )
