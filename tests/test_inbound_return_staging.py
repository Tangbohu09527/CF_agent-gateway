"""Trusted staging keeps the original binding, bytes and live inbound lease."""

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from test_host_binding_http import host as host_fixture
from test_inbound_media_queue import rig as queue_fixture
from test_return_bridge_scope import PDF, PNG, _capability

from cf_agent_gateway.hermes.return_bridge import plugin
from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError
from cf_agent_gateway.hermes.return_bridge.scope import ReturnScope

host = host_fixture
queue_rig = queue_fixture

BINDING = {
    "dispatch_id": 42,
    "message_id": 17,
    "enterprise_identity_id": "11111111-1111-4111-8111-111111111111",
    "thread_id": "33333333-3333-4333-8333-333333333333",
    "profile_reference": "approved",
    "profile_revision": 2,
    "session_id": "same-session",
    "task_id": "same-session",
    "turn_id": "current-turn",
}


class Guard:
    def __init__(self):
        self.active = True
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.active:
            raise RuntimeError("private host error must not escape")


@pytest.fixture
def execution(tmp_path, monkeypatch):
    root = tmp_path / "request"
    root.mkdir(mode=0o700)
    scope = ReturnScope(
        return_url="https://localhost/internal/hermes/returns/42",
        authorization=_capability(
            message_id=17,
            identity_id=BINDING["enterprise_identity_id"],
            thread_id=BINDING["thread_id"],
            profile="approved",
            revision=2,
        ),
        gateway_origin="https://localhost",
        request_id="synthetic-request",
        work_root=root,
        session_id="same-session",
    )

    async def no_network(*_args):
        raise AssertionError("unit test attempted unexpected network")

    monkeypatch.setattr(scope._transport, "_exchange", no_network)
    current = plugin.Execution(scope, "same-session", None)
    current.bind("same-session", "same-session", "current-turn")
    try:
        yield current
    finally:
        scope.close()


@contextmanager
def current_execution(current):
    token = plugin._execution.set(current)
    try:
        yield
    finally:
        plugin._execution.reset(token)


def stage(current, *, data=PDF, name="sample.pdf", guard=None, binding=None, **overrides):
    arguments = dict(
        attachment_id=5,
        binding=dict(BINDING if binding is None else binding),
        stream=io.BytesIO(data),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        filename=name,
        check_active=guard or Guard(),
    )
    arguments.update(overrides)
    with current_execution(current):
        return plugin.tool_execution(
            tool_name="filebrowser_read_inbound",
            args={},
            next_call=lambda _: plugin.prepare_inbound_return(**arguments),
            session_id="same-session",
            task_id="same-session",
            turn_id="current-turn",
            tool_call_id="official-read-call",
        )


def error_result(value, code):
    assert json.loads(value) == {"success": False, "error": code}


@pytest.mark.parametrize(
    ("data", "name", "kind"), [(PDF, "sample.pdf", "file"), (PNG, "sample.png", "image")]
)
def test_native_read_turn_stages_exact_bytes_and_safe_reference(execution, data, name, kind):
    guard = Guard()
    result = stage(execution, data=data, name=name, guard=guard)
    assert set(result) == {"file_ref", "kind", "filename", "size_bytes", "sha256"}
    assert result["kind"] == kind and result["filename"] == name
    assert result["size_bytes"] == len(data)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert "/" not in result["file_ref"] and "\\" not in result["file_ref"]
    assert str(execution.scope.work_root) not in json.dumps(result)
    assert (execution.scope.work_root / result["file_ref"]).read_bytes() == data
    assert guard.calls >= 4


@pytest.mark.parametrize("field", list(BINDING))
def test_every_host_binding_field_must_match_current_return_execution(execution, field):
    changed = dict(BINDING)
    changed[field] = 99 if type(changed[field]) is int else "foreign"
    error_result(stage(execution, binding=changed), "inbound_return_binding_mismatch")
    assert list(execution.scope.work_root.iterdir()) == []


@pytest.mark.parametrize(
    "field",
    ["dispatch_id", "message_id", "enterprise_identity_id", "thread_id", "profile_revision"],
)
def test_boolean_identity_is_not_an_integer(execution, field):
    error_result(
        stage(execution, binding={**BINDING, field: True}), "inbound_return_binding_mismatch"
    )


