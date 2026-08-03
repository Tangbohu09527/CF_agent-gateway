"""Persist-first message ingestion and admission."""

from cf_agent_gateway.ingestion.errors import (
    MessageIngestionError,
    PersistedMessageNotFoundError,
)
from cf_agent_gateway.ingestion.models import (
    MessageIngestionOutcome,
    PersistedAttachmentSnapshot,
    PersistedMessageSnapshot,
)
from cf_agent_gateway.ingestion.service import (
    AdmissionRequestResolver,
    DefaultAdmissionRequestResolver,
    MessageAdmissionService,
)
from cf_agent_gateway.ingestion.sink import (
    MessageStoreAdmissionSink,
    SessionFactoryMessageStoreAdmissionSink,
)

__all__ = [
    "AdmissionRequestResolver",
    "DefaultAdmissionRequestResolver",
    "MessageAdmissionService",
    "MessageIngestionError",
    "MessageIngestionOutcome",
    "MessageStoreAdmissionSink",
    "SessionFactoryMessageStoreAdmissionSink",
    "PersistedAttachmentSnapshot",
    "PersistedMessageSnapshot",
    "PersistedMessageNotFoundError",
]
