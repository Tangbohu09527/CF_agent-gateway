"""Hermes HTTP client boundary."""

from cf_agent_gateway.hermes.client import DEFAULT_TIMEOUT, HermesClient
from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesAPIKeyError,
    HermesConfigurationError,
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
    HermesUserMessage,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "HermesAPIError",
    "HermesAPIKeyError",
    "HermesAssistantMessage",
    "HermesChatCompletionChoice",
    "HermesChatCompletionRequest",
    "HermesChatCompletionResponse",
    "HermesClient",
    "HermesConfigurationError",
    "HermesError",
    "HermesResponseError",
    "HermesTimeoutError",
    "HermesTransportError",
    "HermesUserMessage",
]
