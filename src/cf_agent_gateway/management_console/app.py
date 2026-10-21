from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from http.client import HTTPException as HTTPClientError
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from cf_agent_gateway.admin.auth import enforce_admin_request_body_limit, require_admin_role
from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.management_console import auth
from cf_agent_gateway.management_console.avatars import fetch_avatar
from cf_agent_gateway.management_console.live import install_live_routes
from cf_agent_gateway.management_console.models import DirectorySource
from cf_agent_gateway.management_console.nodes import install_node_routes
from cf_agent_gateway.management_console.service import ConsoleService
from cf_agent_gateway.management_console.service_control import install_service_routes
from cf_agent_gateway.management_console.upstream import SourceError
from cf_agent_gateway.profile_b.app import install_management
from cf_agent_gateway.profile_b.models import ManagementAudit
from cf_agent_gateway.profile_b.schemas import EmployeeUpdate
from cf_agent_gateway.profile_b.service import ManagementError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginInput(Strict):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class EnrollInput(EmployeeUpdate):
    employee_id: str | None = Field(default=None, max_length=128)
    existing_identity_id: str | None = Field(default=None, max_length=36)


class RoomInput(Strict):
    enabled: bool = True
    group_id: str | None = None


class BulkInput(Strict):
    ids: list[str] = Field(min_length=1, max_length=100)
    bot_allowed: bool | None = None
    group_allowed: bool | None = None
    group_id: str | None = None


