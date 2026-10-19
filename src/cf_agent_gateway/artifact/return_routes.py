"""Authenticated bytes, never an arbitrary path/URL or a caller-selected chat."""

import hashlib
import io
import re
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.adapters.wechat.media_http import _filename as validate_media_filename
from cf_agent_gateway.artifact.enterprise_exports import (
    EnterpriseExportTooLarge,
    export_metadata,
    fetch_verified,
    permitted,
)
from cf_agent_gateway.artifact.errors import (
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactStorageError,
    ArtifactStorageKeyError,
)
from cf_agent_gateway.artifact.models import ArtifactStatus
from cf_agent_gateway.artifact.repository import ArtifactRepository
from cf_agent_gateway.artifact.return_handoff import (
    ReturnDenied,
    authorize,
    authorize_snapshot,
    response_id_for,
    slot_id,
    validate_artifact_constraint,
)
from cf_agent_gateway.hermes.return_bridge.zip_contract import verify_zip
from cf_agent_gateway.message.models import Message

router = APIRouter(prefix="/internal/hermes/returns", tags=["artifact-return"])
NO_STORE = {"Cache-Control": "no-store"}


def _error(status, detail="artifact return unavailable"):
    return HTTPException(status, detail, headers=NO_STORE)


def _settings(request):
    settings = request.app.state.settings.artifact_return
    if not settings.enabled:
        raise _error(404)
    return settings


def _metadata(request, content):
    if request.query_params or request.headers.get("x-cf-return-intent") != "current-chat":
        raise _error(422, "explicit current-chat return required")
    try:
        encoded = request.headers.get("x-cf-filename", "")
        filename = validate_media_filename(unquote(encoded, encoding="utf-8", errors="strict"))
        if quote(filename, safe="") != encoded:
            raise ValueError()
        kind = request.headers.get("x-cf-artifact-kind")
        mime = request.headers.get("content-type")
        digest = request.headers.get("x-cf-content-sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError()
        size = int(request.headers["content-length"])
        if size != len(content) or size <= 0 or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError()
        is_pdf = (
            kind == "file"
            and mime == "application/pdf"
            and filename.lower().endswith(".pdf")
            and content.startswith(b"%PDF-")
            and b"%%EOF" in content[-1024:]
        )
        is_png = (
            kind == "image"
            and mime == "image/png"
            and filename.lower().endswith(".png")
            and content.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            and content.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        )
        is_zip = kind == "file" and mime == "application/zip" and filename.lower().endswith(".zip")
        if is_zip:
            verify_zip(io.BytesIO(content))
        if not (is_pdf or is_png or is_zip):
            raise ValueError()
    except (ValueError, UnicodeError, KeyError):
        raise _error(422, "invalid artifact metadata or content") from None
    return filename, kind, mime, size, digest


def _receipt(artifact):
    return {
        "artifact_id": artifact.artifact_id,
        "response_id": artifact.response_id,
        "status": artifact.status.value,
        "filename": artifact.filename,
        "kind": artifact.kind.value,
        "mime_type": artifact.mime_type,
        "size": artifact.size,
        "sha256": artifact.sha256,
    }


def _authorized_repository(request, session, dispatch_id, slot, settings):
    record, snapshot = authorize_snapshot(
        session,
        settings,
        dispatch_id,
        request.headers.get("authorization", ""),
    )
    response_id = response_id_for(record, snapshot)
    constraint = snapshot.get("original_attachment_constraint")
    if constraint is not None and slot != 0:
        raise ReturnDenied()
    if not 0 <= slot < settings.max_artifacts:
        raise _error(422, "invalid artifact slot")
    repository = ArtifactRepository(
        session,
        request.app.state.settings.artifact.storage_root,
        max_artifact_bytes=settings.max_bytes,
    )
    return repository, response_id, slot_id(response_id, slot), constraint


@router.put("/{dispatch_id}/artifacts/{slot}")
async def put_return_artifact(dispatch_id: int, slot: int, request: Request):
    settings = _settings(request)
    content = await request.body()  # bounded before parsing by the existing ASGI middleware
    if len(content) > settings.max_bytes:
        raise _error(413, "artifact too large")
    # Blocking disk/DB work runs outside the ASGI event loop, with one owned session.
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_put, request, settings, dispatch_id, slot, content)


def _put(request, settings, dispatch_id, slot, content):
    with request.app.state.database_session_factory() as session:
        try:
            repository, response_id, artifact_id, constraint = _authorized_repository(
                request,
                session,
                dispatch_id,
                slot,
                settings,
            )
            filename, kind, mime, size, digest = _metadata(request, content)
            validate_artifact_constraint(
                constraint, slot=slot, kind=kind, mime_type=mime, size=size, sha256=digest
            )
            existing = repository.get(artifact_id)
            if existing is not None:
                if (
                    existing.response_id,
                    existing.filename,
                    existing.kind.value,
                    existing.mime_type,
                    existing.size,
                    existing.sha256,
                    existing.status,
                ) != (response_id, filename, kind, mime, size, digest, ArtifactStatus.READY):
                    raise _error(409, "artifact slot conflict")
                repository.read(artifact_id)  # a corrupt READY row is never an idempotent success
                artifact = existing
            else:
                artifact = repository.create(
                    artifact_id=artifact_id,
                    response_id=response_id,
                    kind=kind,
                    filename=filename,
                    mime_type=mime,
                    content=content,
                    expected_size=size,
                    expected_sha256=digest,
                    commit=False,
                )
            receipt = _receipt(artifact)
            # Recheck time and claim after the disk write, before publishing READY.
            authorize(session, settings, dispatch_id, request.headers.get("authorization", ""))
            session.commit()
        except (ReturnDenied, MediaFetchError):
            session.rollback()
            raise _error(403) from None
        except (ArtifactIntegrityError, ArtifactStorageKeyError):
            session.rollback()
            raise _error(409, "artifact unavailable") from None
        except (ArtifactStorageError, OperationalError):
            session.rollback()
            raise HTTPException(
                503,
                "artifact storage temporarily unavailable",
                headers={
                    **NO_STORE,
                    "Retry-After": "1",
                },
            ) from None
        except ArtifactError:
            session.rollback()
            raise _error(409, "artifact unavailable") from None
    return JSONResponse(receipt, headers=NO_STORE)


