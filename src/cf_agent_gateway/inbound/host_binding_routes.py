import asyncio
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.inbound.host_binding import (
    SCHEMA,
    HostBindingError,
    _fence,
    _owner,
    _principal,
    digest,
    expire_bindings,
    lock_dispatch,
    resolve_binding,
    revoke_dispatch,
)
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.store import aware
from cf_agent_gateway.task.model.models import HermesDispatchRecord

router = APIRouter(prefix="/internal/hermes/inbound-bindings", tags=["host-binding"])
NO_STORE = {"Cache-Control": "no-store"}


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["cf-inbound-host-binding/v1"] = Field(alias="schema")
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:-]+$")
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:-]+$")
    host_instance_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    host_nonce: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class OwnerRequest(ResolveRequest):
    claim_epoch: str = Field(min_length=36, max_length=36)


def _denied():
    return HTTPException(403, "host binding unavailable", headers=NO_STORE)


def _authenticate(request):
    settings = request.app.state.settings
    host = settings.host_binding
    if not host.enabled:
        raise HTTPException(404, "host binding unavailable", headers=NO_STORE)
    expected = os.environ.get(host.service_token_env, "")
    supplied = request.headers.get("authorization", "")
    # This is a separate service identity, never the Gateway admin, FileBrowser
    # runtime token, or Hermes execution API key. No credential is a URL argument.
    reserved = (
        settings.api.token_env,
        settings.api.admin_token_env,
        settings.hermes.api_key_env,
        settings.wechat.token_env,
        "FILEBROWSER_RUNTIME_TOKEN",
    )
    if (
        len(expected) < 32
        or len(supplied) > 256
        or any(expected == os.environ.get(name) for name in reserved)
        or not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode())
    ):
        raise _denied()
    return host


@router.post("/resolve")
def resolve(request: Request, body: ResolveRequest):
    settings = _authenticate(request)
    with request.app.state.database_session_factory() as session:
        try:
            result = resolve_binding(
                session, settings, **body.model_dump(exclude={"schema_version"})
            )
        except (HostBindingError, MediaFetchError, ValueError, KeyError, TypeError):
            session.rollback()
            raise _denied() from None
    return JSONResponse(result, headers=NO_STORE)


def _owned(session, binding_id, settings, body):
    binding = session.get(InboundHostBinding, binding_id, populate_existing=True)
    if binding is None or binding.session_id != body.session_id:
        raise HostBindingError()
    lock_dispatch(session, binding.dispatch_id)
    session.refresh(binding)
    _principal(binding, settings)
    _owner(
        binding,
        task_id=body.task_id,
        host_instance_id=body.host_instance_id,
        host_nonce=body.host_nonce,
        claim_epoch=body.claim_epoch,
    )
    return binding


@router.post("/{binding_id}/closed")
def closed(binding_id: str, request: Request, body: OwnerRequest):
    settings = _authenticate(request)
    with request.app.state.database_session_factory() as session:
        try:
            binding = _owned(session, binding_id, settings, body)
            # Closing one's own still-running scope is cancellation. It can never
            # close another host's scope and remains safe after claim expiry.
            session.execute(
                update(InboundHostBinding)
                .where(
                    InboundHostBinding.id == binding.id,
                    InboundHostBinding.closed_at.is_(None),
                )
                .values(
                    state="closed",
                    closed_at=datetime.now(UTC),
                    grant_ciphertext=None,
                    sequence=InboundHostBinding.sequence + 1,
                    reason="host_closed",
                )
            )
            job = session.get(InboundMediaJob, binding.job_id)
            if job.read_claim_token and digest(job.read_claim_token) == binding.claim_token_hash:
                job.read_token_hash = job.read_claim_token = job.read_expires_at = None
            session.commit()
        except HostBindingError:
            session.rollback()
            raise _denied() from None
    return JSONResponse(
        {"schema": SCHEMA, "binding_id": binding_id, "state": "closed"}, headers=NO_STORE
    )