def install_console(app, config):
    platform = "wechat-demo" if config.mode == "demo" else "wechat"
    install_management(
        app,
        config.hermes_connections,
        source=platform,
        accounts=lambda s: frozenset(s.scalars(select(DirectorySource.account_id))) - {None},
        allow_test_messages=False,
    )
    profile_state = app.state.profile_b_state
    live = install_live_routes(app, config)
    install_service_routes(app, config, live)
    install_node_routes(app, config)
    database_session = Depends(get_database_session)
    failed_logins = {}

    @app.middleware("http")
    async def console_auth(request: Request, call_next):
        path = request.url.path
        if path.startswith(("/admin/console", "/admin/profile-b")):
            cookie = (
                None if request.headers.get("Authorization") else request.cookies.get(auth.COOKIE)
            )
            with app.state.database_session_factory() as session:
                username = auth.authenticated(session, cookie)
            if username:
                request.state.roles = frozenset({"admin"})
                request.state.admin_subject = username
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    supplied = request.headers.get("X-CSRF-Token", "")
                    if not hmac.compare_digest(supplied, auth.csrf(cookie)):
                        return JSONResponse({"detail": "csrf_required"}, status_code=403)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                origin = request.headers.get("Origin")
                if origin and origin != str(request.base_url).rstrip("/"):
                    return JSONResponse({"detail": "cross_origin_rejected"}, status_code=403)
        response = await call_next(request)
        if path.startswith(("/admin/console", "/admin/profile-b")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        if path.startswith("/admin/console/") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            with app.state.database_session_factory() as session:
                session.add(
                    ManagementAudit(
                        id=str(uuid4()),
                        actor=getattr(request.state, "admin_subject", "unauthenticated"),
                        action="console." + request.method,
                        object_id=path.removeprefix("/admin/console/")[:128],
                        request_id=str(uuid4()),
                        result="http_" + str(response.status_code),
                        created_at=datetime.now(UTC),
                    )
                )
                session.commit()
        return response

    @app.exception_handler(SourceError)
    async def source_error(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    public = APIRouter(
        prefix="/admin/console", dependencies=[Depends(enforce_admin_request_body_limit)]
    )

    @public.post("/login")
    def login(body: LoginInput, request: Request, session=database_session):
        address = request.client.host if request.client else "unknown"
        cutoff = time.monotonic() - 300
        attempts = [stamp for stamp in failed_logins.get(address, []) if stamp > cutoff]
        if len(attempts) >= 8:
            raise HTTPException(429, "login_rate_limited")
        if not auth.verify_password(config.admin, body.username, body.password):
            failed_logins[address] = attempts + [time.monotonic()]
            raise HTTPException(401, "login_failed")
        failed_logins.pop(address, None)
        cookie = auth.login(session, body.username)
        response = JSONResponse(
            {"authenticated": True, "username": body.username, "csrf_token": auth.csrf(cookie)}
        )
        response.set_cookie(
            auth.COOKIE,
            cookie,
            httponly=True,
            samesite="strict",
            max_age=28800,
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @public.get("/me")
    def me(request: Request, session=database_session):
        cookie = request.cookies.get(auth.COOKIE)
        username = auth.authenticated(session, cookie)
        if not username:
            raise HTTPException(401, "login_required")
        return {"authenticated": True, "username": username, "csrf_token": auth.csrf(cookie)}

    app.include_router(public)
    router = APIRouter(
        prefix="/admin/console",
        dependencies=[Depends(require_admin_role), Depends(enforce_admin_request_body_limit)],
    )

    def console_service(request: Request, session=database_session):
        return ConsoleService(
            session, config, actor=getattr(request.state, "admin_subject", "admin-api")
        )

    service = Depends(console_service)

    @router.post("/logout")
    def logout(request: Request, session=database_session):
        from cf_agent_gateway.management_console.models import ConsoleSession

        cookie = request.cookies.get(auth.COOKIE, "")
        row = session.get(ConsoleSession, auth.session_key(cookie))
        if row:
            session.delete(row)
            session.commit()
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    @router.get("/state")
    def state(s: ConsoleService = service):
        return {
            "mode": config.mode,
            "candidate_only": True,
            "production_effective": False,
            "sources": [s.source_view(s.source(key)) for key in config.sources],
            "profile": profile_state(s=s.profiles()),
        }

    @router.post("/sources/{key}/sync")
    def sync_source(key: str, s: ConsoleService = service):
        return s.sync(key)

    @router.get("/contacts")
    def contacts(
        source: str,
        q: str = Query("", max_length=128),
        kind: str = "individual",
        linked: str = "all",
        authorized: str = "all",
        group_id: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=100),
        s: ConsoleService = service,
    ):
        return s.entries(
            source,
            q=q,
            kind=kind,
            linked=linked,
            authorized=authorized,
            group_id=group_id,
            offset=offset,
            limit=limit,
        )

    @router.get("/rooms")
    def rooms(
        source: str,
        q: str = Query("", max_length=128),
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=100),
        s: ConsoleService = service,
    ):
        return s.entries(source, q=q, kind="chatroom", offset=offset, limit=limit)

    @router.post("/contacts/bulk")
    def bulk(body: BulkInput, s: ConsoleService = service):
        entries = [s.entry(key) for key in dict.fromkeys(body.ids)]
        if any(s.employee(e) is None for e in entries):
            raise ManagementError("bulk_requires_linked_employees", 422)
        if "group_id" in body.model_fields_set:
            s.profiles().group(body.group_id, "employee")
        failures = []
        updated = 0
        for entry in entries:
            employee = s.employee(entry)
            values = {
                k: employee[k]
                for k in ("name", "enabled", "bot_allowed", "group_allowed", "group_id")
            }
            values.update({k: getattr(body, k) for k in body.model_fields_set - {"ids"}})
            try:
                s.enroll(entry.id, EnrollInput(**values))
                updated += 1
            except ManagementError as error:
                s.s.rollback()
                failures.append({"id": entry.id, "error": error.code})
        return JSONResponse(
            {"updated": updated, "failures": failures, "candidate_only": True},
            status_code=207 if failures else 200,
        )

    @router.get("/contacts/{key}")
    def contact(key: str, s: ConsoleService = service):
        return s.entry_view(s.entry(key))

    @router.post("/contacts/{key}/employee")
    def enroll(key: str, body: EnrollInput, s: ConsoleService = service):
        return s.enroll(key, body)

    @router.post("/rooms/{key}/settings")
    def group_settings(key: str, body: RoomInput, s: ConsoleService = service):
        return s.set_room(key, body)

    @router.get("/contacts/{key}/avatar")
    def avatar(key: str, s: ConsoleService = service):
        entry = s.entry(key)
        if config.mode == "demo" and entry.small_head_url == "demo:synthetic":
            colour = "#" + hashlib.sha256(entry.username.encode()).hexdigest()[:6]
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
                f'<rect width="64" height="64" rx="18" fill="{colour}"/>'
                '<circle cx="32" cy="24" r="10" fill="white"/>'
                '<path d="M14 56a18 18 0 0 1 36 0" fill="white"/></svg>'
            )
            return Response(svg, media_type="image/svg+xml")
        try:
            data, media_type = fetch_avatar(entry.small_head_url or "", config.runtime / "avatars")
            return Response(data, media_type=media_type)
        except (ValueError, OSError, TimeoutError, HTTPClientError):
            return Response(status_code=204)

    @router.get("/conversations")
    def conversations(
        source: str,
        q: str = Query("", max_length=128),
        offset: int = Query(0, ge=0),
        limit: int = Query(30, ge=1, le=100),
        s: ConsoleService = service,
    ):
        return s.conversations(source, q, offset, limit)

    @router.get("/conversations/{key}/messages")
    def messages(
        key: str,
        source: str,
        q: str = Query("", max_length=128),
        sender: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(40, ge=1, le=100),
        s: ConsoleService = service,
    ):
        return s.messages(
            key,
            source,
            q=q,
            sender=sender,
            date_from=date_from,
            date_to=date_to,
            offset=offset,
            limit=limit,
        )

    app.include_router(router)
    static = Path(__file__).with_name("static")
    app.mount("/console/assets", StaticFiles(directory=static), name="wechat-console-assets")

    @app.get("/console", include_in_schema=False)
    def console_page():
        return FileResponse(
            static / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            },
        )

    return app
