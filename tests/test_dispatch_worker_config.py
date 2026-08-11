from __future__ import annotations

from pathlib import Path

import pytest

from cf_agent_gateway.config import Settings, WorkerSettings, load_settings


def test_worker_settings_have_safe_defaults() -> None:
    settings = Settings()

    assert settings.worker == WorkerSettings(
        enabled=False,
        concurrency=4,
        lease_seconds=60,
        retry_limit=3,
    )


def test_load_worker_settings_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        "worker:\n  enabled: true\n  concurrency: 8\n  lease_seconds: 45\n  retry_limit: 2\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.worker == WorkerSettings(
        enabled=True,
        concurrency=8,
        lease_seconds=45,
        retry_limit=2,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled", "yes", "worker.enabled"),
        ("concurrency", True, "worker.concurrency"),
        ("concurrency", 0, "worker.concurrency"),
        ("lease_seconds", float("inf"), "worker.lease_seconds"),
        ("lease_seconds", 0, "worker.lease_seconds"),
        ("retry_limit", True, "worker.retry_limit"),
        ("retry_limit", -1, "worker.retry_limit"),
    ],
)
def test_worker_settings_reject_invalid_values(
    field: str,
    value: object,
    message: str,
) -> None:
    values = {
        "enabled": False,
        "concurrency": 4,
        "lease_seconds": 60,
        "retry_limit": 3,
        field: value,
    }

    with pytest.raises(ValueError, match=message):
        WorkerSettings(**values)


def test_worker_environment_overrides_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        "worker:\n  enabled: true\n  concurrency: 8\n  lease_seconds: 45\n  retry_limit: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CF_GATEWAY_WORKER_CONCURRENCY", " 6 ")
    monkeypatch.setenv("CF_GATEWAY_WORKER_LEASE_SECONDS", " 90.5 ")
    monkeypatch.setenv("CF_GATEWAY_WORKER_RETRY_LIMIT", " 5 ")

    settings = load_settings(config_path)

    assert settings.worker == WorkerSettings(
        enabled=True,
        concurrency=6,
        lease_seconds=90.5,
        retry_limit=5,
    )


def test_single_worker_environment_override_preserves_other_yaml_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        "worker:\n  enabled: true\n  concurrency: 8\n  lease_seconds: 45\n  retry_limit: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CF_GATEWAY_WORKER_CONCURRENCY", "6")

    settings = load_settings(config_path)

    assert settings.worker == WorkerSettings(
        enabled=True,
        concurrency=6,
        lease_seconds=45,
        retry_limit=2,
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("CF_GATEWAY_WORKER_CONCURRENCY", "true", "must be an integer"),
        ("CF_GATEWAY_WORKER_CONCURRENCY", "0", "worker.concurrency"),
        ("CF_GATEWAY_WORKER_LEASE_SECONDS", "nan", "worker.lease_seconds"),
        ("CF_GATEWAY_WORKER_LEASE_SECONDS", "0", "worker.lease_seconds"),
        ("CF_GATEWAY_WORKER_RETRY_LIMIT", "1.5", "must be an integer"),
        ("CF_GATEWAY_WORKER_RETRY_LIMIT", "-1", "worker.retry_limit"),
    ],
)
def test_worker_environment_rejects_invalid_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    config_path = tmp_path / "worker.yaml"
    config_path.write_text("worker:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        load_settings(config_path)
