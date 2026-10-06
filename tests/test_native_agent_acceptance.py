"""Offline checks only: no Hermes installation, model, credential, or sample access."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROBE_PATH = Path(__file__).parent / "native_agent_acceptance" / "run.py"
# Public, synthetic one-pixel PNG; not either acceptance sample.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6WQAAAABJRU5ErkJggg=="
)


def load_probe():
    spec = importlib.util.spec_from_file_location("native_acceptance_offline", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return load_probe()


def test_owned_process_success_preserves_output_without_a_model(probe, tmp_path):
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    result = probe.run_child(
        [sys.executable, "-I", "-S", "-c", "print('synthetic output')"],
        payload={},
        env=env,
        work=tmp_path,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "synthetic output"


def test_owned_process_timeout_kills_only_synthetic_tree_and_retains_output(probe, tmp_path):
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(90)']); "
        "print('synthetic tree started',flush=True); time.sleep(90)"
    )
    started = time.monotonic()
    result = probe.run_child(
        [sys.executable, "-I", "-S", "-c", code],
        payload={},
        env=env,
        work=tmp_path,
        timeout=1,
    )
    assert result.returncode == 124
    assert "synthetic tree started" in result.stdout
    assert "no retry" in result.stderr
    assert time.monotonic() - started < 20


def data_url(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def test_import_never_starts_model_process_or_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Import attempted real acceptance execution")

    for operation in ("run", "Popen", "check_output"):
        monkeypatch.setattr(subprocess, operation, forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    module = load_probe()
    assert callable(module.make_prompt)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": data_url(PNG)}},
        {"type": "input_image", "image_url": data_url(PNG)},
    ],
)
def test_image_evidence_observes_pixels_in_both_official_wire_formats(probe, part):
    body = {"input": [{"role": "user", "content": [part]}]}
    assert probe.image_evidence(body) == [
        {
            "media_type": "image/png",
            "bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest(),
        }
    ]


def test_image_evidence_accepts_jpeg_signature_without_claiming_decode(probe):
    # This only tests byte evidence, not image parsing or sample validity.
    jpeg = b"\xff\xd8\xff\xe0synthetic-jpeg-evidence\xff\xd9"
    evidence = probe.image_evidence(
        {"type": "input_image", "image_url": data_url(jpeg, "image/jpeg")}
    )
    assert evidence == [
        {
            "media_type": "image/jpeg",
            "bytes": len(jpeg),
            "sha256": hashlib.sha256(jpeg).hexdigest(),
        }
    ]


@pytest.mark.parametrize(
    "value",
    [
        {"meta": {"native_vision": True, "size_bytes": 123}},
        {"type": "text", "text": "Image attached natively for the main model"},
        {"type": "input_text", "text": data_url(PNG)},
        {"text": data_url(PNG)},
        data_url(PNG),
        {"type": "input_image", "image_url": "https://example.invalid/image.png"},
    ],
)
def test_image_markers_text_and_remote_urls_are_not_image_evidence(probe, value):
    assert probe.image_evidence(value) == []


@pytest.mark.parametrize(
    "value",
    [
        {"type": "input_image", "image_url": "data:image/png;base64,"},
        {"type": "input_image", "image_url": "data:image/png;base64,!broken!"},
        {"type": "input_image", "image_url": "data:image/png;base64"},
        {"type": "input_image", "image_url": data_url(b"not-image-bytes")},
        {"type": "input_image", "image_url": data_url(PNG, "image/jpeg")},
        {"type": "input_image", "image_url": data_url(PNG, "image/gif")},
    ],
)
def test_malformed_actual_image_parts_fail_closed(probe, value):
    with pytest.raises(ValueError):
        probe.image_evidence(value)


def test_write_new_persists_unicode_and_never_overwrites(probe, tmp_path):
    destination = tmp_path / "evidence.json"
    probe.write_new(destination, {"answer": "合成回答", "bytes": 8})
    original = destination.read_bytes()
    assert json.loads(original) == {"answer": "合成回答", "bytes": 8}
    with pytest.raises(FileExistsError):
        probe.write_new(destination, {"answer": "replacement"})
    assert destination.read_bytes() == original


@pytest.mark.parametrize("task,extension", [("pdf", "pdf"), ("image", "png")])
def test_prompt_has_only_new_task_interface_and_no_answers(probe, tmp_path, task, extension):
    remote_path = f"/SYNTHETIC-UNREAD.{extension}"
    work = tmp_path / "empty-task"
    prompt = probe.make_prompt(task, remote_path, work)
    assert remote_path in prompt and work.as_posix() in prompt
    assert "自己调用已有工具" in prompt
    assert "CF_ACCEPTANCE_HTTP_REFERENCE" in prompt
    assert "1048576" in prompt and "不覆盖" in prompt
    assert "不跟随重定向" in prompt and "保持主机名验证" in prompt
    assert "禁止服务器写入、删除或扫描" in prompt
    # The generator receives no sample bytes, credential values, answer key, or history.
    for contaminant in ("47行", "42/42", "expected.json", "核验 JSON", "已提取正文"):
        assert contaminant not in prompt
    assert not work.exists()
