import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermes_return_probe import probe

from cf_agent_gateway.hermes.return_bridge import files


def test_linux_probe_metadata_is_exact_public_files():
    paths = probe._readonly_metadata_paths("posix", 42)
    assert paths == {
        Path(value).resolve()
        for value in (
            "/proc/stat",
            "/proc/version",
            "/proc/1/cgroup",
            "/proc/42/stat",
            "/dev/null",
            "/etc/os-release",
            "/usr/lib/os-release",
        )
    }
    assert probe._readonly_metadata_paths("nt", 42) == set()


def test_probe_metadata_exception_still_rejects_writes_and_nearby_files(tmp_path, monkeypatch):
    case = tmp_path / "private-case"
    case.mkdir()
    metadata = tmp_path / "system" / "os-release"
    events, hooks = [], []
    monkeypatch.setattr(probe, "_readonly_metadata_paths", lambda *_: {metadata})
    monkeypatch.setattr(probe, "record", lambda kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(probe.sys, "addaudithook", hooks.append)
    probe._install_child_audit(
        case / "source", case / "repository", case, {"base_url": "http://127.0.0.1:1/v1"}
    )
    audit = hooks[0]
    audit("open", (metadata, "r", os.O_RDONLY))
    audit("open", (case / "result.json", "w", os.O_WRONLY | os.O_CREAT))
    with pytest.raises(PermissionError, match="write outside"):
        audit("open", (metadata, "w", os.O_WRONLY))
    nearby = metadata.parent / "shadow"
    with pytest.raises(PermissionError, match="file outside"):
        audit("open", (nearby, "r", os.O_RDONLY))
    with pytest.raises(PermissionError, match="installed credentials"):
        audit("open", (tmp_path / ".env", "r", os.O_RDONLY))
    assert [item[0] for item in events] == ["probe_audit_denied"] * 3
    assert events[0][1]["path"] == str(metadata.resolve())
    assert events[1][1]["path"] == str(nearby.resolve())


def test_probe_posix_openat_exception_is_bound_to_reader_flags_and_task(tmp_path, monkeypatch):
    case = tmp_path / "case"
    root = case / "work" / "request-scope"
    monkeypatch.setattr(probe.os, "O_DIRECTORY", 0x10000, raising=False)
    monkeypatch.setattr(probe.os, "O_NOFOLLOW", 0x20000, raising=False)
    monkeypatch.setattr(probe.os, "O_NONBLOCK", 0x800, raising=False)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    caller = SimpleNamespace(
        f_code=files._posix_read.__code__, f_locals={"root": root, "directory": 10}
    )
    monkeypatch.setattr(probe.os, "readlink", lambda _: str(root))
    assert probe._pinned_task_open_path(root.anchor, directory_flags, caller, case) == Path(
        root.anchor
    )
    assert probe._pinned_task_open_path("input.pdf", file_flags, caller, case) == root / "input.pdf"
    assert probe._pinned_task_open_path("input.pdf", file_flags | os.O_WRONLY, caller, case) is None
    assert probe._pinned_task_open_path("../input.pdf", file_flags, caller, case) is None
    assert probe._pinned_task_open_path(root.anchor, file_flags, caller, case) is None
    monkeypatch.setattr(probe.os, "readlink", lambda _: str(root.parent))
    assert probe._pinned_task_open_path(root.name, directory_flags, caller, case) == root
    assert probe._pinned_task_open_path("other-task", directory_flags, caller, case) is None
    assert probe._pinned_task_open_path("parent.txt", file_flags, caller, case) is None
    caller.f_code = test_probe_posix_openat_exception_is_bound_to_reader_flags_and_task.__code__
    assert probe._pinned_task_open_path(root.anchor, directory_flags, caller, case) is None
