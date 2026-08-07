from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.response.errors import ResponseValidationError
from cf_agent_gateway.response.store import DeliveryTarget, ResponseStore
from cf_agent_gateway.workspace.models import ThreadStatus, ThreadType
from cf_agent_gateway.workspace.store import WorkspaceStore


@dataclass(frozen=True, slots=True)
class ResponsePersistenceOutcome:
    response_id: str
    delivery_id: int
    created: bool


class ResponsePersistenceProcessor:
    """Convert a successful Hermes result into durable response and delivery records."""

    def __init__(
        self,
        session: Session,
        *,
        response_store: ResponseStore | None = None,
        message_store: MessageStore | None = None,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        self._session = session
        self._response_store = (
            response_store if response_store is not None else ResponseStore(session)
        )
        self._message_store = message_store if message_store is not None else MessageStore(session)
        self._workspace_store = (
            workspace_store if workspace_store is not None else WorkspaceStore(session)
        )

    def handle(self, response: HermesDispatchOutcome) -> None:
        self.process(response)

    def process(self, response: HermesDispatchOutcome) -> ResponsePersistenceOutcome:
        message = self._message_store.get(response.message_id)
        if message is None:
            raise ResponseValidationError("message_not_found")
        thread = self._workspace_store.get_thread(response.ai_thread_id)
        if thread is None:
            raise ResponseValidationError("ai_thread_not_found")
        self._session.refresh(thread)
        if (
            thread.thread_type is ThreadType.PRIVATE
            and thread.workspace_id != response.workspace_id
        ):
            raise ResponseValidationError("ai_thread_workspace_mismatch")
        if thread.status is not ThreadStatus.ACTIVE:
            raise ResponseValidationError("ai_thread_unavailable")
        if message.source != "wechat":
            raise ResponseValidationError("unsupported_channel")

        stored, delivery, created = self._response_store.save_generated(
            response,
            target=DeliveryTarget(
                channel=message.source,
                account_id=message.source_account_id,
                conversation_id=message.conversation_id,
            ),
        )
        return ResponsePersistenceOutcome(
            response_id=stored.response_id,
            delivery_id=delivery.id,
            created=created,
        )
