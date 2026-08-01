"""agent-wechat HTTP client and conservative message normalization."""

from cf_agent_gateway.adapters.wechat.client import AgentWechatClient
from cf_agent_gateway.adapters.wechat.errors import (
    WechatAdapterError,
    WechatAPIError,
    WechatNormalizationError,
    WechatResponseError,
    WechatTimeoutError,
    WechatTransportError,
)
from cf_agent_gateway.adapters.wechat.normalized_models import (
    NormalizedWechatMessage,
    WechatConversationType,
    WechatMessageType,
    WechatReplySummary,
)
from cf_agent_gateway.adapters.wechat.normalizer import (
    build_wechat_event_id,
    normalize_wechat_message,
)
from cf_agent_gateway.adapters.wechat.raw_models import (
    AgentWechatAuthStatus,
    RawWechatMessage,
)

__all__ = [
    "AgentWechatAuthStatus",
    "AgentWechatClient",
    "NormalizedWechatMessage",
    "RawWechatMessage",
    "WechatAPIError",
    "WechatAdapterError",
    "WechatConversationType",
    "WechatMessageType",
    "WechatNormalizationError",
    "WechatReplySummary",
    "WechatResponseError",
    "WechatTimeoutError",
    "WechatTransportError",
    "build_wechat_event_id",
    "normalize_wechat_message",
]
