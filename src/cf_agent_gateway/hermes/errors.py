from __future__ import annotations


class HermesError(RuntimeError):
    """Base class for stable Hermes client errors."""

    code = "hermes_error"


class HermesConfigurationError(HermesError, ValueError):
    """The Hermes client cannot be created from the supplied configuration."""

    code = "hermes_configuration_error"


class HermesAPIKeyError(HermesConfigurationError):
    """The supplied Hermes API key is missing or invalid."""

    code = "hermes_api_key_error"

    def __init__(self) -> None:
        super().__init__("Hermes API key is missing or invalid")


class HermesAPIError(HermesError):
    """Hermes returned a non-success HTTP status."""

    code = "hermes_api_error"

    def __init__(self, *, operation: str, status_code: int) -> None:
        self.operation = operation
        self.status_code = status_code
        self.category = _http_error_category(status_code)
        super().__init__(f"Hermes API operation {operation!r} returned HTTP {status_code}")


class HermesTransportError(HermesError):
    """A request could not reach Hermes."""

    code = "hermes_transport_error"

    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"Hermes API operation {operation!r} failed in transport")


class HermesTimeoutError(HermesTransportError):
    """A request to Hermes exceeded its configured timeout."""

    code = "hermes_timeout_error"

    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        HermesError.__init__(self, f"Hermes API operation {operation!r} timed out")


class HermesResponseError(HermesError):
    """Hermes returned a successful response with an invalid shape."""

    code = "hermes_response_error"

    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"Hermes API operation {operation!r} returned an invalid response")


def _http_error_category(status_code: int) -> str:
    if status_code in {401, 403}:
        return "authentication"
    if status_code == 429:
        return "rate_limit"
    if 400 <= status_code < 500:
        return "client"
    if 500 <= status_code < 600:
        return "server"
    return "unexpected_status"
