"""Official Hermes plugin seams; no replacement Agent loop or HTTP proxy.

0f4a98f exposes native platform handlers and tool middleware. Its API worker
submission drops ContextVars and its TCP helper has no SSLContext argument.
The plugin uses asyncio's executor API and the SAME aiohttp Application/runner
to supply those two missing pieces. No Hermes function is monkey-patched.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import ipaddress
import json
import logging
import os
import sqlite3
import ssl
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, fields
from pathlib import Path

from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError
from cf_agent_gateway.hermes.return_bridge.protocol import (
    RETURN_ACK_HEADER,
    RETURN_AUTH_HEADER,
    RETURN_URL_HEADER,
    TOOL_NAME,
    TOOLSET,
)
from cf_agent_gateway.hermes.return_bridge.scope import ReturnScope, _claims, activate_scope

_execution = contextvars.ContextVar("cf_return_execution", default=None)
_tool_execution = contextvars.ContextVar("cf_return_tool_execution", default=None)
_APP_KEY = "cf_artifact_return_bridge"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeSettings:
    gateway_origin: str
    work_root: str
    profile_reference: str
    tls_host: str
    tls_port: int
    tls_cert_file: str
    tls_key_file: str
    gateway_ca_file: str | None = None
    profile_revision: int = 1
    request_timeout_seconds: int = 600

    def __post_init__(self):
        from urllib.parse import urlsplit

        origin = urlsplit(self.gateway_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
            or "%" in origin.netloc
            or "\\" in origin.netloc
            or any(ord(c) < 33 or ord(c) > 126 for c in self.gateway_origin)
        ):
            raise ValueError("invalid approved Gateway HTTPS origin")
        if not self.profile_reference or type(self.profile_revision) is not int:
            raise ValueError("approved execution Profile required")
        if self.profile_revision < 1 or not Path(self.work_root).is_absolute():
            raise ValueError("invalid return work root or Profile")
        if type(self.tls_port) is not int or not 0 <= self.tls_port <= 65535:
            raise ValueError("invalid return TLS listener")
        if self.tls_port == 0 and not _loopback(self.tls_host):
            raise ValueError("dynamic TLS port is allowed only for loopback tests")
        if (
            type(self.request_timeout_seconds) is not int
            or not 1 <= self.request_timeout_seconds <= 900
        ):
            raise ValueError("invalid return request deadline")
        if not self.tls_cert_file or not self.tls_key_file:
            raise ValueError("TLS certificate references required")


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ContextExecutor(ThreadPoolExecutor):
    """Preserve standard executor behavior while copying each submission context."""

    def submit(self, fn, /, *args, **kwargs):
        context = contextvars.copy_context()
        return super().submit(context.run, fn, *args, **kwargs)


def _check_root(root):
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts or root.drive.startswith("\\\\"):
        raise ReturnBridgeError("return_ledger_unavailable")
    for path in (root, *root.parents):
        info = path.lstat()
        if path.is_symlink() or path.is_junction() or not stat.S_ISDIR(info.st_mode):
            raise ReturnBridgeError("return_ledger_unavailable")


def _check_ledger_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        path.is_symlink()
        or path.is_junction()
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise ReturnBridgeError("return_ledger_unavailable")


@contextmanager
def _windows_pin(path, *, directory):
    """Prevent rename/replacement while SQLite opens the already checked path."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # OPEN_REPARSE_POINT; read/write sharing without FILE_SHARE_DELETE.
    handle = create(
        str(path),
        0x1 if directory else 0x80000000,
        0x3,
        None,
        3,
        0x00200000 | (0x02000000 if directory else 0),
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ReturnBridgeError("return_ledger_unavailable")
    try:

        class Attributes(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        attributes = Attributes()
        get_info = kernel.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if (
            not get_info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes))
            or attributes.attributes & 0x400
            or bool(attributes.attributes & 0x10) != directory
        ):
            raise ReturnBridgeError("return_ledger_unavailable")
        yield
    finally:
        close(handle)


