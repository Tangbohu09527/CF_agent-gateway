from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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
class Settings:
    server: ServerSettings = ServerSettings()
    database: DatabaseSettings = DatabaseSettings()
    logging: LoggingSettings = LoggingSettings()


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read configuration {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"cannot parse configuration {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"configuration {config_path} must contain a YAML mapping")

    server = _mapping(raw, "server")
    database = _mapping(raw, "database")
    logging = _mapping(raw, "logging")

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
    )


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a YAML mapping")
    return value
