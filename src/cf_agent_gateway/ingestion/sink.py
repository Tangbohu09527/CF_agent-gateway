from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.adapters.wechat import NormalizedWechatMessage
from cf_agent_gateway.hermes import HermesDispatcher
from cf_agent_gateway.ingestion.models import MessageIngestionOutcome
from cf_agent_gateway.ingestion.service import AdmissionRequestResolver, MessageAdmissionService

logger = logging.getLogger(__name__)


def _rollback_session(session: Session) -> None:
    try:
        session.rollback()
    except Exception:
        logger.warning(
            "ingestion cleanup failed",
            extra={
                "fields": {
                    "component": "database_session",
                    "error_code": "session_rollback_cleanup_failed",
                }
            },
        )


def _close_session(session: Session) -> None:
    try:
        session.close()
    except Exception:
        logger.warning(
            "ingestion cleanup failed",
            extra={
                "fields": {
                    "component": "database_session",
                    "error_code": "session_close_cleanup_failed",
                }
            },
        )


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
        hermes_dispatcher_factory: Callable[[Session], HermesDispatcher] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._request_resolver = request_resolver
        self._hermes_dispatcher_factory = hermes_dispatcher_factory

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
                hermes_dispatcher=hermes_dispatcher,
            ).process(message)
        except Exception:
            # Preserve the processing error even if cleanup also encounters a failure.
            _rollback_session(session)
            _close_session(session)
            raise
        _close_session(session)
        return outcome
