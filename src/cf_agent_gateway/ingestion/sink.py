from __future__ import annotations

from cf_agent_gateway.adapters.wechat import NormalizedWechatMessage
from cf_agent_gateway.ingestion.models import MessageIngestionOutcome
from cf_agent_gateway.ingestion.service import MessageAdmissionService


class MessageStoreAdmissionSink:
    """Polling-compatible sink that preserves all storage and admission failures."""

    def __init__(self, service: MessageAdmissionService) -> None:
        self._service = service

    def handle(self, message: NormalizedWechatMessage) -> None:
        self.process(message)

    def process(self, message: NormalizedWechatMessage) -> MessageIngestionOutcome:
        return self._service.process(message)
