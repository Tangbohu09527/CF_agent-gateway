"""Hermes HTTP client boundary."""

from cf_agent_gateway.hermes.client import DEFAULT_TIMEOUT, HERMES_SESSION_HEADER, HermesClient
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
    HermesAssistantMessage,
    HermesChatCompletionChoice,
    HermesChatCompletionRequest,
    HermesChatCompletionResponse,
    HermesChatResult,
    HermesDispatchOutcome,
    HermesResponseDeliveryOutcome,
    HermesUserMessage,
)
from cf_agent_gateway.hermes.outbox import HermesDispatchOutboxExecutor
from cf_agent_gateway.hermes.response import (
    HermesResponseHandler,
    HermesResponseProcessor,
    HermesResponseRelay,
)
from cf_agent_gateway.hermes.service import (
    HermesChatClient,
    HermesDispatcher,
    HermesDispatchService,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "HERMES_SESSION_HEADER",
    "HermesAPIError",
    "HermesAPIKeyError",
    "HermesAssistantMessage",
    "HermesChatResult",
    "HermesChatClient",
    "HermesChatCompletionChoice",
    "HermesChatCompletionRequest",
    "HermesChatCompletionResponse",
    "HermesClient",
    "HermesConfigurationError",
    "HermesDeliveryError",
    "HermesDispatchError",
    "HermesDispatcher",
    "HermesDispatchOutcome",
    "HermesDispatchOutboxExecutor",
    "HermesDispatchService",
    "HermesError",
    "HermesResponseDeliveryOutcome",
    "HermesResponseError",
    "HermesResponseHandler",
    "HermesResponseProcessor",
    "HermesResponseRelay",
    "HermesTimeoutError",
    "HermesTransportError",
    "HermesUserMessage",
]
