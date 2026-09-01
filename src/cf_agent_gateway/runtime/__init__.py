"""Runtime assembly and lifecycle entry points."""

from cf_agent_gateway.runtime.delivery import (
    drain_wechat_delivery_outbox,
    run_wechat_delivery_once,
)
from cf_agent_gateway.runtime.errors import (
    DispatchWorkerDisabledError,
    DispatchWorkerRuntimeError,
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
    HermesRuntimeDisabledError,
    HermesRuntimeError,
    WechatClientInitializationError,
    WechatPollingExecutionError,
    WechatRuntimeDisabledError,
    WechatRuntimeError,
    WechatTokenEnvironmentError,
)
from cf_agent_gateway.runtime.wechat import run_wechat_poll_once

__all__ = [
    "DispatchWorkerDisabledError",
    "DispatchWorkerRuntimeError",
    "HermesAPIKeyEnvironmentError",
    "HermesClientInitializationError",
    "HermesRuntimeDisabledError",
    "HermesRuntimeError",
    "WechatClientInitializationError",
    "WechatPollingExecutionError",
    "WechatRuntimeDisabledError",
    "WechatRuntimeError",
    "drain_wechat_delivery_outbox",
    "WechatTokenEnvironmentError",
    "run_wechat_poll_once",
    "run_wechat_delivery_once",
]
