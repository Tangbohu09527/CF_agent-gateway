"""Explicit opt-in management surface, using the existing Gateway app and admin auth."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from cf_agent_gateway.access.policy_models import UserAccessPolicy
from cf_agent_gateway.admin.auth import enforce_admin_request_body_limit, require_admin_role
from cf_agent_gateway.config import DatabaseSettings, ServerSettings, Settings
from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.identity.models import EnterpriseIdentity, SourceIdentityMapping
from cf_agent_gateway.message.models import Conversation
from cf_agent_gateway.profile_b.hermes import Connection
from cf_agent_gateway.profile_b.models import (
    DerivedProfile,
    EmployeeSettings,
    ManagedSession,
    ManagementAudit,
    ManagementGroup,
    Node,
    RoomSettings,
    Template,
)
from cf_agent_gateway.profile_b.schemas import (
    DeriveInput,
    EmployeeInput,
    EmployeeUpdate,
    GroupInput,
    NodeInput,
    RoomInput,
    RoomUpdate,
    TemplateInput,
    TestMessage,
)
from cf_agent_gateway.profile_b.service import SOURCE, ManagementError, Service
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread


def install_management(app, connections: dict[str, Connection]):
    """Only the operator can install connection refs; URLs and secrets never enter the UI."""
    router = APIRouter(
        prefix="/admin/profile-b",
        dependencies=[
            Depends(require_admin_role),
            Depends(enforce_admin_request_body_limit),
        ],
    )

    database_session = Depends(get_database_session)

    @app.middleware("http")
    async def audit_management_request(request: Request, call_next):
        writing = request.url.path.startswith("/admin/profile-b/") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }
        request_id = str(uuid4())
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            if writing:
                with app.state.database_session_factory() as session:
                    session.add(
                        ManagementAudit(
                            id=str(uuid4()),
                            actor=getattr(request.state, "profile_b_actor", "unauthenticated"),
                            action="http." + request.method,
                            object_id=request.url.path.removeprefix("/admin/profile-b/")[:128],
                            request_id=request_id,
                            result="http_" + str(status_code),
                            created_at=datetime.now(UTC),
                        )
                    )
                    session.commit()

    def service(request: Request, session=database_session):
        # Existing auth establishes admin. Fingerprint attributes actions to the credential
        # without recording it. A trusted role middleware may provide its own subject.
        actor = getattr(request.state, "admin_subject", None)
        if not actor:
            credential = request.headers.get("authorization", "trusted-role-middleware")
            actor = "admin:" + hashlib.sha256(credential.encode()).hexdigest()[:16]
        request.state.profile_b_actor = actor
        return Service(session, connections, actor=actor)

    admin_service = Depends(service)

    @app.exception_handler(ManagementError)
    async def management_error(_request, error):
        return JSONResponse(status_code=error.status, content={"detail": error.code})

    @router.get("/state")
    def state(s: Service = admin_service):
        def records(model, fields):
            return [
                {key: getattr(item, key) for key in fields} for item in s.s.scalars(select(model))
            ]

        employees = []
        for item in s.s.scalars(select(EmployeeSettings)):
            identity = s.get(EnterpriseIdentity, item.identity_id)
            policy = s.s.scalar(
                select(UserAccessPolicy).where(
                    UserAccessPolicy.enterprise_identity_id == identity.id
                )
            )
            mapping = s.s.scalar(
                select(SourceIdentityMapping).where(
                    SourceIdentityMapping.platform == SOURCE,
                    SourceIdentityMapping.enterprise_identity_id == identity.id,
                )
            )
            employees.append(
                {
                    "id": identity.id,
                    "employee_id": identity.employee_id,
                    "name": identity.display_name,
                    "enabled": identity.status.value == "active",
                    "bot_allowed": bool(policy and policy.enabled),
                    "group_allowed": item.group_allowed,
                    "group_id": item.group_id,
                    "account_id": mapping.account_id if mapping else None,
                    "sender_id": mapping.sender_id if mapping else None,
                }
            )
        rooms = []
        for item in s.s.scalars(select(RoomSettings)):
            conv = s.get(Conversation, item.conversation_id)
            rooms.append(
                {
                    "id": str(conv.id),
                    "name": conv.conversation_name,
                    "account_id": conv.source_account_id,
                    "conversation_id": conv.conversation_id,
                    "enabled": item.enabled,
                    "group_id": item.group_id,
                }
            )
        sessions = records(
            ManagedSession,
            [
                "conversation_id",
                "derived_id",
                "thread_id",
                "first_template_id",
                "first_version",
                "effective_status",
            ],
        )
        for item in sessions:
            item["hermes_session_id"] = s.get(AIThread, item["thread_id"]).hermes_thread_id
        audits = list(
            s.s.scalars(
                select(ManagementAudit).order_by(ManagementAudit.created_at.desc()).limit(40)
            )
        )
        return {
            "test_data": True,
            "source": SOURCE,
            "connection_refs": sorted(connections),
            "employees": employees,
            "rooms": rooms,
            "templates": records(Template, ["id", "name", "kind", "version", "soul", "language"]),
            "nodes": records(
                Node,
                [
                    "id",
                    "name",
                    "connection_ref",
                    "status",
                    "capabilities",
                    "last_error",
                    "checked_at",
                ],
            ),
            "groups": records(ManagementGroup, ["id", "name", "kind", "template_id", "node_id"]),
            "derived": records(
                DerivedProfile,
                [
                    "id",
                    "kind",
                    "identity_id",
                    "conversation_id",
                    "node_id",
                    "template_id",
                    "actual_profile",
                    "desired_version",
                    "applied_template_id",
                    "applied_version",
                    "status",
                    "last_error",
                    "operation_until",
                ],
            ),
            "sessions": sessions,
            "in_flight": [
                {"id": t.id, "status": t.status.value, "thread_id": t.ai_thread_id}
                for t in s.s.scalars(
                    select(HermesDispatchRecord).where(
                        HermesDispatchRecord.status.in_(
                            [HermesDispatchStatus.RUNNING, HermesDispatchStatus.UNCERTAIN]
                        )
                    )
                )
            ],
            "audits": [
                {
                    "actor": a.actor,
                    "action": a.action,
                    "object_id": a.object_id,
                    "request_id": a.request_id,
                    "result": a.result,
                    "version": a.version,
                    "time": a.created_at,
                }
                for a in audits
            ],
        }

    @router.post("/employees")
    def create_employee(body: EmployeeInput, s: Service = admin_service):
        return {"id": s.save_employee(body)}

    @router.put("/employees/{key}")
    def update_employee(key: str, body: EmployeeUpdate, s: Service = admin_service):
        return {"id": s.save_employee(body, key)}

    @router.post("/templates")
    def create_template(body: TemplateInput, s: Service = admin_service):
        return {"id": s.save_template(body)}

    @router.put("/templates/{key}")
    def update_template(key: str, body: TemplateInput, s: Service = admin_service):
        return {"id": s.save_template(body, key)}

    @router.post("/nodes")
    def create_node(body: NodeInput, s: Service = admin_service):
        return {"id": s.save_node(body)}

    @router.post("/nodes/{key}/check")
    def check_node(key: str, s: Service = admin_service):
        return s.check_node(key)

    @router.post("/groups")
    def create_group(body: GroupInput, s: Service = admin_service):
        return {"id": s.save_group(body)}

    @router.put("/groups/{key}")
    def update_group(key: str, body: GroupInput, s: Service = admin_service):
        return {"id": s.save_group(body, key)}

    @router.post("/rooms")
    def create_room(body: RoomInput, s: Service = admin_service):
        return {"id": s.save_room(body)}

    @router.put("/rooms/{key}")
    def update_room(key: int, body: RoomUpdate, s: Service = admin_service):
        return {"id": s.save_room(body, key)}

    @router.post("/profiles/ensure")
    def derive(body: DeriveInput, s: Service = admin_service):
        key = s.derive(body.kind, body.subject_id)
        binding = s.get(DerivedProfile, key)
        return {"id": key, "status": binding.status, "error": binding.last_error}

    @router.post("/test-messages")
    def test_message(body: TestMessage, s: Service = admin_service):
        return s.send(body)

    app.include_router(router)
    static = Path(__file__).with_name("static")
    app.mount("/manage/assets", StaticFiles(directory=static), name="profile-b-assets")

    @app.get("/manage", include_in_schema=False)
    def management_page():
        return FileResponse(
            static / "index.html",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
                ),
            },
        )

    return app


def main():
    runtime = Path(os.environ["CF_PROFILE_B_RUNTIME"]).resolve()
    connections = {
        key: Connection(**value)
        for key, value in json.loads(
            (runtime / "connections.json").read_text(encoding="utf-8")
        ).items()
    }
    settings = Settings(
        server=ServerSettings(
            host="127.0.0.1", port=int(os.environ.get("CF_PROFILE_B_WEB_PORT", "18760"))
        ),
        database=DatabaseSettings(url="sqlite:///" + (runtime / "gateway.db").as_posix()),
    )
    app = install_management(create_app(settings), connections)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("CF_PROFILE_B_WEB_PORT", "18760")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
