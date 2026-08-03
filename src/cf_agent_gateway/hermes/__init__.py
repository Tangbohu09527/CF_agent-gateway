"""Hermes HTTP client boundary."""

from cf_agent_gateway.hermes.client import DEFAULT_TIMEOUT, HermesClient
from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesAPIKeyError,
    HermesConfigurationError,
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
    HermesUserMessage,
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
    "HermesDispatchError",
    "HermesDispatcher",
    "HermesDispatchOutcome",
    "HermesDispatchService",
    "HermesError",
    "HermesResponseError",
    "HermesTimeoutError",
    "HermesTransportError",
    "HermesUserMessage",
]
