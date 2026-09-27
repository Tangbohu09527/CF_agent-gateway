"""Real loopback Gateway reads through Linux private storage and native flock."""

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import test_inbound_media_queue as queue_tests
from sqlalchemy import select
from test_inbound_media_integration import gateway_server
from test_inbound_media_staging import bound

from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.identity.models import EnterpriseIdentity, IdentityStatus
from cf_agent_gateway.inbound.access import grant_read
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.message.models import Attachment, Message
from cf_agent_gateway.task.model import HermesDispatchRecordStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires Linux private dirfd storage")
rig = queue_tests.rig


@pytest.fixture
def ready_http(rig, tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    rig.staging = InboundMediaStaging(root)
    queue_tests.admit(rig)
    assert queue_tests.worker(rig).run_once() == "ready"
    settings = Settings(
        database=DatabaseSettings(url=str(rig.engine.url)),
        inbound_media=replace(rig.media, staging_root=str(root)),
    )
    with gateway_server(settings) as configured, rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="native-http")
        media = session.get(InboundMediaJob, 1)
        descriptor = grant_read(
            session, media, public_base_url=configured.inbound_media.public_base_url
        )
        session.commit()
        value = SimpleNamespace(
            rig=rig,
            root=root,
            blob=root / (media.manifest["reference"] + ".blob"),
            dispatch_id=record.id,
            descriptor=descriptor,
            headers={"Authorization": descriptor["authorization"]},
        )
        with httpx.Client(trust_env=False, timeout=10) as client:
            value.client = client
            value.url = descriptor["url"]
            yield value


@contextmanager
def locked(root):
    import fcntl

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def assert_unavailable(response, value, status):
    assert response.status_code == status
    assert response.json() == {"detail": "attachment read unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get("retry-after") == ("1" if status == 503 else None)
    assert str(value.root) not in response.text
    assert value.descriptor["authorization"] not in response.text


def assert_bytes(response, value):
    assert response.status_code == 200
    assert response.content == queue_tests.IMAGE
    assert len(response.content) == value.descriptor["size"]
    assert hashlib.sha256(response.content).hexdigest() == value.descriptor["sha256"]
    assert response.headers["cache-control"] == "no-store"


def test_authorized_busy_read_is_retryable_then_succeeds(ready_http):
    value = ready_http
    with locked(value.root):
        assert_unavailable(value.client.get(value.url, headers=value.headers), value, 503)
    assert_bytes(value.client.get(value.url, headers=value.headers), value)


@pytest.mark.parametrize("denial", ["missing", "wrong", "expired", "identity", "source"])
def test_denied_read_stays_opaque_even_when_storage_is_busy(ready_http, denial):
    value = ready_http
    headers = value.headers
    if denial == "missing":
        headers = {}
    elif denial == "wrong":
        headers = {"Authorization": "Bearer synthetic-invalid"}
    else:
        with value.rig.sessions() as session:
            if denial == "expired":
                session.get(InboundMediaJob, 1).read_expires_at = datetime.now(UTC) - timedelta(
                    seconds=1
                )
            elif denial == "identity":
                session.scalar(select(EnterpriseIdentity)).status = IdentityStatus.DISABLED
            else:
                session.get(Message, 1).content = "changed source"
            session.commit()
    with locked(value.root):
        assert_unavailable(value.client.get(value.url, headers=headers), value, 403)


@pytest.mark.parametrize("operation", ["read", "publish"])
def test_concurrent_native_read_or_publication_returns_bounded_busy(
    ready_http, monkeypatch, operation
):
    value = ready_http
    entered, release = threading.Event(), threading.Event()
    verify = InboundMediaStaging._verify

    def pause_under_native_lock(fd, name, expected, *, links=1):
        verify(fd, name, expected, links=links)
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "test did not release native storage holder"

    monkeypatch.setattr(InboundMediaStaging, "_verify", staticmethod(pause_under_native_lock))
    with ThreadPoolExecutor(max_workers=1) as pool:
        if operation == "read":
            holder = pool.submit(value.client.get, value.url, headers=value.headers)
        else:
            holder = pool.submit(value.rig.staging.publish, bound())
        try:
            assert entered.wait(10), "holder did not reach real file verification"
            assert_unavailable(value.client.get(value.url, headers=value.headers), value, 503)
        finally:
            release.set()
        result = holder.result(timeout=10)
        if operation == "read":
            assert_bytes(result, value)
    assert_bytes(value.client.get(value.url, headers=value.headers), value)


@pytest.mark.parametrize(
    "damage",
    ["bytes", "permissions", "root_permissions", "symlink", "hardlink", "registry", "missing"],
)
def test_corrupt_or_untrusted_registered_file_fails_closed(ready_http, damage):
    value = ready_http
    if damage == "bytes":
        value.blob.write_bytes(b"!" * len(queue_tests.IMAGE))
    elif damage == "permissions":
        value.blob.chmod(0o644)
    elif damage == "root_permissions":
        value.root.chmod(0o755)
    elif damage == "symlink":
        target = value.root.parent / "synthetic-target"
        value.blob.rename(target)
        value.blob.symlink_to(target)
    elif damage == "hardlink":
        os.link(value.blob, value.root.parent / "unaccounted-link")
    elif damage == "missing":
        value.blob.unlink()
    else:
        with value.rig.sessions() as session:
            session.scalar(select(Attachment)).hash = "0" * 64
            session.commit()
    assert_unavailable(value.client.get(value.url, headers=value.headers), value, 403)


@pytest.mark.parametrize("revocation", ["completed", "expired"])
def test_capability_revoked_during_file_read_never_returns_bytes(
    ready_http, monkeypatch, revocation
):
    value = ready_http
    entered, release = threading.Event(), threading.Event()
    read = InboundMediaStaging.read

    def pause_after_verified_read(staging, *args, **kwargs):
        data = read(staging, *args, **kwargs)
        entered.set()
        assert release.wait(10), "test did not release verified read"
        return data

    monkeypatch.setattr(InboundMediaStaging, "read", pause_after_verified_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(value.client.get, value.url, headers=value.headers)
        try:
            assert entered.wait(10), "request did not complete native verification"
            with value.rig.sessions() as session:
                if revocation == "completed":
                    HermesDispatchRecordStore(session).mark_success(
                        value.dispatch_id, claim_token="native-http"
                    )
                else:
                    session.get(InboundMediaJob, 1).read_expires_at = datetime.now(UTC) - timedelta(
                        seconds=1
                    )
                    session.commit()
        finally:
            release.set()
        assert_unavailable(request.result(timeout=10), value, 403)
