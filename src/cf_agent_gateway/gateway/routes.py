from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cf_agent_gateway.database import get_database_session
from cf_agent_gateway.message.schemas import MessageCreated, MessageEvent, MessageResponse
from cf_agent_gateway.message.store import MessageStore

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


DatabaseSession = Annotated[Session, Depends(get_database_session)]


@router.post(
    "/internal/messages",
    response_model=MessageCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["messages"],
)
def create_message(
    event: MessageEvent,
    response: Response,
    session: DatabaseSession,
) -> MessageCreated:
    message, created = MessageStore(session).create(event)
    if not created:
        response.status_code = status.HTTP_200_OK
    return MessageCreated(id=message.id)


@router.get(
    "/messages/{message_id}",
    response_model=MessageResponse,
    tags=["messages"],
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
)
def get_conversation_messages(
    source: str,
    source_account_id: str,
    conversation_id: str,
    session: DatabaseSession,
) -> list[MessageResponse]:
    messages = MessageStore(session).list_for_conversation(
        source=source,
        source_account_id=source_account_id,
        conversation_id=conversation_id,
    )
    return [MessageResponse.model_validate(message) for message in messages]