@contextmanager
def _ledger_guard(root):
    # The approved private parent remains an administrative trust boundary, not
    # a sandbox against arbitrary programs running as the same operating user.
    _check_root(root)
    ledger = root / ".return-claims.sqlite3"
    with ExitStack() as stack:
        if os.name == "nt":
            for path in reversed((root, *root.parents)):
                stack.enter_context(_windows_pin(path, directory=True))
        try:
            descriptor = os.open(ledger, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        if os.name == "nt":
            stack.enter_context(_windows_pin(ledger, directory=False))
        _check_root(root)
        for suffix in ("", "-journal", "-wal", "-shm"):
            _check_ledger_file(Path(str(ledger) + suffix))
        yield ledger


def _consume_capability(root, authorization, request_id, expires):
    """Persist only a digest until expiry: a restart cannot reopen a spent request."""
    now = int(time.time())
    if not now < expires <= now + 3600:
        raise ReturnBridgeError("invalid_return_context")
    digest = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    try:
        with (
            _ledger_guard(root) as ledger,
            closing(sqlite3.connect(ledger.as_uri() + "?mode=rw", uri=True, timeout=2.0)) as db,
            db,
        ):
            db.execute("PRAGMA busy_timeout=2000")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS spent "
                "(digest TEXT PRIMARY KEY, request_id TEXT NOT NULL, expires INTEGER NOT NULL)"
            )
            db.execute("DELETE FROM spent WHERE expires <= ?", (now,))
            if db.execute("SELECT COUNT(*) FROM spent").fetchone()[0] >= 10000:
                raise ReturnBridgeError("return_capacity_exceeded")
            db.execute("INSERT INTO spent VALUES (?, ?, ?)", (digest, request_id, expires))
    except sqlite3.IntegrityError:
        raise ReturnBridgeError("return_context_consumed") from None
    except (sqlite3.Error, OSError):
        raise ReturnBridgeError("return_ledger_unavailable") from None


class Execution:
    def __init__(self, scope, session_id, observer):
        self.scope = scope
        self.session_id = session_id
        self.turn = None
        self._lock = threading.Lock()
        self.observer = observer

    def __repr__(self):
        return "<ReturnExecution>"

    def event(self, name, **payload):
        if self.observer is not None:
            # Observability receives only explicitly chosen non-secret fields.
            self.observer(name, {"request_id": self.scope.request_id, **payload})

    def bind(self, session_id, task_id, turn_id):
        self.scope.check_active()
        with self._lock:
            if session_id != self.session_id or not task_id or not turn_id:
                raise ReturnBridgeError("return_execution_mismatch")
            value = (session_id, task_id, turn_id)
            if self.turn is not None and self.turn != value:
                raise ReturnBridgeError("return_execution_mismatch")
            self.turn = value
        self.event("agent_bound", session_id=session_id, task_id=task_id, turn_id=turn_id)

    def check(self, session_id, task_id, turn_id):
        self.scope.check_active()
        if self.turn is None or self.turn != (session_id, task_id, turn_id):
            raise ReturnBridgeError("return_execution_mismatch")


def pre_llm_call(*, session_id="", task_id="", turn_id="", **_kwargs):
    current = _execution.get()
    if current is None:
        return None
    try:
        current.bind(session_id, task_id, turn_id)
    except ReturnBridgeError:
        current.scope.close()
        return None
    # This is non-sensitive tool guidance, not an authorization control.
    return {
        "context": (
            f"本次文件返回工具的授权工作目录是 {current.scope.work_root}。"
            f"仅在用户明确要求把文件或图片发回当前聊天时调用 {TOOL_NAME}，"
            "file_ref 使用该目录内的相对路径；普通读取不要调用。"
            "工具成功只表示交给 Gateway，仍等待任务最终成功及投递，不表示微信已实收。"
        )
    }


def tool_execution(
    *, tool_name, args, next_call, session_id="", task_id="", turn_id="", tool_call_id="", **_kwargs
):
    if tool_name != TOOL_NAME:
        return next_call(args)
    current = _execution.get()
    try:
        if current is None or not tool_call_id:
            raise ReturnBridgeError("return_execution_unavailable")
        current.check(session_id, task_id, turn_id)
        marker = _tool_execution.set((current, tool_call_id))
        try:
            current.event(
                "tool_start",
                tool=TOOL_NAME,
                tool_call_id=tool_call_id,
                session_id=session_id,
                task_id=task_id,
                turn_id=turn_id,
            )
            return next_call(args)
        finally:
            _tool_execution.reset(marker)
    except ReturnBridgeError as error:
        # Hermes skips middleware that raises; deny with a terminal safe result.
        return json.dumps({"success": False, "error": error.code})


