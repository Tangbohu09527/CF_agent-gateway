"""Exercise bounded test-peer downloads over a real Gateway TCP connection.

The portable storage double below does not replace native flock tests.
"""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import test_inbound_media_queue as queue_tests
from inbound_media_download_peer import PeerDownloadError, fetch_verified
from test_inbound_media_integration import gateway_server

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.inbound import access
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.task.model import HermesDispatchRecordStore

rig = queue_tests.rig


@pytest.fixture
def download(rig, monkeypatch):
    queue_tests.admit(rig)
    assert queue_tests.worker(rig).run_once() == "ready"
    monkeypatch.setattr(access, "InboundMediaStaging", lambda _root: rig.staging)
    settings = Settings(database=DatabaseSettings(url=str(rig.engine.url)), inbound_media=rig.media)
    with gateway_server(settings) as configured, rig.sessions() as session:
        HermesDispatchRecordStore(session).claim_next(claim_token="bounded-peer")
        descriptor = access.grant_read(
            session,
            session.get(InboundMediaJob, 1),
            public_base_url=configured.inbound_media.public_base_url,
        )
        session.commit()
        assert descriptor["download_policy"] == {
            "max_attempts": 4,
            "total_timeout_seconds": 30,
            "retryable_status_codes": [503],
            "retry_after_seconds": 1,
        }
        yield descriptor


def test_repeated_503_stops_at_four_total_attempts(download, rig, monkeypatch):
    statuses = []

    def busy(*_args, **_kwargs):
        raise MediaFetchError("media_staging_busy", retryable=True)

    monkeypatch.setattr(rig.staging, "read", busy)
    with pytest.raises(PeerDownloadError, match="download_attempts_exhausted"):
        asyncio.run(fetch_verified(download, on_response=statuses.append))
    assert statuses == [503] * 4


@pytest.mark.parametrize("expiry_bound", [False, True])
def test_retry_sleep_cannot_exceed_total_or_capability_deadline(
    download, rig, monkeypatch, expiry_bound
):
    statuses = []

    def busy(*_args, **_kwargs):
        raise MediaFetchError("media_staging_busy", retryable=True)

    monkeypatch.setattr(rig.staging, "read", busy)
    if expiry_bound:
        download["expires_at"] = (datetime.now(UTC) + timedelta(seconds=0.5)).isoformat()
    else:
        download["download_policy"]["total_timeout_seconds"] = 0.5
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(fetch_verified(download, on_response=statuses.append))
    assert statuses == [503] and time.monotonic() - start < 2


def test_waiting_for_http_response_is_part_of_total_budget(download, rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    read = rig.staging.read

    def paused_read(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return read(*args, **kwargs)

    monkeypatch.setattr(rig.staging, "read", paused_read)
    download["download_policy"]["total_timeout_seconds"] = 0.5
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(fetch_verified(download))
        assert entered.is_set() and time.monotonic() - start < 2
    finally:
        release.set()


@pytest.mark.parametrize("failure", ["authorization", "digest"])
def test_denied_or_invalid_download_is_never_retried(download, failure):
    if failure == "authorization":
        download["authorization"] = "Bearer synthetic-wrong"
    else:
        download["sha256"] = "0" * 64
    statuses = []
    with pytest.raises(PeerDownloadError, match="download_rejected|invalid_download_digest"):
        asyncio.run(fetch_verified(download, on_response=statuses.append))
    assert statuses == ([403] if failure == "authorization" else [200])
