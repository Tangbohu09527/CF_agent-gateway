from __future__ import annotations


class HermesRuntimeError(RuntimeError):
    code = "hermes_runtime_error"


class HermesAPIKeyEnvironmentError(HermesRuntimeError):
    code = "hermes_api_key_environment_missing"

    def __init__(self, environment_variable: str) -> None:
        self.environment_variable = environment_variable
        super().__init__(f"missing Hermes API key environment variable: {environment_variable}")


class HermesClientInitializationError(HermesRuntimeError):
    code = "hermes_client_initialization_failed"

    def __init__(self) -> None:
        super().__init__("cannot initialize the Hermes client")


class WechatRuntimeError(RuntimeError):
    code = "wechat_runtime_error"


class WechatRuntimeDisabledError(WechatRuntimeError):
    code = "wechat_runtime_disabled"

    def __init__(self) -> None:
        super().__init__("WeChat runtime is disabled")


class WechatTokenEnvironmentError(WechatRuntimeError):
    code = "wechat_token_environment_missing"

    def __init__(self, environment_variable: str) -> None:
        self.environment_variable = environment_variable
        super().__init__(f"missing WeChat token environment variable: {environment_variable}")


class WechatClientInitializationError(WechatRuntimeError):
    code = "wechat_client_initialization_failed"

    def __init__(self) -> None:
        super().__init__("cannot initialize the agent-wechat client")


class WechatPollingExecutionError(WechatRuntimeError):
    code = "wechat_poll_execution_failed"

    def __init__(self) -> None:
        super().__init__("cannot complete the WeChat polling cycle")