def handle_return(args, **_kwargs):
    current = _execution.get()
    marker = _tool_execution.get()
    try:
        if current is None or marker is None or marker[0] is not current:
            raise ReturnBridgeError("return_execution_unavailable")
        if not isinstance(args, dict) or set(args) - {"file_ref", "kind", "filename"}:
            raise ReturnBridgeError("invalid_return_arguments")
        if not {"file_ref", "kind"} <= set(args):
            raise ReturnBridgeError("invalid_return_arguments")
        result = current.scope.return_file(**args)
        current.event("tool_result", tool=TOOL_NAME, tool_call_id=marker[1], result=result)
    except ReturnBridgeError as error:
        result = {"success": False, "error": error.code}
    except Exception:
        result = {"success": False, "error": "return_unavailable"}
    return json.dumps(result, ensure_ascii=False)


TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "将本次授权工作目录中的 PDF 文件或 PNG 图片交给 Gateway 返回原聊天。"
    "仅用于明确的返回请求；成功仅表示已交接，等待最终任务成功和投递。",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file_ref": {"type": "string", "description": "本次授权工作目录内相对文件路径"},
            "kind": {"type": "string", "enum": ["file", "image"]},
            "filename": {"type": "string", "description": "可选安全文件名，不含目录"},
        },
        "required": ["file_ref", "kind"],
    },
}


