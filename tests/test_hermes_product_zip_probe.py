"""Probe contracts only: no service/model started; not end-to-end evidence."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "product_zip_probe_contract", Path(__file__).with_name("hermes_product_zip_probe.py")
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def body(*results):
    return {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                    },
                },
            }
            for name in (probe.PRODUCT, probe.RETURN, probe.INGEST)
        ],
        "messages": [{"role": "tool", "content": json.dumps(result)} for result in results],
    }


def test_inbound_planner_requires_current_ingest_before_inspection(tmp_path):
    planner = probe.Planner("/never-a-model-local-path", tmp_path)
    planner.attachment_id = 7
    call = planner.decide(body())["tool_calls"][0]["function"]
    assert call["name"] == probe.INGEST
    assert json.loads(call["arguments"]) == {"attachment_id": 7}
    assert planner.actions == ["ingest"]


def test_ingest_inspection_selects_only_returned_enterprise_path(tmp_path):
    planner = probe.Planner("/unused", tmp_path)
    planner.attachment_id = 7
    result = {
        "ok": True,
        "result": {
            "path": "/approved/incoming/3/original.zip",
            "sha256": "a" * 64,
            "manifest": [{"path": "sample.png", "size_bytes": 42, "sha256": "b" * 64}],
            "untrusted_content": True,
        },
    }
    call = planner.decide(body(result))["tool_calls"][0]["function"]
    args = json.loads(call["arguments"])
    assert call["name"] == probe.PRODUCT and args["action"] == "preview"
    assert args["path"] == result["result"]["path"]
    assert planner.archive == args["path"]


def test_ingest_failure_does_not_fall_back_to_arbitrary_path(tmp_path):
    planner = probe.Planner("/unused", tmp_path)
    planner.attachment_id = 7
    with pytest.raises(AssertionError, match="actual tool failed"):
        planner.decide(body({"ok": False, "error": {"code": "host_control_rejected"}}))
    assert not planner.actions


@pytest.mark.skipif(os.name != "nt", reason="Windows must keep POSIX staging prohibition")
def test_ingest_stops_before_creating_case_on_windows(monkeypatch, tmp_path):
    target = tmp_path / "not-created"
    args = ["probe"]
    for flag in (
        "hermes-source",
        "hermes-python",
        "filebridge-source",
        "worker",
        "file-client-config",
        "input-zip",
        "inbound-worker",
    ):
        args += ["--" + flag, str(Path(__file__).resolve())]
    args += [
        "--output",
        str(target),
        "--source",
        "fixture",
        "--root",
        "/approved",
        "--archive",
        "/approved/input.zip",
        "--ingest",
    ]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(AssertionError, match="POSIX"):
        probe.main()
    assert not target.exists()
