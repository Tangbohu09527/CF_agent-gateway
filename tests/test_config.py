from pathlib import Path

import pytest

from cf_agent_gateway.config import load_settings


def test_load_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
server:
  host: 127.0.0.1
  port: 9090
database:
  url: postgresql+psycopg://gateway:secret@db/gateway
logging:
  level: debug
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 9090
    assert settings.database.url.startswith("postgresql+psycopg://")
    assert settings.logging.level == "DEBUG"


def test_load_settings_rejects_invalid_port(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server:\n  port: 70000\n", encoding="utf-8")

    with pytest.raises(ValueError, match="server.port"):
        load_settings(config_path)
