from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from threading import TIMEOUT_MAX
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

_POLLING_INTERVAL_ERROR = (
    "runtime.polling_interval_seconds must be within the supported positive timeout range"
)
_RUNTIME_TIMEOUT_ERROR = "runtime timeout values must be finite positive numbers"
_CONFIGURATION_KEYS = frozenset(
    {"server", "database", "logging", "api", "runtime", "wechat", "hermes"}
)
_SECTION_KEYS = {
    "server": frozenset({"host", "port"}),
    "database": frozenset({"url"}),
    "logging": frozenset({"level"}),
    "api": frozenset({"message_auth_enabled", "bearer_token_env"}),
    "runtime": frozenset(
        {
            "polling_interval_seconds",
            "polling_retry_max_seconds",
            "heartbeat_interval_seconds",
            "heartbeat_stale_after_seconds",
            "cycle_stale_after_seconds",
        }
    ),
    "wechat": frozenset({"enabled", "base_url", "bootstrap_mode", "token_env"}),
    "hermes": frozenset({"enabled", "base_url", "api_key_env", "model"}),
}


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str = "sqlite:///./data/gateway.db"


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str = "INFO"


@dataclass(frozen=True, slots=True)
class ApiSettings:
    message_auth_enabled: bool = True
    bearer_token_env: str = "CF_AGENT_GATEWAY_API_TOKEN"

    def __post_init__(self) -> None:
        if not isinstance(self.message_auth_enabled, bool):
            raise ValueError("api.message_auth_enabled must be a boolean")
        if not isinstance(self.bearer_token_env, str) or not self.bearer_token_env.strip():
            raise ValueError("api.bearer_token_env must name an environment variable")
        bearer_token_env = self.bearer_token_env.strip()
        if "=" in bearer_token_env or "\x00" in bearer_token_env:
            raise ValueError("api.bearer_token_env must name an environment variable")
        object.__setattr__(self, "bearer_token_env", bearer_token_env)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    polling_interval_seconds: float = 3.0
    polling_retry_max_seconds: float = 60.0
    heartbeat_interval_seconds: float = 5.0
    heartbeat_stale_after_seconds: float = 30.0
    cycle_stale_after_seconds: float = 300.0

    def __post_init__(self) -> None:
        interval = self.polling_interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError(_POLLING_INTERVAL_ERROR)
        try:
            normalized_interval = float(interval)
        except (OverflowError, ValueError):
            raise ValueError(_POLLING_INTERVAL_ERROR) from None
        if (
            not math.isfinite(normalized_interval)
            or normalized_interval <= 0
            or normalized_interval > TIMEOUT_MAX
        ):
            raise ValueError(_POLLING_INTERVAL_ERROR)
        object.__setattr__(self, "polling_interval_seconds", normalized_interval)

        for field_name in (
            "polling_retry_max_seconds",
            "heartbeat_interval_seconds",
            "heartbeat_stale_after_seconds",
            "cycle_stale_after_seconds",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(_RUNTIME_TIMEOUT_ERROR)
            normalized_value = float(value)
            if (
                not math.isfinite(normalized_value)
                or normalized_value <= 0
                or normalized_value > TIMEOUT_MAX
            ):
                raise ValueError(_RUNTIME_TIMEOUT_ERROR)
            object.__setattr__(self, field_name, normalized_value)
        if self.polling_retry_max_seconds < self.polling_interval_seconds:
            raise ValueError(
                "runtime.polling_retry_max_seconds must be greater than or equal to "
                "runtime.polling_interval_seconds"
            )
        if self.heartbeat_stale_after_seconds <= self.heartbeat_interval_seconds * 2:
            raise ValueError(
                "runtime.heartbeat_stale_after_seconds must exceed twice "
                "runtime.heartbeat_interval_seconds"
            )
        if self.cycle_stale_after_seconds <= self.heartbeat_stale_after_seconds:
            raise ValueError(
                "runtime.cycle_stale_after_seconds must exceed "
                "runtime.heartbeat_stale_after_seconds"
            )


@dataclass(frozen=True, slots=True)
class WechatSettings:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:6174"
    bootstrap_mode: Literal["latest", "backfill"] = "latest"
    token_env: str = "CF_AGENT_WECHAT_TOKEN"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("wechat.enabled must be a boolean")

        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ValueError("wechat.base_url must be a non-empty HTTP or HTTPS URL")
        base_url = self.base_url.strip()
        if any(character.isspace() or ord(character) < 0x20 for character in base_url):
            raise ValueError("wechat.base_url must be a non-empty HTTP or HTTPS URL")
        try:
            parsed_url = urlsplit(base_url)
            port = parsed_url.port
        except ValueError:
            raise ValueError("wechat.base_url must be a non-empty HTTP or HTTPS URL") from None
        if (
            parsed_url.scheme.lower() not in {"http", "https"}
            or parsed_url.hostname is None
            or port == 0
            or parsed_url.username is not None
            or parsed_url.password is not None
            or bool(parsed_url.query)
            or bool(parsed_url.fragment)
        ):
            raise ValueError("wechat.base_url must be a non-empty HTTP or HTTPS URL")

        if not isinstance(self.bootstrap_mode, str) or self.bootstrap_mode not in (
            "latest",
            "backfill",
        ):
            raise ValueError("wechat.bootstrap_mode must be latest or backfill")

        if not isinstance(self.token_env, str) or not self.token_env.strip():
            raise ValueError("wechat.token_env must name an environment variable")
        token_env = self.token_env.strip()
        if "=" in token_env or "\x00" in token_env:
            raise ValueError("wechat.token_env must name an environment variable")

        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "token_env", token_env)


