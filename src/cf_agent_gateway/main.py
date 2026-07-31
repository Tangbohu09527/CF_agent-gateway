from __future__ import annotations

import os

import uvicorn

from cf_agent_gateway.config import load_settings
from cf_agent_gateway.gateway.app import create_app

DEFAULT_CONFIG_PATH = "config/config.yaml"


def main() -> None:
    config_path = os.getenv("CF_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH)
    settings = load_settings(config_path)
    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