def configure_app(native, adapter, settings, *, prepare_scope=None, observer=None):
    """Official register_platform_handler factory; adapter fields stay read-only.

    prepare_scope/observer are explicit isolated-test callbacks, not config keys or
    remotely selectable functions. Production register(ctx) never supplies them.
    """
    from aiohttp import web

    if _APP_KEY in native:
        raise ValueError("artifact return already attached")
    if not _loopback(adapter._host):
        raise ValueError("official plaintext listener must be loopback before enabling return TLS")
    root = Path(settings.work_root)
    try:
        _check_root(root)
    except (ReturnBridgeError, OSError):
        raise ValueError("approved private task parent must already exist without links") from None
    state = {
        "ready": False,
        "site": None,
        "startup": None,
        "scopes": set(),
        "executor": None,
        "previous_executor": None,
    }
    native[_APP_KEY] = state

    async def start_tls():
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(settings.tls_cert_file, settings.tls_key_file)
            async with asyncio.timeout(5):
                while adapter._runner is None or adapter._runner.server is None:
                    await asyncio.sleep(0.01)
            site = web.TCPSite(
                adapter._runner, settings.tls_host, settings.tls_port, ssl_context=context
            )
            await site.start()
            state["site"] = site
            state["ready"] = True
        except Exception:
            state["ready"] = False
            logger.error("Artifact return TLS listener failed; capability requests remain disabled")
            if observer:
                observer("tls_start_failed", {})

    async def startup(_app):
        loop = asyncio.get_running_loop()
        previous = getattr(loop, "_default_executor", None)
        if isinstance(previous, ContextExecutor):
            state["executor"] = previous
            state["startup"] = asyncio.create_task(start_tls())
            return
        if previous is not None and type(previous) is not ThreadPoolExecutor:
            raise ValueError("custom executor requires explicit compatibility review")
        executor = ContextExecutor(
            max_workers=getattr(previous, "_max_workers", None), thread_name_prefix="cf-return"
        )
        state["previous_executor"] = previous
        state["executor"] = executor
        loop.set_default_executor(executor)
        # Existing standard-pool work drains; no running call is cancelled.
        if previous is not None:
            previous.shutdown(wait=False)
        state["startup"] = asyncio.create_task(start_tls())

    async def cleanup(_app):
        state["ready"] = False
        active = list(state["scopes"])
        # Revoke EVERY request before waiting for even one admitted exchange.
        # Otherwise a slow first drain could leave later requests authorized.
        for scope in active:
            scope.revoke()
        if active:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.to_thread(scope.close) for scope in active)), timeout=35
            )
        if state["startup"] is not None:
            state["startup"].cancel()
            await asyncio.gather(state["startup"], return_exceptions=True)
        if state["site"] is not None and state["site"] in adapter._runner.sites:
            await state["site"].stop()
        # The loop owns and shuts down its current standard executor. Do not
        # restore the previously drained pool, or cancel unrelated loop work.

    async def watch_disconnect(request):
        while True:
            if request.transport is None or request.transport.is_closing():
                return
            await asyncio.sleep(0.05)

    @web.middleware
    async def middleware(request, handler):
        present = RETURN_AUTH_HEADER in request.headers or RETURN_URL_HEADER in request.headers
        if not present:
            return await handler(request)

        def denied(status):
            return web.json_response(
                {"error": "artifact return unavailable"},
                status=status,
                headers={"Cache-Control": "no-store"},
            )

        if not adapter._expected_api_key():
            return denied(401)
        auth_error = adapter._check_auth(request)
        if auth_error is not None:
            return auth_error
        if not state["ready"] or not request.secure:
            return denied(503)
        if request.method != "POST" or request.raw_path != "/v1/chat/completions":
            return denied(400)
        if any(
            len(request.headers.getall(name, [])) != 1
            for name in (RETURN_AUTH_HEADER, RETURN_URL_HEADER, "X-Hermes-Session-Id")
        ):
            return denied(400)
        scope = None
        running = disconnected = None
        token = None
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("stream", False) is not False:
                return denied(400)
            session_id = request.headers["X-Hermes-Session-Id"]
            claims = _claims(request.headers[RETURN_AUTH_HEADER], session_id)
            if (claims.get("profile"), claims.get("revision")) != (
                settings.profile_reference,
                settings.profile_revision,
            ):
                return denied(403)
            request_id = uuid.uuid4().hex
            await asyncio.to_thread(
                _consume_capability,
                root,
                request.headers[RETURN_AUTH_HEADER],
                request_id,
                claims["expires"],
            )
            work_root = root / request_id
            work_root.mkdir(mode=0o700)
            scope = ReturnScope(
                return_url=request.headers[RETURN_URL_HEADER],
                authorization=request.headers[RETURN_AUTH_HEADER],
                gateway_origin=settings.gateway_origin,
                request_id=request_id,
                work_root=work_root,
                session_id=session_id,
                ca_file=settings.gateway_ca_file,
            )
            state["scopes"].add(scope)
            current = Execution(scope, session_id, observer)
            current.event("request_scope", session_id=session_id, work_root=str(work_root))
            if prepare_scope is not None:
                prepare_scope(scope)
            token = _execution.set(current)
            with activate_scope(scope):
                running = asyncio.create_task(handler(request))
                disconnected = asyncio.create_task(watch_disconnect(request))
                done, _ = await asyncio.wait(
                    (running, disconnected),
                    timeout=settings.request_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if running not in done:
                    return denied(504)
                response = running.result()
                scope.check_active()
                if current.turn is None:
                    # An idempotency-cache hit did not execute this request's Agent.
                    return denied(409)
                response.headers[RETURN_ACK_HEADER] = scope.accepted_ack
                current.event(
                    "completion_ack",
                    session_id=session_id,
                    turn_id=current.turn[2],
                    status=response.status,
                )
                return response
        except ReturnBridgeError as error:
            return denied(409 if error.code == "return_context_consumed" else 403)
        except (ValueError, TypeError, KeyError):
            return denied(403)
        finally:
            if scope is not None:
                # Revoke immediately; run bounded network drain outside the event loop.
                scope.revoke()
                await asyncio.to_thread(scope.close)
                state["scopes"].discard(scope)
                if observer:
                    observer("request_closed", {"request_id": scope.request_id})
            for task in (running, disconnected):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (running, disconnected) if t is not None), return_exceptions=True
            )
            if token is not None:
                _execution.reset(token)

    native.middlewares.append(middleware)
    native.on_startup.append(startup)
    native.on_cleanup.append(cleanup)
    return state


def setup(ctx, *, prepare_scope=None, observer=None):
    """Called by the official plugin loader's register(ctx); disabled unless configured."""
    if ctx.get_config("enabled", False) is not True:
        return
    values = {
        field.name: ctx.get_config(field.name, field.default) for field in fields(BridgeSettings)
    }
    settings = BridgeSettings(**values)

    def factory(native, adapter):
        configure_app(native, adapter, settings, prepare_scope=prepare_scope, observer=observer)

    ctx.register_platform_handler("api_server", factory)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_middleware("tool_execution", tool_execution)
    ctx.register_tool(name=TOOL_NAME, toolset=TOOLSET, schema=TOOL_SCHEMA, handler=handle_return)
