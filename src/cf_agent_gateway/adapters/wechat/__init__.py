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
    WechatSenderType,
)
from cf_agent_gateway.adapters.wechat.normalizer import (
    build_wechat_event_id,
    normalize_wechat_message,
)
from cf_agent_gateway.adapters.wechat.raw_models import (
    AgentWechatAuthStatus,
    AgentWechatMedia,
    RawWechatMessage,
)

__all__ = [
    "AgentWechatAuthStatus",
    "AgentWechatClient",
    "AgentWechatMedia",
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
    "WechatSenderType",
    "build_wechat_event_id",
    "normalize_wechat_message",
]
