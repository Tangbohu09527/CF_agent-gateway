from pathlib import Path

import pytest

from cf_agent_gateway.config import HermesSettings, WechatSettings, load_settings


def test_wechat_settings_defaults() -> None:
    settings = WechatSettings()

    assert settings.enabled is False
    assert settings.base_url == "http://127.0.0.1:6174"
    assert settings.bootstrap_mode == "latest"
    assert settings.token_env == "CF_AGENT_WECHAT_TOKEN"


def test_hermes_settings_defaults() -> None:
    settings = HermesSettings()

    assert settings.enabled is False
    assert settings.base_url == ""
    assert settings.api_key_env == "HERMES_API_KEY"
    assert settings.model == "hermes-agent"


def test_legacy_yaml_without_wechat_settings_uses_safe_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("logging:\n  level: INFO\n", encoding="utf-8")

    settings = load_settings(config_path)

    assert settings.wechat == WechatSettings()
    assert settings.hermes == HermesSettings()


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
hermes:
  enabled: true
  base_url: https://hermes.internal:8642
  api_key_env: TEST_HERMES_API_KEY
  model: test-hermes-agent
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
    assert settings.hermes == HermesSettings(
        enabled=True,
        base_url="https://hermes.internal:8642",
        api_key_env="TEST_HERMES_API_KEY",
        model="test-hermes-agent",
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


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "hermes.internal:8642",
        "ftp://hermes.internal",
        "http://",
        "http://hermes.internal:not-a-port",
        "http://user:password@hermes.internal",
        "http://hermes.internal?key=value",
        "http://hermes.internal#fragment",
    ],
)
def test_enabled_hermes_rejects_invalid_base_url(tmp_path: Path, base_url: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"hermes:\n  enabled: true\n  base_url: {base_url!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hermes.base_url"):
        load_settings(config_path)


def test_load_settings_rejects_plaintext_hermes_api_key_without_leaking_it(
    tmp_path: Path,
) -> None:
    api_key = "plaintext-key-that-must-not-leak"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"hermes:\n  api_key: {api_key}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hermes.api_key") as error:
        load_settings(config_path)

    assert api_key not in str(error.value)


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
