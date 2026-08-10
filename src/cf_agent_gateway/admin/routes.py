from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from cf_agent_gateway.admin.auth import require_admin_role
from cf_agent_gateway.admin.schemas import (
    AdminConversationItem,
    AdminDeliveryItem,
    AdminMessageItem,
    AdminPage,
    AdminThreadDetail,
)
from cf_agent_gateway.admin.store import AdminArchiveStore, AdminQuery
from cf_agent_gateway.database import get_database_session

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_role)],
)

DatabaseSession = Annotated[Session, Depends(get_database_session)]


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
    thread_id: str,
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
