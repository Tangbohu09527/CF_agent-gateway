from __future__ import annotations


class WechatAdapterError(RuntimeError):
    """Base class for stable agent-wechat adapter errors."""

    code = "wechat_adapter_error"


class WechatAPIError(WechatAdapterError):
    """An agent-wechat endpoint returned a non-success HTTP status."""

    code = "wechat_api_error"

    def __init__(self, *, operation: str, status_code: int) -> None:
        self.operation = operation
        self.status_code = status_code
        self.category = _http_error_category(status_code)
        super().__init__(f"agent-wechat API operation {operation!r} returned HTTP {status_code}")


class WechatTransportError(WechatAdapterError):
    """A request could not reach agent-wechat."""

    code = "wechat_transport_error"

    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"agent-wechat API operation {operation!r} failed in transport")


class WechatTimeoutError(WechatTransportError):
    """A request to agent-wechat exceeded its configured timeout."""

    code = "wechat_timeout_error"

    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        WechatAdapterError.__init__(self, f"agent-wechat API operation {operation!r} timed out")


class WechatResponseError(WechatAdapterError):
    """agent-wechat returned a successful response with an invalid shape."""

    code = "wechat_response_error"

    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"agent-wechat API operation {operation!r} returned an invalid response")


class WechatNormalizationError(WechatAdapterError):
    """A raw WeChat message cannot be normalized without inventing data."""

    code = "wechat_normalization_error"


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
