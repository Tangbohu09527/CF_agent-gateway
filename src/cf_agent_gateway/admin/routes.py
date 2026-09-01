import hashlib
import logging
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.orm import Session

from cf_agent_gateway.admin.auth import (
    enforce_admin_request_body_limit,
    require_admin_role,
)
from cf_agent_gateway.admin.recovery import (
    DispatchRecoveryError,
    DispatchRecoveryNotFoundError,
    DispatchRecoveryStore,
)
from cf_agent_gateway.admin.schemas import (
    AdminConversationItem,
    AdminDeliveryItem,
    AdminDispatchInspection,
    AdminDispatchRecoveryRequest,
    AdminDispatchRecoveryResult,
    AdminMessageItem,
    AdminPage,
    AdminThreadDetail,
)
from cf_agent_gateway.admin.store import AdminArchiveStore, AdminQuery
from cf_agent_gateway.config import Settings
from cf_agent_gateway.database import get_database_session

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[
        Depends(require_admin_role),
        Depends(enforce_admin_request_body_limit),
    ],
)

logger = logging.getLogger(__name__)

DatabaseSession = Annotated[Session, Depends(get_database_session)]
DispatchRecordId = Annotated[int, Path(ge=1)]
ThreadId = Annotated[str, Path(min_length=1, max_length=36)]
_MIN_SECRET_LENGTH = 8


def _validated_recovery_request(
    request: Request,
    payload: AdminDispatchRecoveryRequest,
) -> AdminDispatchRecoveryRequest:
    settings: Settings = request.app.state.settings
    secrets = _configured_recovery_secrets(settings)
    if any(
        secret in value
        for secret in secrets
        for value in (payload.operator, payload.reference, payload.reason)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recovery metadata may not contain secret material",
        )
    return payload


def _configured_recovery_secrets(settings: Settings) -> tuple[str, ...]:
    environment_names = (
        settings.api.admin_token_env,
        settings.api.token_env,
        settings.wechat.token_env,
        settings.hermes.api_key_env,
    )
    secrets: set[str] = set()
    for environment_name in dict.fromkeys(environment_names):
        raw_value = os.getenv(environment_name)
        if raw_value is None:
            continue
        for candidate in (raw_value, raw_value.strip()):
            if len(candidate) >= _MIN_SECRET_LENGTH and candidate.strip():
                secrets.add(candidate)
    return tuple(secrets)


ValidatedRecoveryRequest = Annotated[
    AdminDispatchRecoveryRequest,
    Depends(_validated_recovery_request),
]


def admin_query(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    identity_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    source: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    source_account_id: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    conversation_id: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminQuery:
    if start_time is not None and end_time is not None:
        try:
            invalid_interval = start_time >= end_time
        except TypeError:
            invalid_interval = True
        if invalid_interval:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start_time must be earlier than end_time",
            )
    return AdminQuery(
        start_time=start_time,
        end_time=end_time,
        identity_id=identity_id,
        source=source,
        source_account_id=source_account_id,
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
    )


AdminQueryParameters = Annotated[AdminQuery, Depends(admin_query)]


@router.get("/conversations", response_model=AdminPage[AdminConversationItem])
def get_admin_conversations(
    query: AdminQueryParameters,
    session: DatabaseSession,
) -> AdminPage[AdminConversationItem]:
    return AdminArchiveStore(session).list_conversations(query)


@router.get("/messages", response_model=AdminPage[AdminMessageItem])
def get_admin_messages(
    query: AdminQueryParameters,
    session: DatabaseSession,
) -> AdminPage[AdminMessageItem]:
    return AdminArchiveStore(session).list_messages(query)


@router.get("/threads/{thread_id}", response_model=AdminThreadDetail)
def get_admin_thread(
    thread_id: ThreadId,
    query: AdminQueryParameters,
    session: DatabaseSession,
) -> AdminThreadDetail:
    thread = AdminArchiveStore(session).get_thread(thread_id, query)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
    return thread