@router.get("/{binding_id}/events")
async def events(binding_id: str, request: Request):
    settings = _authenticate(request)
    try:
        body = OwnerRequest(
            **{
                "schema": SCHEMA,
                "session_id": request.headers.get("x-cf-session-id"),
                "task_id": request.headers.get("x-cf-task-id"),
                "host_instance_id": request.headers.get("x-cf-host-instance-id"),
                "host_nonce": request.headers.get("x-cf-host-nonce"),
                "claim_epoch": request.headers.get("x-cf-claim-epoch"),
            }
        )
        with request.app.state.database_session_factory() as session:
            binding = _owned(session, binding_id, settings, body)
            if request.headers.get("last-event-id", str(binding.sequence)) != str(binding.sequence):
                job = session.get(InboundMediaJob, binding.job_id)
                session.execute(
                    update(InboundHostBinding)
                    .where(
                        InboundHostBinding.id == binding.id,
                        InboundHostBinding.state == "running",
                    )
                    .values(
                        state="revoked",
                        reason="event_sequence_gap",
                        grant_ciphertext=None,
                        sequence=InboundHostBinding.sequence + 1,
                    )
                )
                if (
                    job.read_claim_token
                    and digest(job.read_claim_token) == binding.claim_token_hash
                ):
                    job.read_token_hash = job.read_claim_token = job.read_expires_at = None
                session.commit()
                raise HostBindingError()
            _fence(session, binding)
            result = session.execute(
                update(InboundHostBinding)
                .where(
                    InboundHostBinding.id == binding.id,
                    InboundHostBinding.events_connected.is_(False),
                    InboundHostBinding.state == "running",
                    InboundHostBinding.host_lease_until > datetime.now(UTC),
                )
                .values(events_connected=True)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise HostBindingError()
            session.commit()
    except (HostBindingError, ValueError):
        raise _denied() from None

    def snapshot():
        with request.app.state.database_session_factory() as session:
            expire_bindings(session, binding_id=binding_id)
            binding = session.get(InboundHostBinding, binding_id)
            return {
                "schema": SCHEMA,
                "binding_id": binding.id,
                "claim_epoch": binding.claim_epoch,
                "host_instance_id": binding.host_instance_id,
                "host_nonce": body.host_nonce,
                "sequence": binding.sequence,
                "state": binding.state,
                "lease_valid_until": aware(binding.host_lease_until).isoformat(),
                "reason": binding.reason,
            }

    async def stream():
        try:
            while True:
                payload = await asyncio.to_thread(snapshot)
                yield "id: {}\nevent: binding\ndata: {}\n\n".format(
                    payload["sequence"], json.dumps(payload, separators=(",", ":"))
                )
                if payload["state"] != "running" or await request.is_disconnected():
                    break
                # Repeated same-sequence snapshots are keepalives, never a new lease.
                await asyncio.sleep(0.25)
        finally:
            with request.app.state.database_session_factory() as session:
                binding = session.get(InboundHostBinding, binding_id)
                record_claim = session.scalar(
                    select(HermesDispatchRecord.claim_token).where(
                        HermesDispatchRecord.id == binding.dispatch_id
                    )
                )
                # Revoke this ID directly if the claim has changed; never revoke the new owner.
                if record_claim and digest(record_claim) == binding.claim_token_hash:
                    revoke_dispatch(
                        session,
                        binding.dispatch_id,
                        "events_disconnected",
                        claim_token=record_claim,
                    )
                else:
                    session.execute(
                        update(InboundHostBinding)
                        .where(
                            InboundHostBinding.id == binding_id,
                            InboundHostBinding.state == "running",
                        )
                        .values(
                            state="revoked",
                            reason="claim_changed",
                            grant_ciphertext=None,
                            sequence=InboundHostBinding.sequence + 1,
                        )
                    )
                session.commit()

    return StreamingResponse(stream(), media_type="text/event-stream", headers=NO_STORE)
