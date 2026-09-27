import base64
import hashlib
import json
import os
from dataclasses import replace

import pytest

from cf_agent_gateway.adapters.wechat.inbound_media import (
    ExpectedOriginal,
    parse_inbound_media,
)
from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult, MediaFetchError
from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging

DATA = b"%PDF-1.7\nsynthetic-not-real-doc\n"


def bound(data=DATA, **kwargs):
    payload = {"type": "file", "filename": "private-fixture.pdf",
               "data": base64.b64encode(data).decode()}
    return BoundMediaResult("a" * 64, parse_inbound_media(payload, **kwargs))


def test_atomic_no_overwrite_and_recovery_reuses_complete_bytes(tmp_path):
    tmp_path.chmod(0o700)
    store = InboundMediaStaging(tmp_path)
    b = bound(expected=ExpectedOriginal(len(DATA), hashlib.sha256(DATA).hexdigest()))
    first = store.publish(b)
    files = sorted(tmp_path.iterdir())
    assert len(files) == 2
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in files)
    assert all(p.stat().st_nlink == 1 for p in files)
    assert not first.existing
    second = store.publish(b)
    assert second.existing and first.reference == second.reference
    meta = json.loads((tmp_path / (first.reference + ".json")).read_bytes())
    assert meta["formal_archive"] is False
    assert meta["original_comparison"] == "match"
    assert "private-fixture" not in repr(second)


def test_variants_preserved_and_never_upgraded_to_original(tmp_path):
    tmp_path.chmod(0o700)
    store = InboundMediaStaging(tmp_path)
    expected = ExpectedOriginal(len(DATA), hashlib.sha256(DATA).hexdigest())
    original = store.publish(bound(expected=expected))
    variant = store.publish(bound(DATA + b"variant", expected=expected))
    assert original.reference != variant.reference
    assert variant.original_comparison == "different"
    assert len(list(tmp_path.iterdir())) == 4


def test_different_source_has_separate_storage(tmp_path):
    tmp_path.chmod(0o700)
    store = InboundMediaStaging(tmp_path)
    one = store.publish(bound())
    two = store.publish(replace(bound(), source_fingerprint="b" * 64))
    assert one.reference != two.reference


@pytest.mark.parametrize("mode", [0o755, 0o777, 0o750])
def test_nonprivate_root_is_not_changed(tmp_path, mode):
    tmp_path.chmod(mode)
    with pytest.raises(MediaFetchError, match="media_staging_permissions"):
        InboundMediaStaging(tmp_path).publish(bound())
    assert tmp_path.stat().st_mode & 0o777 == mode
    assert list(tmp_path.iterdir()) == []


def test_root_symlink_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(MediaFetchError, match="media_staging_io_failed"):
        InboundMediaStaging(link).publish(bound())
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("tamper", ["bytes", "mode", "hardlink", "symlink"])
def test_existing_blob_tamper_never_overwritten(tmp_path, tamper):
    tmp_path.chmod(0o700)
    store = InboundMediaStaging(tmp_path)
    initial = store.publish(bound())
    blob = tmp_path / (initial.reference + ".blob")
    if tamper == "bytes":
        blob.write_bytes(b"x" * len(DATA))
    elif tamper == "mode":
        blob.chmod(0o644)
    elif tamper == "hardlink":
        os.link(blob, tmp_path / "other")
    else:
        blob.rename(tmp_path / "original")
        blob.symlink_to(tmp_path / "original")
    with pytest.raises(MediaFetchError):
        store.publish(bound())
    if tamper == "bytes":
        assert blob.read_bytes() == b"x" * len(DATA)


def test_pending_cannot_create_even_root():
    b = BoundMediaResult("a" * 64, parse_inbound_media({"type": "pending"}))
    with pytest.raises(MediaFetchError, match="media_not_ready_for_staging"):
        InboundMediaStaging("/not-provisioned-cf-test-path").publish(b)


def test_forged_bytes_rejected_before_write(tmp_path):
    tmp_path.chmod(0o700)
    b = bound()
    b = replace(b, media=replace(b.media, sha256="0" * 64))
    with pytest.raises(MediaFetchError, match="media_not_ready_for_staging"):
        InboundMediaStaging(tmp_path).publish(b)
    assert list(tmp_path.iterdir()) == []


def test_manifest_failure_does_not_claim_published_or_delete_blob(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    store = InboundMediaStaging(tmp_path)
    original = store._publish

    def fault(fd, name, data):
        if name.endswith(".json"):
            raise OSError("synthetic disk full")
        return original(fd, name, data)

    monkeypatch.setattr(store, "_publish", fault)
    with pytest.raises(MediaFetchError, match="media_staging_io_failed"):
        store.publish(bound())
    assert len(list(tmp_path.glob("*.blob"))) == 1
    assert not list(tmp_path.glob("*.json"))
    # An explicit later recovery verifies and reuses the blob; no source replay.
    monkeypatch.setattr(store, "_publish", original)
    recovered = store.publish(bound())
    assert (tmp_path / (recovered.reference + ".blob")).read_bytes() == DATA
