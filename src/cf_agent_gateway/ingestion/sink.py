from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.adapters.wechat import NormalizedWechatMessage
from cf_agent_gateway.hermes import HermesDispatcher
from cf_agent_gateway.ingestion.models import MessageIngestionOutcome
from cf_agent_gateway.ingestion.service import AdmissionRequestResolver, MessageAdmissionService


class MessageStoreAdmissionSink:
    """Polling-compatible sink that preserves all storage and admission failures."""

    def __init__(self, service: MessageAdmissionService) -> None:
        self._service = service

    def handle(self, message: NormalizedWechatMessage) -> None:
        self.process(message)

    def process(self, message: NormalizedWechatMessage) -> MessageIngestionOutcome:
        return self._service.process(message)


class SessionFactoryMessageStoreAdmissionSink:
    """Run each message through admission with a fresh database session."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        request_resolver: AdmissionRequestResolver | None = None,
        *,
        v2_routing_enabled: bool = False,
        hermes_dispatcher_factory: Callable[[Session], HermesDispatcher] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._request_resolver = request_resolver
        self._hermes_dispatcher_factory = hermes_dispatcher_factory
        self._v2_routing_enabled = v2_routing_enabled

    def handle(self, message: NormalizedWechatMessage) -> None:
        self.process(message)

    def process(self, message: NormalizedWechatMessage) -> MessageIngestionOutcome:
        session = self._session_factory()
        try:
            hermes_dispatcher = (
                self._hermes_dispatcher_factory(session)
                if self._hermes_dispatcher_factory is not None
                else None
            )
            outcome = MessageAdmissionService(
                session,
                request_resolver=self._request_resolver,
                v2_routing_enabled=self._v2_routing_enabled,
                hermes_dispatcher=hermes_dispatcher,
            ).process(message)
        except Exception:
            # Preserve the processing error even if cleanup also encounters a failure.
            with suppress(Exception):
                session.rollback()
            with suppress(Exception):
                session.close()
            raise
        session.close()
        return outcome
