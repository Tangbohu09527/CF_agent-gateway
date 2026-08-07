from __future__ import annotations

import logging
import os

import uvicorn

from cf_agent_gateway.config import load_settings
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.logging import configure_logging

DEFAULT_CONFIG_PATH = "config/config.yaml"
logger = logging.getLogger(__name__)


def main() -> int:
    config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
    try:
        settings = load_settings(config_path)
    except Exception:
        configure_logging("INFO")
        logger.error(
            "gateway failed",
            extra={"fields": {"error_code": "runtime_configuration_invalid"}},
        )
        return 1

    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
