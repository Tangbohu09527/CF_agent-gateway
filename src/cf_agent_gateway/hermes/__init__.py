"""Hermes HTTP client boundary."""

from cf_agent_gateway.hermes.client import DEFAULT_TIMEOUT, HermesClient
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
    HermesDispatchOutcome,
    HermesResponseDeliveryOutcome,
    HermesUserMessage,
)
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
    "HermesAPIError",
    "HermesAPIKeyError",
    "HermesAssistantMessage",
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
