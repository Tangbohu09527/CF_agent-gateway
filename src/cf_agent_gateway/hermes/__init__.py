"""Hermes HTTP boundary with demand-loaded public Gateway exports.

The official Hermes plugin imports only the return bridge and TLS support. It
must not initialize Gateway database/runtime modules merely by importing this
package; those remain available through the original public names.
"""

from importlib import import_module

_EXPORT_MODULES = {
    "DEFAULT_TIMEOUT": "cf_agent_gateway.hermes.client",
    "HERMES_IDEMPOTENCY_HEADER": "cf_agent_gateway.hermes.client",
    "HERMES_SESSION_HEADER": "cf_agent_gateway.hermes.client",
    "HermesClient": "cf_agent_gateway.hermes.client",
    "HermesAPIError": "cf_agent_gateway.hermes.errors",
    "HermesAPIKeyError": "cf_agent_gateway.hermes.errors",
    "HermesConfigurationError": "cf_agent_gateway.hermes.errors",
    "HermesDeliveryError": "cf_agent_gateway.hermes.errors",
    "HermesDispatchError": "cf_agent_gateway.hermes.errors",
    "HermesError": "cf_agent_gateway.hermes.errors",
    "HermesExecutionTimeoutError": "cf_agent_gateway.hermes.errors",
    "HermesResponseError": "cf_agent_gateway.hermes.errors",
    "HermesTimeoutError": "cf_agent_gateway.hermes.errors",
    "HermesTransportError": "cf_agent_gateway.hermes.errors",
    "HERMES_CONTEXT_TOOL_NAMES": "cf_agent_gateway.hermes.models",
    "ArtifactRefPart": "cf_agent_gateway.hermes.models",
    "HermesAssistantMessage": "cf_agent_gateway.hermes.models",
    "HermesChatCompletionChoice": "cf_agent_gateway.hermes.models",
    "HermesChatCompletionRequest": "cf_agent_gateway.hermes.models",
    "HermesChatCompletionResponse": "cf_agent_gateway.hermes.models",
    "HermesChatResult": "cf_agent_gateway.hermes.models",
    "HermesDispatchOutcome": "cf_agent_gateway.hermes.models",
    "HermesResponseDeliveryOutcome": "cf_agent_gateway.hermes.models",
    "HermesUserMessage": "cf_agent_gateway.hermes.models",
    "ResponseEnvelope": "cf_agent_gateway.hermes.models",
    "ResponsePart": "cf_agent_gateway.hermes.models",
    "TextPart": "cf_agent_gateway.hermes.models",
    "HermesDispatchOutboxExecutor": "cf_agent_gateway.hermes.outbox",
    "HermesResponseHandler": "cf_agent_gateway.hermes.response",
    "HermesResponseProcessor": "cf_agent_gateway.hermes.response",
    "HermesResponseRelay": "cf_agent_gateway.hermes.response",
    "HermesDispatchResponse": "cf_agent_gateway.hermes.result_models",
    "HermesDispatchResponseStore": "cf_agent_gateway.hermes.result_store",
    "HermesChatClient": "cf_agent_gateway.hermes.service",
    "HermesDispatcher": "cf_agent_gateway.hermes.service",
    "HermesDispatchService": "cf_agent_gateway.hermes.service",
    "DispatchClaim": "cf_agent_gateway.hermes.worker",
    "DispatchProcessResult": "cf_agent_gateway.hermes.worker",
    "HermesDispatchWorker": "cf_agent_gateway.hermes.worker",
}

__all__ = [
    "DEFAULT_TIMEOUT",
    "HERMES_CONTEXT_TOOL_NAMES",
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
    "HermesExecutionTimeoutError",
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


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