def test_prepare_requires_exact_read_marker_and_return_requires_own_tool(execution):
    with current_execution(execution):
        with pytest.raises(ReturnBridgeError, match="return_execution_unavailable"):
            plugin.prepare_inbound_return(
                attachment_id=5,
                binding=BINDING,
                stream=io.BytesIO(PDF),
                size_bytes=len(PDF),
                sha256=hashlib.sha256(PDF).hexdigest(),
                filename="sample.pdf",
                check_active=Guard(),
            )
        result = plugin.tool_execution(
            tool_name="filebrowser_read_inbound",
            args={},
            next_call=lambda _: plugin.handle_return({"file_ref": "x.pdf", "kind": "file"}),
            session_id="same-session",
            task_id="same-session",
            turn_id="current-turn",
            tool_call_id="read",
        )
        error_result(result, "return_execution_unavailable")


@pytest.mark.parametrize(
    "change",
    [
        {"size_bytes": 1},
        {"sha256": "0" * 64},
        {"filename": "../x.pdf"},
        {"attachment_id": True},
    ],
)
def test_invalid_metadata_or_changed_bytes_never_creates_file(execution, change):
    assert json.loads(stage(execution, **change))["success"] is False
    assert list(execution.scope.work_root.iterdir()) == []


def test_same_attachment_reuses_reference_and_retains_first_guard(execution):
    original = Guard()
    first = stage(execution, guard=original)
    assert stage(execution) == first
    assert len(list(execution.scope.work_root.iterdir())) == 1
    original.active = False
    error_result(stage(execution), "inbound_return_inactive")


def test_repeat_cannot_change_filename_or_bytes(execution):
    stage(execution)
    error_result(stage(execution, name="other.pdf"), "inbound_return_conflict")
    error_result(stage(execution, data=PDF.replace(b"input", b"other")), "inbound_return_conflict")
    assert len(list(execution.scope.work_root.iterdir())) == 1


def test_concurrent_same_attachment_stages_once(execution):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result(timeout=10) for f in [pool.submit(stage, execution) for _ in range(4)]]
    assert all(result == results[0] for result in results)
    assert len(list(execution.scope.work_root.iterdir())) == 1


def test_cancelled_source_denies_before_and_during_copy(execution):
    inactive = Guard()
    inactive.active = False
    error_result(stage(execution, guard=inactive), "inbound_return_inactive")
    active = Guard()

    class CancellingStream(io.BytesIO):
        def read(self, count):
            value = super().read(count)
            active.active = False
            return value

    error_result(
        stage(execution, guard=active, stream=CancellingStream(PDF)), "inbound_return_inactive"
    )
    assert list(execution.scope.work_root.iterdir()) == []


def test_return_file_and_cached_receipt_keep_original_lease(execution, monkeypatch):
    guard = Guard()
    prepared = stage(execution, guard=guard)
    calls = []

    async def exchange(_scope, item, method, _headers, _remaining):
        calls.append(method)
        return 200, "0", item.expected

    monkeypatch.setattr(execution.scope._transport, "_exchange", exchange)
    args = {key: prepared[key] for key in ("file_ref", "kind", "filename")}
    first = execution.scope.return_file(**args)
    assert first["success"] and calls == ["PUT"]
    assert execution.scope.return_file(**args) == first and calls == ["PUT"]
    guard.active = False
    with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
        execution.scope.return_file(**args)
    assert calls == ["PUT"]


def test_no_new_slot_by_renaming_prepared_inbound(execution):
    result = stage(execution)
    with pytest.raises(ReturnBridgeError, match="inbound_return_conflict"):
        execution.scope.return_file(result["file_ref"], result["kind"], "renamed.pdf")
    assert execution.scope._publications == {}


def test_staged_file_drift_is_rejected_before_transport(execution):
    result = stage(execution)
    (execution.scope.work_root / result["file_ref"]).write_bytes(PDF.replace(b"input", b"other"))
    with pytest.raises(ReturnBridgeError, match="inbound_return_conflict"):
        execution.scope.return_file(result["file_ref"], result["kind"], result["filename"])
    assert execution.scope._publications == {}