@dataclass(frozen=True, slots=True)
class HermesSettings:
    enabled: bool = False
    base_url: str = ""
    api_key_env: str = "HERMES_API_KEY"
    model: str = "hermes-agent"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("hermes.enabled must be a boolean")

        if not isinstance(self.base_url, str):
            raise ValueError("hermes.base_url must be an HTTP or HTTPS URL")
        base_url = self.base_url.strip()
        if self.enabled and not base_url:
            raise ValueError("hermes.base_url is required when Hermes is enabled")
        if base_url:
            if any(character.isspace() or ord(character) < 0x20 for character in base_url):
                raise ValueError("hermes.base_url must be an HTTP or HTTPS URL")
            try:
                parsed_url = urlsplit(base_url)
                port = parsed_url.port
            except ValueError:
                raise ValueError("hermes.base_url must be an HTTP or HTTPS URL") from None
            if (
                parsed_url.scheme.lower() not in {"http", "https"}
                or parsed_url.hostname is None
                or port == 0
                or parsed_url.username is not None
                or parsed_url.password is not None
                or bool(parsed_url.query)
                or bool(parsed_url.fragment)
            ):
                raise ValueError("hermes.base_url must be an HTTP or HTTPS URL")

        if not isinstance(self.api_key_env, str) or not self.api_key_env.strip():
            raise ValueError("hermes.api_key_env must name an environment variable")
        api_key_env = self.api_key_env.strip()
        if "=" in api_key_env or "\x00" in api_key_env:
            raise ValueError("hermes.api_key_env must name an environment variable")

        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("hermes.model must not be empty")
        model = self.model.strip()

        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "api_key_env", api_key_env)
        object.__setattr__(self, "model", model)


@dataclass(frozen=True, slots=True)
class Settings:
    server: ServerSettings = ServerSettings()
    database: DatabaseSettings = DatabaseSettings()
    logging: LoggingSettings = LoggingSettings()
    api: ApiSettings = ApiSettings()
    wechat: WechatSettings = WechatSettings()
    hermes: HermesSettings = HermesSettings()
    runtime: RuntimeSettings = RuntimeSettings()


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read configuration {config_path}: {exc}") from exc
    except yaml.YAMLError:
        raise ValueError(f"cannot parse configuration {config_path}") from None

    if not isinstance(raw, dict):
        raise ValueError(f"configuration {config_path} must contain a YAML mapping")

    server = _mapping(raw, "server")
    database = _mapping(raw, "database")
    logging = _mapping(raw, "logging")
    api = _mapping(raw, "api")
    runtime = _mapping(raw, "runtime")
    wechat = _mapping(raw, "wechat")
    hermes = _mapping(raw, "hermes")

    if "api_key" in hermes:
        raise ValueError("hermes.api_key is not allowed; use hermes.api_key_env")
    if "bearer_token" in api:
        raise ValueError("api.bearer_token is not allowed; use api.bearer_token_env")
    if "token" in wechat:
        raise ValueError("wechat.token is not allowed; use wechat.token_env")
    _reject_unknown_keys(raw, _CONFIGURATION_KEYS, "configuration")
    for section_name, section in (
        ("server", server),
        ("database", database),
        ("logging", logging),
        ("api", api),
        ("runtime", runtime),
        ("wechat", wechat),
        ("hermes", hermes),
    ):
        _reject_unknown_keys(section, _SECTION_KEYS[section_name], section_name)

    host = str(server.get("host", "0.0.0.0")).strip()
    port = int(server.get("port", 8080))
    database_url = str(database.get("url", "sqlite:///./data/gateway.db")).strip()
    log_level = str(logging.get("level", "INFO")).upper()

    if not host:
        raise ValueError("server.host is required")
    if not 1 <= port <= 65535:
        raise ValueError("server.port must be between 1 and 65535")
    if not database_url:
        raise ValueError("database.url is required")
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"unsupported logging.level: {log_level}")

    return Settings(
        server=ServerSettings(host=host, port=port),
        database=DatabaseSettings(url=database_url),
        logging=LoggingSettings(level=log_level),
        api=ApiSettings(
            message_auth_enabled=api.get("message_auth_enabled", True),
            bearer_token_env=api.get("bearer_token_env", "CF_AGENT_GATEWAY_API_TOKEN"),
        ),
        runtime=RuntimeSettings(
            polling_interval_seconds=runtime.get("polling_interval_seconds", 3.0),
            polling_retry_max_seconds=runtime.get("polling_retry_max_seconds", 60.0),
            heartbeat_interval_seconds=runtime.get("heartbeat_interval_seconds", 5.0),
            heartbeat_stale_after_seconds=runtime.get("heartbeat_stale_after_seconds", 30.0),
            cycle_stale_after_seconds=runtime.get("cycle_stale_after_seconds", 300.0),
        ),
        wechat=WechatSettings(
            enabled=wechat.get("enabled", False),
            base_url=wechat.get("base_url", "http://127.0.0.1:6174"),
            bootstrap_mode=wechat.get("bootstrap_mode", "latest"),
            token_env=wechat.get("token_env", "CF_AGENT_WECHAT_TOKEN"),
        ),
        hermes=HermesSettings(
            enabled=hermes.get("enabled", False),
            base_url=hermes.get("base_url", ""),
            api_key_env=hermes.get("api_key_env", "HERMES_API_KEY"),
            model=hermes.get("model", "hermes-agent"),
        ),
    )


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a YAML mapping")
    return value


def _reject_unknown_keys(
    values: dict[object, object],
    allowed: frozenset[str],
    section_name: str,
) -> None:
    unknown = sorted(str(key) for key in values if key not in allowed)
    if unknown:
        raise ValueError(f"{section_name} contains unsupported keys: {', '.join(unknown)}")
