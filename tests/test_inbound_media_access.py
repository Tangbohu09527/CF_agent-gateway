"""Portable HTTP error mapping; real flock/storage coverage is in read_http."""

import errno

import pytest
import test_inbound_media_queue as queue_tests
from fastapi.testclient import TestClient
from test_inbound_media_queue import MEDIA, admit, worker

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.inbound import access
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.task.model import HermesDispatchRecordStore

rig = queue_tests.rig


@pytest.mark.parametrize(
    "code,retryable,status",
    [
        ("media_staging_busy", True, 503),
        ("media_staging_busy", False, 403),
        ("media_staging_io_failed", True, 503),
        ("media_staging_io_failed", False, 403),
        ("media_staging_missing", True, 403),
        ("media_staging_conflict", True, 403),
        ("media_registry_conflict", True, 403),
        ("media_source_binding_changed", True, 403),
        ("unknown_error", True, 403),
    ],
)
def test_only_explicit_temporary_storage_errors_offer_retry(
    rig, monkeypatch, code, retryable, status
):
    admit(rig)
    assert worker(rig).run_once() == "ready"
    with rig.sessions() as session:
        HermesDispatchRecordStore(session).claim_next(claim_token="http-classification")
        descriptor = access.grant_read(
            session, session.get(InboundMediaJob, 1), public_base_url=MEDIA.public_base_url
        )
        session.commit()
    calls = []

    def fail_read(*_args, **_kwargs):
        calls.append(True)
        raise MediaFetchError(code, retryable=retryable)

    monkeypatch.setattr(rig.staging, "read", fail_read)
    monkeypatch.setattr(access, "InboundMediaStaging", lambda _root: rig.staging)
    settings = Settings(database=DatabaseSettings(url=str(rig.engine.url)), inbound_media=MEDIA)
    with TestClient(create_app(settings)) as client:
        denied = client.get("/inbound-media/1/content")
        assert denied.status_code == 403 and not calls
        response = client.get(
            "/inbound-media/1/content", headers={"Authorization": descriptor["authorization"]}
        )
    assert response.status_code == status and len(calls) == 1
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get("retry-after") == ("1" if status == 503 else None)
    assert code not in response.text and descriptor["authorization"] not in response.text


@pytest.mark.parametrize(
    "error_number,retryable",
    [
        (errno.EIO, True),
        (errno.EBUSY, True),
        (errno.EACCES, False),
        (errno.ELOOP, False),
        (None, False),
    ],
)
def test_storage_os_errors_do_not_turn_permission_or_link_errors_into_retries(
    monkeypatch, error_number, retryable
):
    # Mapping-only unit test: no native path or filesystem validation is replaced
    # in the integration suite; Windows cannot construct Linux private staging.
    staging = object.__new__(InboundMediaStaging)

    def fail_open():
        raise OSError(error_number, "synthetic private path must stay private")

    monkeypatch.setattr(staging, "_root_fd", fail_open)
    with pytest.raises(MediaFetchError) as raised:
        staging.read("a" * 64, size=1, sha256="b" * 64)
    assert raised.value.code == "media_staging_io_failed"
    assert raised.value.retryable is retryable
