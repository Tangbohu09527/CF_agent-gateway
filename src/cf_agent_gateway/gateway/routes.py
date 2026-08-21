from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.gateway.security import require_message_api_bearer
from cf_agent_gateway.message.errors import ConversationTypeConflictError
from cf_agent_gateway.message.schemas import MessageCreated, MessageEvent, MessageResponse
from cf_agent_gateway.message.store import (
    DEFAULT_CONVERSATION_MESSAGE_LIMIT,
    MAX_CONVERSATION_MESSAGE_LIMIT,
    MAX_CONVERSATION_MESSAGE_OFFSET,
    MessageStore,
)
from cf_agent_gateway.runtime.health import HealthResponse, check_runtime_health

router = APIRouter()
DatabaseSession = Annotated[Session, Depends(get_database_session)]


@router.get(
    "/health",
    response_model=HealthResponse,
    response_model_exclude_none=True,
    tags=["system"],
)
def health(
    request: Request,
    response: Response,
    session: DatabaseSession,
) -> HealthResponse:
    result = check_runtime_health(session, request.app.state.settings)
    if result.status == "degraded":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return result


@router.post(
    "/internal/messages",
    response_model=MessageCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["messages"],
    dependencies=[Depends(require_message_api_bearer)],
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
    dependencies=[Depends(require_message_api_bearer)],
)
def get_message(message_id: int, session: DatabaseSession) -> MessageResponse:
    message = MessageStore(session).get(message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    return MessageResponse.model_validate(message)


@router.get(
    "/sources/{source}/accounts/{source_account_id}/conversations/{conversation_id}/messages",
    response_model=list[MessageResponse],
    tags=["messages"],
    dependencies=[Depends(require_message_api_bearer)],
)
def get_conversation_messages(
    source: str,
    source_account_id: str,
    conversation_id: str,
    session: DatabaseSession,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_CONVERSATION_MESSAGE_LIMIT),
    ] = DEFAULT_CONVERSATION_MESSAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_CONVERSATION_MESSAGE_OFFSET)] = 0,
) -> list[MessageResponse]:
    messages = MessageStore(session).list_for_conversation(
        source=source,
        source_account_id=source_account_id,
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
    )
    return [MessageResponse.model_validate(message) for message in messages]