@router.post("/{dispatch_id}/artifacts/{slot}/enterprise-import")
async def import_enterprise_artifact(dispatch_id: int, slot: int, request: Request):
    """A generated output of this current task; never a replacement original."""
    settings = _settings(request)
    if not request.app.state.settings.enterprise_exports.enabled:
        raise _error(404)
    if request.query_params or request.headers.get("x-cf-return-intent") != "current-chat":
        raise _error(422)
    try:
        export = export_metadata(await request.json())
    except (ValueError, TypeError, KeyError):
        raise _error(422) from None
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_import_enterprise, request, settings, dispatch_id, slot, export)


def _import_enterprise(request, settings, dispatch_id, slot, export):
    with request.app.state.database_session_factory() as session:
        try:
            repository, response_id, artifact_id, constraint = _authorized_repository(
                request, session, dispatch_id, slot, settings
            )
            # Original-file continuation is a different operation. It can never
            # acquire generated-output authority by selecting another MIME.
            if constraint is not None:
                raise ReturnDenied()
            record, _ = authorize_snapshot(
                session, settings, dispatch_id, request.headers.get("authorization", "")
            )
            message = session.get(Message, record.message_id, populate_existing=True)
            enterprise = request.app.state.settings.enterprise_exports
            if message.conversation_type != "private":
                raise ReturnDenied()
            permitted(
                enterprise,
                export,
                account=message.source_account_id,
                chat=message.conversation_id,
                identity=record.enterprise_identity_id,
            )
            if export["size_bytes"] > min(
                settings.max_bytes, request.app.state.settings.wechat.max_media_bytes
            ):
                raise EnterpriseExportTooLarge()
            expected = (
                response_id,
                export["name"],
                "file",
                "application/zip",
                export["size_bytes"],
                export["sha256"],
                ArtifactStatus.READY,
            )
            existing = repository.get(artifact_id)
            if existing is not None:
                if (
                    existing.response_id,
                    existing.filename,
                    existing.kind.value,
                    existing.mime_type,
                    existing.size,
                    existing.sha256,
                    existing.status,
                ) != expected:
                    raise _error(409, "artifact slot conflict")
                repository.read(artifact_id)
                artifact = existing
            else:

                def active():
                    authorize(
                        session, settings, dispatch_id, request.headers.get("authorization", "")
                    )

                with fetch_verified(enterprise, export, check_active=active) as content:
                    artifact = repository.create(
                        artifact_id=artifact_id,
                        response_id=response_id,
                        kind="file",
                        filename=export["name"],
                        mime_type="application/zip",
                        content=content,
                        expected_size=export["size_bytes"],
                        expected_sha256=export["sha256"],
                        commit=False,
                    )
            authorize(session, settings, dispatch_id, request.headers.get("authorization", ""))
            receipt = _receipt(artifact)
            session.commit()
            return JSONResponse(receipt, headers=NO_STORE)
        except (ReturnDenied, MediaFetchError):
            session.rollback()
            raise _error(403) from None
        except EnterpriseExportTooLarge:
            session.rollback()
            raise _error(413, "enterprise export too large") from None
        except (ValueError, TypeError, KeyError, ArtifactError):
            session.rollback()
            raise _error(409, "enterprise export unavailable or changed") from None
        except (OSError, httpx.HTTPError):
            session.rollback()
            raise _error(503, "enterprise export temporarily unavailable") from None


@router.get("/{dispatch_id}/artifacts/{slot}")
def get_return_artifact(dispatch_id: int, slot: int, request: Request):
    """Inspect an ambiguous PUT using the same live capability; never redownload bytes."""
    settings = _settings(request)
    with request.app.state.database_session_factory() as session:
        try:
            repository, _, artifact_id, constraint = _authorized_repository(
                request,
                session,
                dispatch_id,
                slot,
                settings,
            )
            artifact = repository.get(artifact_id)
            if artifact is None:
                raise _error(404)
            if artifact.status is not ArtifactStatus.READY:
                raise _error(409, "artifact unavailable")
            validate_artifact_constraint(
                constraint,
                slot=slot,
                kind=artifact.kind.value,
                mime_type=artifact.mime_type,
                size=artifact.size,
                sha256=artifact.sha256,
            )
            repository.read(artifact_id)
            authorize(session, settings, dispatch_id, request.headers.get("authorization", ""))
            receipt = _receipt(artifact)
        except (ReturnDenied, MediaFetchError):
            raise _error(403) from None
        except (ArtifactIntegrityError, ArtifactStorageKeyError):
            raise _error(409, "artifact unavailable") from None
        except (ArtifactStorageError, OperationalError):
            raise HTTPException(
                503,
                "artifact storage temporarily unavailable",
                headers={
                    **NO_STORE,
                    "Retry-After": "1",
                },
            ) from None
        except ArtifactError:
            raise _error(409, "artifact unavailable") from None
        finally:
            session.rollback()  # release the short inspection fence, no mutation
    return JSONResponse(receipt, headers=NO_STORE)
