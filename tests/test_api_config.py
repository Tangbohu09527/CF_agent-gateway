from __future__ import annotations

from pathlib import Path

import pytest

from cf_agent_gateway.config import ApiSettings, load_settings


def test_api_settings_default_to_fail_closed_authentication() -> None:
    settings = ApiSettings()

    assert settings.message_auth_enabled is True
    assert settings.bearer_token_env == "CF_AGENT_GATEWAY_API_TOKEN"


def test_api_authentication_can_be_explicitly_disabled_programmatically() -> None:
    settings = ApiSettings(message_auth_enabled=False)

    assert settings.message_auth_enabled is False


def test_load_settings_reads_api_authentication_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "api:\n  message_auth_enabled: false\n  bearer_token_env: TEST_GATEWAY_TOKEN\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.api == ApiSettings(
        message_auth_enabled=False,
        bearer_token_env="TEST_GATEWAY_TOKEN",
    )


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("api:\n  message_auth_enabled: 'true'\n", "api.message_auth_enabled"),
        ("api:\n  bearer_token_env: ''\n", "api.bearer_token_env"),
        ("api:\n  bearer_token_env: BAD=VALUE\n", "api.bearer_token_env"),
    ],
)
def test_load_settings_rejects_invalid_api_config(
    tmp_path: Path,
    config: str,
    error: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config, encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        load_settings(config_path)


def test_load_settings_rejects_plaintext_bearer_without_leaking_it(
    tmp_path: Path,
) -> None:
    secret = "plaintext-token-that-must-not-leak"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"api:\n  bearer_token: {secret}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="api.bearer_token") as error:
        load_settings(config_path)

    assert secret not in str(error.value)


def test_repository_config_enables_message_api_authentication() -> None:
    settings = load_settings("config/config.yaml")

    assert settings.api.message_auth_enabled is True
    assert settings.api.bearer_token_env == "CF_AGENT_GATEWAY_API_TOKEN"
