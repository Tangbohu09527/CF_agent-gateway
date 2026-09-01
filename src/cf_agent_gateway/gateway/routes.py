import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.gateway.security import require_api_token
from cf_agent_gateway.message.errors import ConversationTypeConflictError
from cf_agent_gateway.message.schemas import MessageCreated, MessageEvent, MessageResponse
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.runtime.health import DatabaseReadinessMonitor

logger = logging.getLogger(__name__)

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


class RuntimeHealthResponse(BaseModel):
    status: str
    checked_at: str
    components: dict[str, dict[str, object]]
    dispatch: dict[str, object]
    delivery: dict[str, object]


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/runtime", response_model=RuntimeHealthResponse, tags=["system"])
def runtime_health(request: Request, response: Response) -> RuntimeHealthResponse:
    service = getattr(request.app.state, "runtime_health", None)
    if service is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return RuntimeHealthResponse(
            status="unhealthy",
            checked_at="unavailable",
            components={
                "database": {"status": "unavailable"},
                "migration_schema": {"status": "unknown"},
            },
            dispatch={},
            delivery={},
        )
    snapshot = service.snapshot()
    if snapshot["status"] == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return RuntimeHealthResponse.model_validate(snapshot)


@router.get("/ready", response_model=HealthResponse, tags=["system"])
async def readiness(request: Request, response: Response) -> HealthResponse:
    if not getattr(request.app.state, "ready", False):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="not_ready")

    monitor: DatabaseReadinessMonitor | None = getattr(
        request.app.state,
        "database_readiness",
        None,
    )
    if monitor is None or not monitor.is_ready():
        logger.warning(
            "readiness check failed",
            extra={"fields": {"error_code": "database_unavailable"}},
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="not_ready")

    return HealthResponse(status="ready")


DatabaseSession = Annotated[Session, Depends(get_database_session)]


@router.post(
    "/internal/messages",
    response_model=MessageCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["messages"],
    dependencies=[Depends(require_api_token)],
)
def create_message(
    event: MessageEvent,
    response: Response,
    session: DatabaseSession,
) -> MessageCreated:
    try:
        message, created = MessageStore(session).create(event)
    except ConversationTypeConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": error.code,
                "source": error.source,
                "source_account_id": error.source_account_id,
                "conversation_id": error.conversation_id,
                "existing_type": error.existing_type,
                "requested_type": error.requested_type,
            },
        ) from error
    if not created:
        response.status_code = status.HTTP_200_OK
    return MessageCreated(id=message.id)


@router.get(
    "/messages/{message_id}",
    response_model=MessageResponse,
    tags=["messages"],
    dependencies=[Depends(require_api_token)],
)
def get_message(
    message_id: Annotated[int, Path(ge=1)],
    session: DatabaseSession,
) -> MessageResponse:
    message = MessageStore(session).get(message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    return MessageResponse.model_validate(message)


@router.get(
    "/sources/{source}/accounts/{source_account_id}/conversations/{conversation_id}/messages",
    response_model=list[MessageResponse],
    tags=["messages"],
    dependencies=[Depends(require_api_token)],
)
def get_conversation_messages(
    source: Annotated[str, Path(min_length=1, max_length=64)],
    source_account_id: Annotated[str, Path(min_length=1, max_length=255)],
    conversation_id: Annotated[str, Path(min_length=1, max_length=255)],
    session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> list[MessageResponse]:
    messages = MessageStore(session).list_for_conversation(
        source=source,
        source_account_id=source_account_id,
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
    )
    return [MessageResponse.model_validate(message) for message in messages]