@router.get("/deliveries", response_model=AdminPage[AdminDeliveryItem])
def get_admin_deliveries(
    query: AdminQueryParameters,
    session: DatabaseSession,
) -> AdminPage[AdminDeliveryItem]:
    return AdminArchiveStore(session).list_deliveries(query)


@router.get(
    "/dispatches/{dispatch_record_id}",
    response_model=AdminDispatchInspection,
)
def inspect_admin_dispatch(
    dispatch_record_id: DispatchRecordId,
    session: DatabaseSession,
) -> AdminDispatchInspection:
    inspection = DispatchRecoveryStore(session).inspect(dispatch_record_id)
    if inspection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="dispatch record not found",
        )
    return AdminDispatchInspection.model_validate(inspection)


@router.post(
    "/dispatches/{dispatch_record_id}/retry-approved",
    response_model=AdminDispatchRecoveryResult,
)
def retry_approved_admin_dispatch(
    dispatch_record_id: DispatchRecordId,
    request: ValidatedRecoveryRequest,
    session: DatabaseSession,
) -> AdminDispatchRecoveryResult:
    try:
        result = DispatchRecoveryStore(session).retry_approved(
            dispatch_record_id,
            operator=request.operator,
            reference=request.reference,
            reason=request.reason,
        )
    except DispatchRecoveryError as error:
        raise _recovery_http_error(error) from None
    _log_recovery(
        result.action.value, result.status.value, result.idempotent, request, result.audit_id
    )
    return AdminDispatchRecoveryResult.model_validate(result)


@router.post(
    "/dispatches/{dispatch_record_id}/mark-dead",
    response_model=AdminDispatchRecoveryResult,
)
def mark_dead_admin_dispatch(
    dispatch_record_id: DispatchRecordId,
    request: ValidatedRecoveryRequest,
    session: DatabaseSession,
) -> AdminDispatchRecoveryResult:
    try:
        result = DispatchRecoveryStore(session).mark_dead(
            dispatch_record_id,
            operator=request.operator,
            reference=request.reference,
            reason=request.reason,
        )
    except DispatchRecoveryError as error:
        raise _recovery_http_error(error) from None
    _log_recovery(
        result.action.value, result.status.value, result.idempotent, request, result.audit_id
    )
    return AdminDispatchRecoveryResult.model_validate(result)


@router.post(
    "/dispatches/{dispatch_record_id}/confirm-success",
    response_model=AdminDispatchRecoveryResult,
)
def confirm_success_admin_dispatch(
    dispatch_record_id: DispatchRecordId,
    request: ValidatedRecoveryRequest,
    session: DatabaseSession,
) -> AdminDispatchRecoveryResult:
    try:
        result = DispatchRecoveryStore(session).confirm_success(
            dispatch_record_id,
            operator=request.operator,
            reference=request.reference,
            reason=request.reason,
        )
    except DispatchRecoveryError as error:
        raise _recovery_http_error(error) from None
    _log_recovery(
        result.action.value, result.status.value, result.idempotent, request, result.audit_id
    )
    return AdminDispatchRecoveryResult.model_validate(result)


def _recovery_http_error(error: DispatchRecoveryError) -> HTTPException:
    status_code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(error, DispatchRecoveryNotFoundError)
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=status_code, detail=error.code)


def _log_recovery(
    action: str,
    resulting_status: str,
    idempotent: bool,
    request: AdminDispatchRecoveryRequest,
    audit_id: int,
) -> None:
    logger.info(
        "admin recovery action",
        extra={
            "fields": {
                "action": action,
                "resulting_status": resulting_status,
                "idempotent": idempotent,
                "audit_id": audit_id,
                "operator_ref": _opaque_reference(request.operator),
                "request_ref": _opaque_reference(request.reference),
            }
        },
    )


def _opaque_reference(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
