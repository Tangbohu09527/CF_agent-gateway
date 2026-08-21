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
from cf_agent_gateway.adapters.wechat.message_event import wechat_message_to_event
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
from cf_agent_gateway.adapters.wechat.outbound import WechatMessageSender
from cf_agent_gateway.adapters.wechat.outbound_http import WechatHttpMessageSender
from cf_agent_gateway.adapters.wechat.polling_errors import (
    InvalidBootstrapModeError,
    WechatChatIdentityError,
    WechatCheckpointConflictError,
    WechatCheckpointNotFoundError,
    WechatCheckpointValueError,
    WechatConversationMismatchError,
    WechatLocalIdError,
    WechatPollingError,
)
from cf_agent_gateway.adapters.wechat.polling_models import (
    MAX_CHECKPOINT_LOCAL_ID,
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
    "MAX_CHECKPOINT_LOCAL_ID",
    "RawWechatMessage",
    "WechatAPIError",
    "WechatAdapterError",
    "WechatConversationType",
    "WechatConversationMismatchError",
    "WechatChatIdentityError",
    "WechatCheckpointConflictError",
    "WechatCheckpointNotFoundError",
    "WechatCheckpointValueError",
    "WechatCheckpointStore",
    "WechatLocalIdError",
    "WechatMessageType",
    "WechatMessageSender",
    "WechatHttpMessageSender",
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
    "wechat_message_to_event",
]
