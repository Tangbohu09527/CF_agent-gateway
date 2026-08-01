from __future__ import annotations


class WechatPollingError(RuntimeError):
    """Base class for stable, sanitized WeChat polling errors."""

    code = "wechat_polling_error"


class InvalidBootstrapModeError(WechatPollingError, ValueError):
    code = "wechat_invalid_bootstrap_mode"

    def __init__(self) -> None:
        super().__init__("bootstrap_mode must be 'latest' or 'backfill'")


class WechatChatIdentityError(WechatPollingError):
    code = "wechat_chat_identity_error"

    def __init__(self) -> None:
        super().__init__("agent-wechat chat requires an id or username")


class WechatLocalIdError(WechatPollingError):
    code = "wechat_local_id_error"

    def __init__(self) -> None:
        super().__init__("agent-wechat message requires a positive numeric localId")


class WechatCheckpointNotFoundError(WechatPollingError):
    code = "wechat_checkpoint_not_found"

    def __init__(self) -> None:
        super().__init__("WeChat sync checkpoint does not exist")


class WechatCheckpointValueError(WechatPollingError, ValueError):
    code = "wechat_checkpoint_value_error"

    def __init__(self) -> None:
        super().__init__("checkpoint last_local_id must fit the non-negative BigInteger range")


class WechatConversationMismatchError(WechatPollingError):
    code = "wechat_conversation_mismatch"

    def __init__(self) -> None:
        super().__init__("agent-wechat message chatId does not match the requested chat")
