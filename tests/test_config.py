from pathlib import Path

import pytest

from cf_agent_gateway.config import WechatSettings, load_settings


def test_wechat_settings_defaults() -> None:
    settings = WechatSettings()

    assert settings.enabled is False
    assert settings.base_url == "http://127.0.0.1:6174"
    assert settings.bootstrap_mode == "latest"
    assert settings.token_env == "CF_AGENT_WECHAT_TOKEN"


def test_legacy_yaml_without_wechat_settings_uses_safe_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("logging:\n  level: INFO\n", encoding="utf-8")

    settings = load_settings(config_path)

    assert settings.wechat == WechatSettings()


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
wechat:
  enabled: true
  base_url: https://agent-wechat.internal:6174
  bootstrap_mode: backfill
  token_env: TEST_WECHAT_TOKEN
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 9090
    assert settings.database.url.startswith("postgresql+psycopg://")
    assert settings.logging.level == "DEBUG"
    assert settings.wechat == WechatSettings(
        enabled=True,
        base_url="https://agent-wechat.internal:6174",
        bootstrap_mode="backfill",
        token_env="TEST_WECHAT_TOKEN",
    )


def test_load_settings_rejects_invalid_port(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server:\n  port: 70000\n", encoding="utf-8")

    with pytest.raises(ValueError, match="server.port"):
        load_settings(config_path)


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "agent-wechat.internal:6174",
        "ftp://agent-wechat.internal",
        "http://",
        "http://exa mple.com",
        "http://agent-wechat.internal:not-a-port",
        "http://user:password@agent-wechat.internal",
        "http://agent-wechat.internal?token=secret",
        "http://agent-wechat.internal#fragment",
    ],
)
def test_load_settings_rejects_invalid_wechat_base_url(
    tmp_path: Path,
    base_url: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"wechat:\n  base_url: {base_url!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wechat.base_url"):
        load_settings(config_path)


@pytest.mark.parametrize("bootstrap_mode", ["replay_everything", "[latest]"])
def test_load_settings_rejects_invalid_wechat_bootstrap_mode(
    tmp_path: Path,
    bootstrap_mode: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"wechat:\n  bootstrap_mode: {bootstrap_mode}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wechat.bootstrap_mode"):
        load_settings(config_path)


def test_yaml_parse_error_does_not_echo_sensitive_source_text(tmp_path: Path) -> None:
    sensitive_value = "token-value-that-must-not-leak"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"wechat:\n  token_env: [\n  # {sensitive_value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        load_settings(config_path)

    assert sensitive_value not in str(error.value)
