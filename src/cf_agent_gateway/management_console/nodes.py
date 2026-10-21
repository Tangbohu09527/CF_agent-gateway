"""Candidate node inventory and bounded, read-only connection checks.

Credentials are operator-owned references or protected runtime data. A health check
never submits a prompt, creates a profile, or changes the remote configuration.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import socket
import ssl
import tempfile
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from cf_agent_gateway.admin.auth import enforce_admin_request_body_limit, require_admin_role
from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.profile_b.hermes import Connection, HermesNode, NodeError
from cf_agent_gateway.profile_b.models import ManagementGroup, Node

OFFICIAL_REVISION = "427d605d4985c2d49a795493ef6c5d66413057d1"
TTL_SECONDS = 120
CHECK_TIMEOUT = 3.0
MAX_RESPONSE = 512_000
NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")
)


def stamp():
    return datetime.now(UTC).isoformat()


def node_error(code, status=422):
    raise HTTPException(status, code)


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    business_url: str = Field(default="", max_length=512)
    model_name: str = Field(default="hermes-agent", min_length=1, max_length=128)
    management_url: str = Field(default="", max_length=512)
    setup: str = Field(default="new", pattern="^(new|existing)$")
    business_token: SecretStr | None = None
    management_token: SecretStr | None = None


class ManagementConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=512)
    token: SecretStr


def validate_url(value, *, management=False):
    """Only explicit local/LAN node origins. No proxy paths, userinfo or redirects."""
    try:
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not (1024 <= port <= 65535 or port == 443)
            or any(c in value for c in "\r\n\t\\")
        ):
            raise ValueError
        host = parsed.hostname
        # Numeric destinations are checked without a lookup at save time; named
        # hosts are checked and pinned when the operator requests a test.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not host.isascii() or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
                for c in host
            ):
                raise ValueError from None
        else:
            if not any(
                address in network for network in NETWORKS if network.version == address.version
            ):
                raise ValueError
        if (
            management
            and parsed.scheme != "https"
            and host not in {"localhost", "127.0.0.1", "::1"}
        ):
            node_error("management_requires_https")
        return value.rstrip("/")
    except (ValueError, TypeError):
        node_error("node_address_not_allowed")


def resolve_origin(value):
    parsed = urlsplit(validate_url(value))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ProbeError("dns_failed") from None
    addresses = list(dict.fromkeys(item[4][0] for item in answers))
    if not addresses or any(
        not any(
            ipaddress.ip_address(item) in net
            for net in NETWORKS
            if net.version == ipaddress.ip_address(item).version
        )
        for item in addresses
    ):
        raise ProbeError("address_outside_lan")
    return parsed, addresses, port


class ProbeError(RuntimeError):
    pass


def check_result(state, code, **extra):
    observed = datetime.now(UTC)
    return {
        "status": state,
        "code": code,
        "observed_at": observed.isoformat(),
        "expires_at": (observed + timedelta(seconds=TTL_SECONDS)).isoformat(),
        "ttl_seconds": TTL_SECONDS,
        "source": "candidate_server_read_only_probe",
        **extra,
    }


def classify_exception(error):
    if isinstance(
        error, (ssl.SSLError, ssl.CertificateError)
    ) or "CERTIFICATE_VERIFY_FAILED" in str(error):
        return "tls_failed"
    if isinstance(error, (httpx.TimeoutException, TimeoutError, socket.timeout)):
        return "timeout"
    if (
        isinstance(error, ConnectionRefusedError)
        or "Connection refused" in str(error)
        or "10061" in str(error)
    ):
        return "port_closed"
    if isinstance(error, ProbeError):
        return str(error)
    return "network_unreachable"


class PinnedProbe:
    """Resolve once, validate every address, then dial an approved numeric IP.

    HTTPS still uses the original SNI, Host and hostname verification. No proxy
    environment or redirects are involved, and no upstream body is logged.
    """

    def __init__(self, url):
        self.parsed, self.addresses, self.port = resolve_origin(url)
        self.address = self.addresses[0]
        self.context = ssl.create_default_context()

    def _socket(self):
        stream = socket.create_connection((self.address, self.port), CHECK_TIMEOUT)
        if self.parsed.scheme == "https":
            try:
                return self.context.wrap_socket(stream, server_hostname=self.parsed.hostname)
            except BaseException:
                stream.close()
                raise
        return stream

    def get(self, path, token):
        return self.request("GET", path, token)

    def request(self, method, path, token, body=None):
        # http.client consumes the socket we explicitly dialed. This avoids a
        # second DNS resolution while preserving normal HTTP framing/limits.
        from http.client import HTTPResponse

        host = self.parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        host += ":" + str(self.port)
        with self._socket() as stream:
            expired = threading.Event()

            def interrupt():
                expired.set()
                with suppress(OSError):
                    stream.shutdown(socket.SHUT_RDWR)

            deadline = threading.Timer(CHECK_TIMEOUT, interrupt)
            deadline.daemon = True
            deadline.start()
            try:
                content = b"" if body is None else json.dumps(body).encode("utf-8")
                message = (
                    f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {token}\r\n"
                    "Accept: application/json\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(content)}\r\nConnection: close\r\n\r\n"
                )
                stream.sendall(message.encode("ascii") + content)
                response = HTTPResponse(stream)
                response.begin()
                content = response.read(MAX_RESPONSE + 1)
                if expired.is_set():
                    raise TimeoutError
                if len(content) > MAX_RESPONSE:
                    raise ProbeError("response_too_large")
                if 300 <= response.status < 400:
                    raise ProbeError("redirect_rejected")
                try:
                    payload = json.loads(content)
                except (ValueError, UnicodeError):
                    payload = None
                return response.status, payload
            except Exception:
                if expired.is_set():
                    raise TimeoutError from None
                raise
            finally:
                deadline.cancel()

    def rpc(self, token):
        # The websocket library receives the already connected TCP socket; its
        # TLS layer retains the original URL hostname for SNI/verification.
        stream = socket.create_connection((self.address, self.port), CHECK_TIMEOUT)
        scheme = "wss" if self.parsed.scheme == "https" else "ws"
        netloc = self.parsed.netloc
        url = f"{scheme}://{netloc}/api/ws?" + urlencode({"token": token})
        options = {
            "sock": stream,
            "open_timeout": CHECK_TIMEOUT,
            "close_timeout": 1,
            "proxy": None,
            "max_size": MAX_RESPONSE,
        }
        if scheme == "wss":
            options["ssl"] = self.context
        try:
            with connect(url, **options) as ws:
                ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "cf-node-readonly",
                            "method": "gateway.ping",
                            "params": {},
                        }
                    )
                )
                deadline = time.monotonic() + CHECK_TIMEOUT
                while True:
                    message = json.loads(ws.recv(timeout=max(0.01, deadline - time.monotonic())))
                    if message.get("id") == "cf-node-readonly":
                        if message.get("error") or not isinstance(message.get("result"), dict):
                            raise ProbeError("rpc_not_supported")
                        return
                    if time.monotonic() >= deadline:
                        raise ProbeError("timeout")
        finally:
            stream.close()


def probe_connection(endpoint, kind, *, factory=PinnedProbe):
    if not endpoint or not endpoint.get("url") or not endpoint.get("token"):
        return check_result("not_configured", "connection_not_configured")
    checks = {}
    try:
        if not 1 <= len(endpoint["token"]) <= 1024 or any(
            ord(c) < 33 or ord(c) > 126 for c in endpoint["token"]
        ):
            return check_result("failed", "credential_format_invalid", checks=checks)
        probe = factory(endpoint["url"])
        path = "/v1/models" if kind == "business" else "/api/profiles"
        code, body = probe.get(path, endpoint["token"])
        checks["network"] = "reachable"
        checks["tls"] = "verified" if endpoint["url"].startswith("https:") else "not_used_lan_http"
        if code in {401, 403}:
            return check_result("failed", "authentication_failed", checks=checks)
        if code != 200:
            return check_result(
                "failed", "interface_not_supported", http_status=code, checks=checks
            )
        checks["authentication"] = "accepted"
        if kind == "business":
            valid = (
                isinstance(body, dict)
                and body.get("object") == "list"
                and isinstance(body.get("data"), list)
                and all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in body["data"])
            )
        else:
            valid = isinstance(body, dict) and isinstance(body.get("profiles"), list)
        if not valid:
            return check_result("failed", "invalid_response_format", checks=checks)
        wrong_code, _ = probe.get(path, "cf-invalid-" + secrets.token_urlsafe(24))
        checks["invalid_credential_rejected"] = wrong_code in {401, 403}
        if not checks["invalid_credential_rejected"]:
            return check_result("failed", "authentication_not_enforced", checks=checks)
        checks["response_format"] = "supported"
        extra = {}
        if kind == "management":
            schema_code, schema = probe.get("/api/config/schema", endpoint["token"])
            checks["config_schema"] = bool(
                schema_code == 200 and isinstance(schema, dict) and schema.get("fields")
            )
            if not checks["config_schema"]:
                return check_result("partial", "management_schema_not_supported", checks=checks)
            health_code, health = probe.get("/api/health", endpoint["token"])
            if health_code == 200 and isinstance(health, dict):
                extra["version"] = {
                    k: str(health[k])[:80]
                    for k in ("version", "commit")
                    if isinstance(health.get(k), str)
                }
            try:
                probe.rpc(endpoint["token"])
            except Exception as error:
                checks["rpc"] = "failed"
                return check_result(
                    "partial", "rpc_" + classify_exception(error), checks=checks, **extra
                )
            checks["rpc"] = "read_only_ping_passed"
            try:
                probe.rpc("cf-invalid-" + secrets.token_urlsafe(24))
            except (InvalidStatus, ConnectionClosed) as error:
                rejected = (
                    isinstance(error, InvalidStatus) and error.response.status_code in {401, 403}
                ) or (
                    isinstance(error, ConnectionClosed)
                    and error.rcvd is not None
                    and error.rcvd.code in {1008, 4401, 4403}
                )
                checks["invalid_rpc_credential_rejected"] = rejected
                if not rejected:
                    return check_result(
                        "partial", "rpc_authentication_rejection_unverified", checks=checks, **extra
                    )
            except Exception:
                checks["invalid_rpc_credential_rejected"] = False
                return check_result(
                    "partial", "rpc_authentication_rejection_unverified", checks=checks, **extra
                )
            else:
                checks["invalid_rpc_credential_rejected"] = False
                return check_result(
                    "failed", "rpc_authentication_not_enforced", checks=checks, **extra
                )
        else:
            extra["advertised_models"] = [x["id"][:128] for x in body["data"][:30]]
        return check_result(
            "available",
            "read_only_check_passed",
            checks=checks,
            model_execution="not_tested",
            profile_create="not_tested",
            **extra,
        )
    except Exception as error:
        return check_result("failed", classify_exception(error), checks=checks)


TUTORIAL = {
    "official_revision": OFFICIAL_REVISION,
    "prerequisites": [
        "AI 电脑已安装官方 Hermes，原模型已经配置可用；不需要重新安装。",
        "CFserver 到这台电脑已有获准网络路径；使用实际地址，不扫描或扩大防火墙范围。",
        "本向导可生成业务 API 密钥；新 Profile 管理入口的自动配置尚未接通。",
    ],
    "business": {
        "title": "官方 OpenAI 兼容 API Server（业务入口）",
        "fields": [
            {
                "label": "Enable API server",
                "value": "true",
                "help": "开启业务接口；不等于开启 Profile 管理。",
            },
            {
                "label": "API server auth key",
                "value": "本向导生成或已有密钥",
                "help": (
                    "接口认证密钥；不是模型供应商 API Key，也不是 SSH 密码。已有连接保留原密钥。"
                ),
            },
            {
                "label": "API server port",
                "value": "8642",
                "help": "AI 电脑上的监听端口；可按实际配置修改。",
            },
            {
                "label": "API server host",
                "value": "0.0.0.0",
                "help": (
                    "在 AI 电脑上监听可达网卡。0.0.0.0 是监听含义；"
                    "Gateway 连接目标必须是电脑的实际地址或域名。"
                    "需使用已有获准网络和防火墙范围。"
                ),
            },
            {
                "label": "API server model name",
                "value": "hermes-agent",
                "help": ("接口向 /v1/models 广告的名字；这里不会切换 Hermes 实际业务模型。"),
            },
        ],
        "activation": (
            "在官方 Hermes 设置中保存五个字段后，由真人在 AI 电脑正常启动官方 Gateway "
            "（该固定版本的前台入口是 hermes gateway run）；已经通过官方服务管理运行的 "
            "Gateway 使用原有服务方式重新加载/重启后才会读取新设置。先确认没有在途任务。"
            "保存不等于已加载；本向导不远程启动、重启或升级 Hermes。已有连接先保留原设置。"
        ),
    },
    "management": {
        "title": "hermes serve（独立管理入口）",
        "help": (
            "Profile B 使用 /api/profiles、/api/config 与 /api/ws JSON-RPC。"
            "API Server 五个字段不能配置这些接口。"
            "管理入口的地址、端口和会话认证与业务 API 分别配置，不假定同一密钥。"
        ),
        "activation": (
            "复用已配置的官方 hermes serve 管理入口及受信 HTTPS。"
            "固定版本非回环管理监听有独立认证门；不可关闭认证或用 "
            "API_SERVER_KEY 猜作管理 Token。若目前只有业务 API，可先登记，"
            "页面会显示“Profile 管理待配置”。"
            "现有 PR25 的受控 HTTPS 管理连接可直接复用。"
        ),
        "limits": (
            "新管理入口自动配置尚未接通。这里只接入已有受控管理地址及其已有会话凭据，"
            "不会生成无法填入官方设置的管理密钥。没有这两项就先留空；"
            "本页只读测试通过后仍不表示已创建 Profile。"
            "实际创建使用原 Profile 管理动作并记录结果。"
        ),
    },
    "steps": [
        (
            "给电脑命名，填 CFserver 实际可达的业务地址；"
            "已有 Hermes 选择“接入已配置连接”并保留原认证。"
        ),
        (
            "新连接生成配置后，在该电脑官方 Hermes 设置中逐项填写上述五个 "
            "API Server 字段。密钥仅在明确查看时返回，复制后遮蔽。"
        ),
        "按官方版本保存并启用相应服务，保持原模型和 Profile 记忆不变。",
        (
            "回 Web 点击“测试配置”。结果由当前管理后端所在服务器发起；"
            "网络、TLS、认证、业务、管理 REST 与 RPC 分项显示。"
        ),
        "保存节点后去分组选择这台电脑。只有业务接口时不能执行 Profile 管理；需要管理入口另外通过。",
    ],
}


class NodeRegistry:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.runtime) / "node-registry.json"
        self.lock = threading.RLock()
        self.test_locks = {}
        self.data = {"version": 1, "nodes": {}}
        if self.path.exists():
            if self.path.is_symlink():
                raise RuntimeError("node_registry_symlink_rejected")
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.imports = self._imports()
        self.filebrowser = self.imports.pop("__filebrowser__", None)
        for item in [*self.imports.values(), *self.data["nodes"].values()]:
            self._install_connection(item)

    def _imports(self):
        result = {}
        for ref, connection in list(self.config.hermes_connections.items()):
            if ref.startswith("console-node-"):
                continue
            result["connection:" + ref] = {
                "id": "connection:" + ref,
                "identity": "connection:" + ref,
                "name": ref,
                "source": "candidate_connection",
                "connection_ref": ref,
                "management": {"url": connection.url, "token": connection.token},
                "created_at": stamp(),
                "checks": {},
            }
        path = Path(self.config.runtime) / "existing-nodes.json"
        if not path.exists():
            return result
        if path.is_symlink():
            raise RuntimeError("existing_nodes_symlink_rejected")
        raw = json.loads(path.read_text(encoding="utf-8"))
        for supplied in raw.get("nodes", []):
            identity = supplied["identity"]
            key = "import:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            item = {
                "id": key,
                "identity": identity,
                "name": supplied["name"],
                "source": supplied.get("source", "gateway_config"),
                "checks": {},
                "created_at": stamp(),
            }
            for kind in ("business", "management"):
                value = supplied.get(kind)
                if not value:
                    continue
                ref = value.get("connection_ref")
                if ref:
                    connection = self.config.hermes_connections.get(ref)
                    if connection:
                        item[kind] = {"url": connection.url, "token": connection.token}
                        item["connection_ref"] = ref
                        result.pop("connection:" + ref, None)
                elif value.get("url"):
                    item[kind] = {
                        "url": validate_url(value["url"], management=kind == "management"),
                        "token": os.environ.get(value.get("token_env", ""), ""),
                        "model_name": value.get("model_name"),
                    }
            result[key] = item
        value = raw.get("filebrowser")
        if value:
            path = value.get("health_path", "/health")
            if path not in {"/health", "/api/health"}:
                raise RuntimeError("filebrowser_health_path_not_allowed")
            result["__filebrowser__"] = {
                "url": validate_url(value["url"]),
                "token": os.environ.get(value.get("token_env", ""), ""),
                "health_path": path,
            }
        return result

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".nodes-", dir=self.path.parent)
        try:
            os.chmod(name, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _install_connection(self, item):
        endpoint = item.get("management")
        if endpoint and endpoint.get("token"):
            ref = item.get("connection_ref") or "console-node-" + item["id"]
            original = self.config.hermes_connections.get(ref)
            if original and original.url == endpoint["url"] and original.token == endpoint["token"]:
                # Preserve PR25 operator policy, including initial model config.
                item["connection_ref"] = ref
                return
            self.config.hermes_connections[ref] = Connection(endpoint["url"], endpoint["token"])
            item["connection_ref"] = ref

    def find(self, key, session=None):
        item = self.data["nodes"].get(key) or self.imports.get(key)
        if not item and session is not None and key.startswith("unavailable:"):
            row = session.get(Node, key.removeprefix("unavailable:"))
            if row is not None:
                item = {
                    "id": key,
                    "identity": "registered:" + row.id,
                    "name": row.name,
                    "connection_ref": row.connection_ref,
                    "source": "candidate_registered",
                    "created_at": None,
                    "checks": {},
                }
        if not item:
            node_error("node_not_found", 404)
        return item

    def view(self, item, session):
        ref = item.get("connection_ref") or "console-node-" + item["id"]
        row = session.scalar(select(Node).where(Node.connection_ref == ref))
        checks = item.get("checks", {})
        return {
            "id": item["id"],
            "identity": item["identity"],
            "name": item["name"],
            "source": item["source"],
            "registered": bool(row),
            "node_id": row.id if row else None,
            "created_at": item["created_at"],
            "business": {
                "configured": bool(item.get("business")),
                "credential_generated": bool(item.get("business", {}).get("generated")),
                "address": item.get("business", {}).get("url"),
                "model_name": item.get("business", {}).get("model_name"),
                "check": checks.get("business"),
            },
            "management": {
                "configured": bool(item.get("management")),
                "address": item.get("management", {}).get("url"),
                "check": checks.get("management"),
            },
            "profile_create": item.get("profile_verification", {"status": "not_tested"}),
            "checks": checks,
            "groups": [
                {"id": g.id, "name": g.name}
                for g in session.scalars(
                    select(ManagementGroup).where(ManagementGroup.node_id == row.id)
                )
            ]
            if row
            else [],
            "resources": {"cpu": "not_collected", "gpu": "not_collected"},
            "candidate_only": True,
            "production_effective": False,
        }

    def listing(self, session):
        with self.lock:
            items = [v for k, v in self.imports.items() if k not in self.data["nodes"]] + list(
                self.data["nodes"].values()
            )
            refs = {item.get("connection_ref") or "console-node-" + item["id"] for item in items}
            for row in session.scalars(select(Node)):
                if row.connection_ref not in refs:
                    items.append(
                        {
                            "id": "unavailable:" + row.id,
                            "identity": "registered:" + row.id,
                            "name": row.name,
                            "connection_ref": row.connection_ref,
                            "source": "candidate_registered",
                            "created_at": None,
                            "checks": {},
                        }
                    )
            return [self.view(item, session) for item in items]

    def draft(self, body, request_key, session):
        if not request_key or len(request_key) > 128 or not request_key.isascii():
            node_error("idempotency_key_required")
        digest = hashlib.sha256(request_key.encode()).hexdigest()
        fingerprint_data = body.model_dump(mode="json")
        for kind in ("business", "management"):
            secret = getattr(body, kind + "_token")
            fingerprint_data[kind + "_token"] = (
                hashlib.sha256(secret.get_secret_value().encode()).hexdigest() if secret else None
            )
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True).encode()
        ).hexdigest()
        with self.lock:
            existing = next(
                (n for n in self.data["nodes"].values() if n.get("request_key") == digest), None
            )
            if existing:
                if existing["fingerprint"] != fingerprint:
                    node_error("idempotency_payload_conflict", 409)
                return self.view(existing, session)
            if not body.name.strip() or not (body.business_url or body.management_url):
                node_error("name_and_connection_required")
            if body.management_url and not body.management_token:
                node_error("existing_management_credential_required")
            identity = str(uuid4())
            item = {
                "id": identity,
                "identity": "console:" + identity,
                "name": body.name.strip(),
                "source": "console_added",
                "created_at": stamp(),
                "checks": {},
                "request_key": digest,
                "fingerprint": fingerprint,
                "generated": False,
            }
            for kind in ("business", "management"):
                url = getattr(body, kind + "_url")
                if not url:
                    continue
                secret = getattr(body, kind + "_token")
                generated = kind == "business" and body.setup == "new" and not secret
                token = (
                    secret.get_secret_value()
                    if secret
                    else (secrets.token_urlsafe(36) if generated else "")
                )
                if (
                    not token
                    or not 24 <= len(token) <= 1024
                    or any(ord(c) < 33 or ord(c) > 126 for c in token)
                ):
                    node_error("strong_connection_credential_required")
                item[kind] = {
                    "url": validate_url(url, management=kind == "management"),
                    "token": token,
                    "generated": generated,
                }
                item["generated"] = item["generated"] or generated
                if kind == "business":
                    item[kind]["model_name"] = body.model_name
            # Identical address/name is not identity. Surface a deliberate conflict
            # instead of silently taking over an existing computer or rotating its key.
            for other in [*self.imports.values(), *self.data["nodes"].values()]:
                if item["name"] == other["name"] or any(
                    item.get(k, {}).get("url")
                    and item.get(k, {}).get("url") == other.get(k, {}).get("url")
                    for k in ("business", "management")
                ):
                    node_error("possible_duplicate_node_review_existing", 409)
            self.data["nodes"][identity] = item
            self._install_connection(item)
            self._write()
            return self.view(item, session)

    def reveal(self, key):
        with self.lock:
            item = self.find(key)
            if item["source"] != "console_added" or not item.get("generated"):
                node_error("existing_credentials_not_revealable", 403)
            return {
                "business": item.get("business")
                if item.get("business", {}).get("generated")
                else None,
                # Management setup only accepts existing session credentials.
                # Never disclose one, including records from an older draft.
                "management": None,
                "no_store": True,
            }

    def test(self, key, session):
        with self.lock:
            item = self.find(key, session)
            lock = self.test_locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            node_error("node_test_in_progress", 409)
        try:
            result = {
                kind: probe_connection(item.get(kind), kind) for kind in ("business", "management")
            }
            with self.lock:
                item["checks"] = result
                self.data["nodes"][key] = item
                self._write()
                self.update_registered_status(item, session)
            return self.view(item, session)
        finally:
            lock.release()

    def register(self, key, session):
        with self.lock:
            item = self.find(key, session)
            self._install_connection(item)
            ref = item.get("connection_ref") or "console-node-" + item["id"]
            row = session.scalar(select(Node).where(Node.connection_ref == ref))
            if row is None:
                if session.scalar(select(Node).where(Node.name == item["name"])):
                    node_error("node_name_conflict", 409)
                row = Node(
                    id=str(uuid4()),
                    name=item["name"],
                    connection_ref=ref,
                    status="unchecked",
                    capabilities={},
                    last_error=None,
                )
                session.add(row)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    row = session.scalar(select(Node).where(Node.connection_ref == ref))
                    if not row:
                        node_error("node_registration_conflict", 409)
            self.data["nodes"][key] = item
            self._write()
            self.update_registered_status(item, session)
            return self.view(item, session)

    def add_management(self, key, body, session):
        with self.lock:
            item = self.find(key, session)
            if item.get("management"):
                node_error("existing_management_connection_not_overwritten", 409)
            token = body.token.get_secret_value()
            if not 24 <= len(token) <= 1024 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                node_error("strong_connection_credential_required")
            url = validate_url(body.url, management=True)
            for other in [*self.imports.values(), *self.data["nodes"].values()]:
                if other["id"] != key and other.get("management", {}).get("url") == url:
                    node_error("possible_duplicate_node_review_existing", 409)
            item["management"] = {"url": url, "token": token, "generated": False}
            self._install_connection(item)
            self.data["nodes"][key] = item
            self._write()
            self.update_registered_status(item, session)
            return self.view(item, session)

    def update_registered_status(self, item, session):
        ref = item.get("connection_ref") or "console-node-" + item["id"]
        row = session.scalar(select(Node).where(Node.connection_ref == ref))
        if row is None:
            return
        management = item.get("checks", {}).get("management", {})
        checked = management.get("observed_at")
        row.checked_at = datetime.fromisoformat(checked) if checked else None
        row.status = "ready" if management.get("status") == "available" else "unchecked"
        row.capabilities = {
            "rest": management.get("status") == "available",
            "rpc": management.get("checks", {}).get("rpc") == "read_only_ping_passed",
            "profile_create": item.get("profile_verification", {}).get("status", "not_tested"),
            "read_only": True,
            "business": item.get("checks", {}).get("business", {}).get("status", "not_tested"),
        }
        row.last_error = (
            None
            if management.get("status") == "available"
            else management.get("code", "management_not_configured")
        )
        session.commit()

    def verify_profile(self, key, session):
        with self.lock:
            item = self.find(key, session)
            endpoint = item.get("management")
            if (
                not endpoint
                or item.get("checks", {}).get("management", {}).get("status") != "available"
            ):
                node_error("management_read_only_check_required", 409)
            until = item["checks"]["management"].get("expires_at", "")
            if not until or datetime.fromisoformat(until) < datetime.now(UTC):
                node_error("management_check_expired", 409)
            lock = self.test_locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            node_error("node_test_in_progress", 409)
        try:
            # Validate and pin management host before the existing official client
            # is used. Connection enforces HTTPS (or loopback HTTP).
            name = "cfcheck-" + hashlib.sha256(item["identity"].encode()).hexdigest()[:20]
            owner = (
                "cf-console-validation:"
                + hashlib.sha256(item["identity"].encode()).hexdigest()[:32]
            )
            try:
                probe = PinnedProbe(endpoint["url"])

                class PinnedManagementNode(HermesNode):
                    def request(self, method, path, **kwargs):
                        try:
                            code, result = probe.request(
                                method, path, endpoint["token"], kwargs.get("json")
                            )
                            if code != 200:
                                raise NodeError("hermes_http_" + str(code))
                            if not isinstance(result, dict):
                                raise NodeError("node_invalid_json_object")
                            return result
                        except NodeError:
                            raise
                        except Exception as error:
                            raise NodeError(classify_exception(error)) from None

                client = PinnedManagementNode(Connection(endpoint["url"], endpoint["token"]))
                client.ensure_created(name, owner, initialize=False)
                result = check_result(
                    "verified",
                    "isolated_profile_creation_readback_passed",
                    actual_profile=name,
                    model_called=False,
                    cloned_existing_data=False,
                )
            except NodeError as error:
                result = check_result("failed", error.code, actual_profile=name, model_called=False)
            except Exception as error:
                result = check_result(
                    "failed", classify_exception(error), actual_profile=name, model_called=False
                )
            with self.lock:
                item["profile_verification"] = result
                self.data["nodes"][key] = item
                self._write()
                self.update_registered_status(item, session)
            return self.view(item, session)
        finally:
            lock.release()

    def filebrowser_status(self):
        # Cache per process, shared across administrators; no writes to the
        # actual file service and no fabricated mount/space/auth information.
        with self.lock:
            cached = getattr(self, "_filebrowser_check", None)
            if cached and time.monotonic() - cached[0] < 30:
                return cached[1]
            if not self.filebrowser:
                result = check_result("unknown", "filebrowser_not_configured")
            else:
                try:
                    peer = PinnedProbe(self.filebrowser["url"])
                    code, _ = peer.get(
                        self.filebrowser["health_path"], self.filebrowser.get("token", "")
                    )
                    result = check_result(
                        "available" if code == 200 else "failed",
                        "health_responded" if code == 200 else "health_http_" + str(code),
                    )
                except Exception as error:
                    result = check_result("failed", classify_exception(error))
            result.update(
                {
                    "authentication": "not_verified",
                    "storage": "not_collected",
                    "free_space": "not_collected",
                    "read_write": "not_collected",
                    "write_test": "not_run",
                }
            )
            self._filebrowser_check = (time.monotonic(), result)
            return result


def install_node_routes(app, config):
    registry = NodeRegistry(config)
    app.state.console_nodes = registry
    router = APIRouter(
        prefix="/admin/console/nodes",
        dependencies=[Depends(require_admin_role), Depends(enforce_admin_request_body_limit)],
    )
    db = Depends(get_database_session)

    @router.get("")
    def listing(session=db):
        return {
            "items": registry.listing(session),
            "tutorial": TUTORIAL,
            "probe_origin": "management_server",
            "candidate_only": True,
        }

    @router.get("/filebrowser-status")
    def filebrowser_status():
        return registry.filebrowser_status()

    @router.get("/{key}")
    def detail(key: str, session=db):
        return registry.view(registry.find(key, session), session)

    @router.post("/drafts")
    async def draft(request: Request, session=db):
        try:
            body = DraftInput.model_validate(await request.json())
        except (ValueError, ValidationError):
            node_error("invalid_node_configuration")
        return registry.draft(body, request.headers.get("Idempotency-Key"), session)

    @router.post("/{key}/reveal")
    def reveal(key: str):
        return registry.reveal(key)

    @router.post("/{key}/test")
    def test(key: str, session=db):
        return registry.test(key, session)

    @router.post("/{key}/register")
    def register(key: str, session=db):
        return registry.register(key, session)

    @router.post("/{key}/management")
    async def add_management(key: str, request: Request, session=db):
        try:
            body = ManagementConnectionInput.model_validate(await request.json())
        except (ValueError, ValidationError):
            node_error("invalid_node_configuration")
        return registry.add_management(key, body, session)

    @router.post("/{key}/verify-profile")
    async def verify_profile(key: str, request: Request, session=db):
        try:
            body = await request.json()
        except ValueError:
            node_error("explicit_profile_creation_consent_required")
        if body != {"acknowledge_creation": True}:
            node_error("explicit_profile_creation_consent_required")
        return await run_in_threadpool(registry.verify_profile, key, session)

    app.include_router(router)
    return registry
