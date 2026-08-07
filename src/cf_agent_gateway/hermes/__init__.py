"""Hermes HTTP client boundary."""

from cf_agent_gateway.hermes.client import (
    DEFAULT_TIMEOUT,
    HERMES_IDEMPOTENCY_HEADER,
    HERMES_SESSION_HEADER,
    HermesClient,
)
from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesAPIKeyError,
    HermesConfigurationError,
    HermesDeliveryError,
    HermesDispatchError,
    HermesError,
    HermesResponseError,
    HermesTimeoutError,
    HermesTransportError,
)
from cf_agent_gateway.hermes.models import (
    ArtifactRefPart,
    HermesAssistantMessage,
    HermesChatCompletionChoice,
    HermesChatCompletionRequest,
    HermesChatCompletionResponse,
    HermesChatResult,
    HermesDispatchOutcome,
    HermesResponseDeliveryOutcome,
    HermesUserMessage,
    ResponseEnvelope,
    ResponsePart,
    TextPart,
)
from cf_agent_gateway.hermes.outbox import HermesDispatchOutboxExecutor
from cf_agent_gateway.hermes.response import (
    HermesResponseHandler,
    HermesResponseProcessor,
    HermesResponseRelay,
)
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
from cf_agent_gateway.hermes.service import (
    HermesChatClient,
    HermesDispatcher,
    HermesDispatchService,
)
from cf_agent_gateway.hermes.worker import (
    DispatchClaim,
    DispatchProcessResult,
    HermesDispatchWorker,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "HERMES_IDEMPOTENCY_HEADER",
    "HERMES_SESSION_HEADER",
    "ArtifactRefPart",
    "DispatchClaim",
    "DispatchProcessResult",
    "HermesAPIError",
    "HermesAPIKeyError",
    "HermesAssistantMessage",
    "HermesChatClient",
    "HermesChatCompletionChoice",
    "HermesChatCompletionRequest",
    "HermesChatCompletionResponse",
    "HermesChatResult",
    "HermesClient",
    "HermesConfigurationError",
    "HermesDeliveryError",
    "HermesDispatchError",
    "HermesDispatcher",
    "HermesDispatchOutcome",
    "HermesDispatchOutboxExecutor",
    "HermesDispatchResponse",
    "HermesDispatchResponseStore",
    "HermesDispatchService",
    "HermesDispatchWorker",
    "HermesError",
    "HermesResponseDeliveryOutcome",
    "HermesResponseError",
    "HermesResponseHandler",
    "HermesResponseProcessor",
    "HermesResponseRelay",
    "HermesTimeoutError",
    "HermesTransportError",
    "HermesUserMessage",
    "ResponseEnvelope",
    "ResponsePart",
    "TextPart",
]