def test_revoked_request_cannot_stage(execution):
    execution.scope.revoke()
    error_result(stage(execution), "return_scope_closed")
    assert list(execution.scope.work_root.iterdir()) == []


def test_failed_creation_does_not_overwrite_existing_leaf(execution, monkeypatch):
    from types import SimpleNamespace

    from cf_agent_gateway.hermes.return_bridge import staging

    monkeypatch.setattr(staging, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    existing = execution.scope.work_root / ("inbound-" + "a" * 32 + ".pdf")
    existing.write_bytes(b"unrelated evidence")
    error_result(stage(execution), "inbound_return_file_unavailable")
    assert existing.read_bytes() == b"unrelated evidence"
    error_result(stage(execution), "inbound_return_conflict")


def test_linked_root_is_rejected_without_writing_target(execution, tmp_path):
    from test_return_bridge_plugin import _directory_link

    target = tmp_path / "foreign"
    target.mkdir()
    link = tmp_path / "link"
    _directory_link(target, link)
    execution.scope.work_root = link
    error_result(stage(execution), "inbound_return_file_unavailable")
    assert list(target.iterdir()) == []


def test_partial_creation_remains_guarded_and_cannot_be_returned_as_generated(
    execution, monkeypatch
):
    from cf_agent_gateway.hermes.return_bridge import scope as scope_module

    guard = Guard()

    def cancelled_write(root, file, _active, _created):
        (root / file.reference).write_bytes(file.content)
        guard.active = False
        raise ReturnBridgeError("inbound_return_inactive")

    monkeypatch.setattr(scope_module, "write_staged_file", cancelled_write)
    error_result(stage(execution, guard=guard), "inbound_return_inactive")
    leaf = next(execution.scope.work_root.iterdir())
    with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
        execution.scope.return_file(leaf.name, "file", "sample.pdf")
    assert execution.scope._publications == {}


def test_ambiguous_put_cannot_query_after_original_source_expires(execution, monkeypatch):
    import httpx

    guard = Guard()
    prepared = stage(execution, guard=guard)
    calls = []

    async def exchange(_scope, _item, method, _headers, _remaining):
        calls.append(method)
        guard.active = False
        raise httpx.ReadError("synthetic dropped upload")

    monkeypatch.setattr(execution.scope._transport, "_exchange", exchange)
    with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
        execution.scope.return_file(prepared["file_ref"], prepared["kind"], prepared["filename"])
    assert calls == ["PUT"]


def test_expiry_during_upload_prevents_success_projection(execution, monkeypatch):
    guard = Guard()
    prepared = stage(execution, guard=guard)

    async def exchange(_scope, item, _method, _headers, _remaining):
        guard.active = False
        return 200, "0", item.expected

    monkeypatch.setattr(execution.scope._transport, "_exchange", exchange)
    with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
        execution.scope.return_file(prepared["file_ref"], prepared["kind"], prepared["filename"])


def test_staged_file_read_rechecks_lease_per_chunk(execution, monkeypatch):
    from cf_agent_gateway.hermes.return_bridge import files

    guard = Guard()
    prepared = stage(execution, guard=guard)
    original_read = files.os.read

    def expire_after_read(descriptor, size):
        content = original_read(descriptor, size)
        guard.active = False
        return content

    monkeypatch.setattr(files.os, "read", expire_after_read)
    with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
        execution.scope.return_file(prepared["file_ref"], prepared["kind"], prepared["filename"])
    assert execution.scope._publications == {}


def test_ordinary_read_without_return_authority_unchanged():
    assert (
        plugin.tool_execution(
            tool_name="filebrowser_read_inbound", args={}, next_call=lambda _: "ordinary-read"
        )
        == "ordinary-read"
    )


def test_capability_logs_only_fixed_metadata(execution, caplog):
    import logging

    caplog.set_level(logging.INFO, logger=plugin.__name__)
    stage(execution)
    plugin._capability_event("registered")
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert set(record.args[:2]) <= {"inbound-staging-v1", "registered", "inbound_prepared"}
        assert len(record.args[2]) == 64
        assert "same-session" not in record.getMessage()
        assert "sample.pdf" not in record.getMessage()
        assert str(execution.scope.work_root) not in record.getMessage()


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows case-insensitive path aliases")
def test_case_alias_cannot_bypass_original_guard_or_allocate_another_slot(execution, monkeypatch):
    from cf_agent_gateway.hermes.return_bridge import files

    guard = Guard()
    prepared = stage(execution, guard=guard)
    calls = []
    monkeypatch.setattr(files.os, "read", lambda *_: calls.append("read"))
    guard.active = False
    with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
        execution.scope.return_file(prepared["file_ref"].upper(), "file", prepared["filename"])
    assert calls == [] and execution.scope._publications == {}


def test_same_bytes_replacement_does_not_rebind_prepared_file(execution):
    prepared = stage(execution)
    path = execution.scope.work_root / prepared["file_ref"]
    path.rename(path.with_suffix(".old"))
    path.write_bytes(PDF)
    with pytest.raises(ReturnBridgeError, match="inbound_return_conflict"):
        execution.scope.return_file(prepared["file_ref"], "file", prepared["filename"])
    assert execution.scope._publications == {}


def test_different_request_cannot_return_previous_request_reference(execution, tmp_path):
    prepared = stage(execution)
    other = tmp_path / "other-request"
    other.mkdir()
    scope = ReturnScope(
        return_url="https://localhost/internal/hermes/returns/42",
        authorization=_capability(
            message_id=17,
            identity_id=BINDING["enterprise_identity_id"],
            thread_id=BINDING["thread_id"],
            profile="approved",
            revision=2,
        ),
        gateway_origin="https://localhost",
        request_id="another-request",
        work_root=other,
        session_id="same-session",
    )
    try:
        with pytest.raises(ReturnBridgeError, match="file_unavailable"):
            scope.return_file(prepared["file_ref"], "file", prepared["filename"])
        assert scope._publications == {}
    finally:
        scope.close()


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows case-insensitive path aliases")
def test_active_case_alias_is_rejected_before_read_or_second_slot(execution, monkeypatch):
    from cf_agent_gateway.hermes.return_bridge import files

    prepared = stage(execution)
    reads = []
    monkeypatch.setattr(files.os, "read", lambda *_: reads.append(True))
    with pytest.raises(ReturnBridgeError, match="inbound_return_conflict"):
        execution.scope.return_file(prepared["file_ref"].upper(), "file", prepared["filename"])
    assert reads == [] and execution.scope._publications == {}


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows 8.3 aliases")
def test_native_short_name_alias_checks_guard_before_read(execution, monkeypatch):
    import ctypes
    from ctypes import wintypes
    from pathlib import Path

    from cf_agent_gateway.hermes.return_bridge import files

    guard = Guard()
    prepared = stage(execution, guard=guard)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    short_path = kernel.GetShortPathNameW
    short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    short_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = short_path(str(execution.scope.work_root / prepared["file_ref"]), buffer, len(buffer))
    assert 0 < length < len(buffer)
    short_name = Path(buffer.value).name
    if short_name.casefold() == prepared["file_ref"].casefold():
        pytest.skip("this isolated volume does not assign an 8.3 alias")
    reads = []
    monkeypatch.setattr(files.os, "read", lambda *_: reads.append(True))
    guard.active = False
    with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
        execution.scope.return_file(short_name, "file", prepared["filename"])
    assert reads == [] and execution.scope._publications == {}


def test_schema_directs_inbound_handle_to_read_prepare_then_return():
    description = json.dumps(plugin.TOOL_SCHEMA, ensure_ascii=False)
    assert "filebrowser_read_inbound" in description
    assert "prepare_return=true" in description
    assert "opaque" in description
    assert "attachment ID" in description
    assert "source/path" in description


# Reuse real admission/claim/host-control HTTP fixtures. Only upstream media bytes
# are a deterministic substitute; identity types are produced by Gateway itself.


@pytest.fixture
def rig(queue_rig, monkeypatch):
    import base64

    from test_inbound_media_queue import Fetcher

    from cf_agent_gateway.adapters.wechat.inbound_media import parse_inbound_media
    from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult

    def fetch(_self, source):
        return BoundMediaResult(
            source.fingerprint,
            parse_inbound_media(
                {
                    "type": "image",
                    "filename": "fixture.png",
                    "data": base64.b64encode(PNG).decode(),
                }
            ),
        )

    monkeypatch.setattr(Fetcher, "fetch", fetch)
    return queue_rig


def test_real_gateway_signed_return_context_matches_actual_host_control_response(
    host, tmp_path, monkeypatch
):
    from test_host_binding_http import resolve

    from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
    from cf_agent_gateway.artifact.return_handoff import issue_context
    from cf_agent_gateway.task.model import HermesDispatchRecord

    queue, client, body, host_binding, app_settings = host
    response = resolve(client, body)
    assert response.status_code == 200
    verified = response.json()
    monkeypatch.setenv("TEST_INBOUND_RETURN_CONTRACT_KEY", "synthetic-contract-" + "x" * 40)
    settings = ArtifactReturnSettings(
        enabled=True,
        host_contract_confirmed=True,
        public_base_url="https://gateway.invalid",
        profile_reference=app_settings.host_binding.profile_reference,
        profile_revision=app_settings.host_binding.profile_revision,
        signing_key_env="TEST_INBOUND_RETURN_CONTRACT_KEY",
    )
    with queue.sessions() as session:
        record = session.get(HermesDispatchRecord, host_binding.dispatch_id)
        context = issue_context(session, record, settings, session_id=verified["session_id"])
        session.commit()
    root = tmp_path / "real-contract-work"
    root.mkdir(mode=0o700)
    scope = ReturnScope(
        return_url=context["url"],
        authorization=context["authorization"],
        gateway_origin=settings.public_base_url,
        request_id="real-contract-test",
        work_root=root,
        session_id=verified["session_id"],
    )
    binding = {key: verified[key] for key in BINDING if key != "turn_id"}
    binding["turn_id"] = "official-turn"
    current = plugin.Execution(scope, binding["session_id"], None)
    current.bind(binding["session_id"], binding["task_id"], binding["turn_id"])
    descriptor = verified["attachments"][0]
    try:
        with current_execution(current):
            result = plugin.tool_execution(
                tool_name="filebrowser_read_inbound",
                args={},
                session_id=binding["session_id"],
                task_id=binding["task_id"],
                turn_id=binding["turn_id"],
                tool_call_id="official-call",
                next_call=lambda _: plugin.prepare_inbound_return(
                    attachment_id=descriptor["attachment_id"],
                    binding=binding,
                    stream=io.BytesIO(PNG),
                    size_bytes=descriptor["size"],
                    sha256=descriptor["sha256"],
                    filename=descriptor["filename"],
                    check_active=Guard(),
                ),
            )
        assert isinstance(result, dict), result
        assert result["kind"] == "image" and result["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert (root / result["file_ref"]).read_bytes() == PNG
    finally:
        scope.close()


def test_failed_attempt_retains_original_guard_before_reading_on_retry(execution, monkeypatch):
    from cf_agent_gateway.hermes.return_bridge import scope as scope_module

    original = Guard()

    def fail_write(*_args):
        raise ReturnBridgeError("inbound_return_file_unavailable")

    monkeypatch.setattr(scope_module, "write_staged_file", fail_write)
    error_result(stage(execution, guard=original), "inbound_return_file_unavailable")
    original.active = False
    reads = []

    class RecordingStream(io.BytesIO):
        def read(self, count):
            reads.append(True)
            return super().read(count)

    error_result(stage(execution, stream=RecordingStream(PDF)), "inbound_return_inactive")
    assert reads == []


def test_async_transport_watcher_observes_original_inbound_revocation(execution):
    import asyncio

    from cf_agent_gateway.hermes.return_bridge.transport import Publication, expected_receipt

    guard = Guard()
    prepared = stage(execution, guard=guard)
    staged = execution.scope._inbound_refs[prepared["file_ref"]]
    item = Publication(
        0,
        staged.file,
        expected_receipt(staged.file, artifact_id="synthetic", response_id="synthetic"),
        check_source=staged.check_active,
    )

    async def run():
        watching = asyncio.create_task(
            execution.scope._transport._watch_revocation(execution.scope, item)
        )
        await asyncio.sleep(0)
        guard.active = False
        with pytest.raises(ReturnBridgeError, match="inbound_return_inactive"):
            await asyncio.wait_for(watching, 1)

    asyncio.run(run())
