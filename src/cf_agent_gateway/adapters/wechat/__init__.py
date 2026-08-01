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
from cf_agent_gateway.adapters.wechat.polling_errors import (
    InvalidBootstrapModeError,
    WechatChatIdentityError,
    WechatCheckpointNotFoundError,
    WechatConversationMismatchError,
    WechatLocalIdError,
    WechatPollingError,
)
from cf_agent_gateway.adapters.wechat.polling_models import (
    BootstrapMode,
    ChatPollResult,
    PollFailure,
    PollFailureStage,
    PollResult,
    WechatSyncCheckpoint,
)
from cf_agent_gateway.adapters.wechat.polling_service import (
    NormalizedMessageSink,
    WechatPollingClient,
    WechatPollingService,
)
from cf_agent_gateway.adapters.wechat.polling_store import (
    WechatCheckpointStore,
    WechatSyncCheckpointStore,
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
    "BootstrapMode",
    "ChatPollResult",
    "InvalidBootstrapModeError",
    "NormalizedWechatMessage",
    "NormalizedMessageSink",
    "PollFailure",
    "PollFailureStage",
    "PollResult",
    "RawWechatMessage",
    "WechatAPIError",
    "WechatAdapterError",
    "WechatConversationType",
    "WechatConversationMismatchError",
    "WechatChatIdentityError",
    "WechatCheckpointNotFoundError",
    "WechatCheckpointStore",
    "WechatLocalIdError",
    "WechatMessageType",
    "WechatNormalizationError",
    "WechatPollingClient",
    "WechatPollingError",
    "WechatPollingService",
    "WechatReplySummary",
    "WechatResponseError",
    "WechatTimeoutError",
    "WechatTransportError",
    "WechatSenderType",
    "WechatSyncCheckpoint",
    "WechatSyncCheckpointStore",
    "build_wechat_event_id",
    "normalize_wechat_message",
]
