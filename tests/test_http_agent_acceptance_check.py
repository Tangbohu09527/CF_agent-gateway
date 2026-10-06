"""Synthetic HTTP evidence only; no approved samples, services, or credentials."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def checker():
    path = Path(__file__).parent / "http_agent_acceptance" / "check.py"
    spec = importlib.util.spec_from_file_location("http_acceptance_check_offline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def freeze(checker, case: Path) -> None:
    write(
        case / "frozen.json",
        {
            str(p.relative_to(case)): {
                "bytes": p.stat().st_size,
                "sha256": checker.sha(p.read_bytes()),
            }
            for p in case.rglob("*")
            if p.is_file() and p != case / "frozen.json"
        },
    )


@pytest.fixture
def case(checker, tmp_path):
    def build(task="pdf"):
        root = tmp_path / task
        work = root / "work"
        work.mkdir(parents=True)
        extension = "pdf" if task == "pdf" else "png"
        data = b"%PDF-synthetic" if task == "pdf" else b"\x89PNG\r\n\x1a\nsynthetic"
        path = work / ("offline." + extension)
        path.write_bytes(data)
        file = {"path": "/offline." + extension, "bytes": len(data), "sha256": checker.sha(data)}
        session = "synthetic-session-" + task
        endpoint = "api/sessions/" + session
        prompt, final = "Read the synthetic task file.", "Synthetic final answer."
        (root / "prompt.txt").write_text(prompt, encoding="utf-8")
        (root / "final.txt").write_text(final, encoding="utf-8")
        write(
            root / "intent.json",
            {
                "session_id": session,
                "entry": "cf_agent_gateway.hermes.client.HermesClient.chat",
                "task": task,
                "remote_path": file["path"],
            },
        )
        write(root / "result.json", {"client_accepted": True, "session_id": session})
        write(
            root / "downloads.json",
            {path.name: {"bytes": len(data), "sha256": checker.sha(data)}},
        )
        reader = "read_file" if task == "pdf" else "vision_analyze"
        native_result = (
            json.dumps({"extracted_document": True, "truncated": False, "content": "Document."})
            if task == "pdf"
            else "Image loaded into your context — you can see it natively now.\n[screenshot]"
        )
        receipt = {
            "local_path": str(path),
            "bytes": len(data),
            "sha256": checker.sha(data),
            "http_status": 200,
        }
        messages = [
            {"role": "user", "content": prompt},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "download", "function": {"name": "terminal", "arguments": "{}"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "download",
                "content": json.dumps({"output": json.dumps(receipt), "exit_code": 0}),
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read",
                        "function": {
                            "name": reader,
                            "arguments": json.dumps(
                                {"path" if task == "pdf" else "image_url": str(path)}
                            ),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read", "content": native_result},
            {"role": "assistant", "content": final},
        ]
        for message in messages:
            message["session_id"] = session
        traces = [
            (
                {"method": "GET", "endpoint": endpoint, "body": None},
                {"status": 404, "headers": {}, "body": {"error": "synthetic missing session"}},
            ),
            (
                {
                    "method": "POST",
                    "endpoint": "v1/chat/completions",
                    "body": {
                        "model": "hermes-agent",
                        "messages": [{"role": "user", "content": prompt}],
                    },
                },
                {
                    "status": 200,
                    "headers": {"X-Hermes-Session-Id": session},
                    "body": {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": final},
                            }
                        ]
                    },
                },
            ),
            (
                {"method": "GET", "endpoint": endpoint, "body": None},
                {"status": 200, "headers": {}, "body": {"id": session}},
            ),
            (
                {
                    "method": "GET",
                    "endpoint": endpoint + "/messages?limit=500&offset=0&order=oldest",
                    "body": None,
                },
                {
                    "status": 200,
                    "headers": {},
                    "body": {
                        "session_id": session,
                        "data": messages,
                        "pagination": {"limit": 500, "offset": 0, "order": "oldest", "returned": 6},
                    },
                },
            ),
        ]
        for i, (request, response) in enumerate(traces, 1):
            write(root / f"request-{i:02}.json", request)
            write(root / f"response-{i:02}.json", response)
        freeze(checker, root)
        return checker.load_frozen(root), file

    return build


def test_valid_pdf_evidence(checker, case):
    checks, _ = checker.score_case(*case())
    assert all(checks.values()), checks


@pytest.mark.parametrize("format", ["path", "scoped_path", "both"])
def test_verifier_path_formats(checker, case, format):
    evidence, file = case()
    if format != "path":
        file["scoped_path"] = file["path"]
    if format == "scoped_path":
        del file["path"]
    checks, _ = checker.score_case(evidence, file)
    assert all(checks.values()), checks


def test_conflicting_verifier_paths_rejected(checker, case):
    evidence, file = case()
    file["scoped_path"] = "/another.pdf"
    with pytest.raises(ValueError, match="path fields conflict"):
        checker.score_case(evidence, file)


def test_scoped_path_requires_exact_remote_match(checker, case):
    evidence, file = case()
    file["scoped_path"] = "/scope" + file.pop("path")
    assert not checker.score_case(evidence, file)[0]["approved_remote_path"]


def test_image_projection_never_claims_wire_observation(checker, case):
    checks, visibility = checker.score_case(*case("image"))
    assert all(checks.values()), checks
    assert visibility["model_transport_image_bytes"] == "unobserved"
    assert visibility["persisted_screenshot_placeholder"] is True
    assert visibility["session_image_parts"] == "absent_text_projection"
    assert visibility["native_vision_metadata"] is False
    assert "session_image_digest" not in checks


@pytest.mark.parametrize("flag,value", [("failed", True), ("partial", True), ("completed", False)])
def test_http_200_failure_not_success(checker, case, flag, value):
    evidence, file = case()
    evidence["responses"]["02"]["body"]["hermes"] = {flag: value}
    checks, _ = checker.score_case(evidence, file)
    assert not checks["http_completed"]


@pytest.mark.parametrize(
    "header,value",
    [("X-Hermes-Completed", "false"), ("X-Hermes-Partial", "true"), ("X-Hermes-Error", "failure")],
)
def test_explicit_header_failure_rejected(checker, case, header, value):
    evidence, file = case()
    evidence["responses"]["02"]["headers"][header] = value
    assert not checker.score_case(evidence, file)[0]["http_completed"]


def test_duplicate_submission_rejected(checker, case):
    evidence, file = case()
    evidence["requests"].append(("05", evidence["requests"][1][1]))
    checks, _ = checker.score_case(evidence, file)
    assert not checks["one_post"]
    assert not checks["get_only_after_post"]


def test_existing_session_rejected(checker, case):
    evidence, file = case()
    evidence["responses"]["01"]["status"] = 200
    assert not checker.score_case(evidence, file)[0]["fresh_get_404_before_post"]


@pytest.mark.parametrize("change", ["session", "offset", "order", "returned", "limit"])
def test_wrong_or_truncated_page_rejected(checker, case, change):
    evidence, file = case()
    page = evidence["responses"]["04"]["body"]
    if change == "session":
        page["session_id"] = "old-session"
    else:
        page["pagination"][change] = "latest" if change == "order" else 500
        if change == "limit":
            page["pagination"][change] = 10
    assert not checker.score_case(evidence, file)[0]["complete_session_page"]


def test_old_user_history_rejected(checker, case):
    evidence, file = case()
    evidence["responses"]["04"]["body"]["data"].insert(0, {"role": "user", "content": "old"})
    assert not checker.score_case(evidence, file)[0]["session_one_new_user"]


@pytest.mark.parametrize("mode", ["missing", "duplicate", "unapproved", "out_of_order"])
def test_tool_trace_rejections(checker, case, mode):
    evidence, file = case()
    messages = evidence["responses"]["04"]["body"]["data"]
    if mode == "missing":
        del messages[2]
    elif mode == "duplicate":
        messages.append(messages[2].copy())
    elif mode == "unapproved":
        messages[1]["tool_calls"][0]["function"]["name"] = "cf_filebridge_download"
    else:
        messages[1], messages[2] = messages[2], messages[1]
    checks, _ = checker.score_case(evidence, file)
    assert not checks[
        {
            "missing": "unique_paired_tools",
            "duplicate": "unique_paired_tools",
            "unapproved": "allowed_tools_only",
            "out_of_order": "tool_results_after_calls",
        }[mode]
    ]


def test_final_answer_must_match_http_and_session(checker, case):
    evidence, file = case()
    evidence["final"] = "A replaced answer"
    checks, _ = checker.score_case(evidence, file)
    assert not checks["final_matches_http"]
    assert not checks["final_matches_session"]


def test_wrong_reader_target_rejected(checker, case):
    evidence, file = case()
    messages = evidence["responses"]["04"]["body"]["data"]
    messages[3]["tool_calls"][0]["function"]["arguments"] = '{"path":"unrelated.pdf"}'
    assert not checker.score_case(evidence, file)[0]["reads_downloaded_file"]


@pytest.mark.parametrize("task", ["pdf", "image"])
@pytest.mark.parametrize("mode", ["reference", "unrelated", "duplicate"])
def test_additional_reader_targets(checker, case, task, mode):
    evidence, file = case(task)
    page = evidence["responses"]["04"]["body"]
    messages = page["data"]
    if mode == "duplicate":
        function = messages[3]["tool_calls"][0]["function"].copy()
    else:
        path = evidence["case"] / (
            "http-reference.json" if mode == "reference" else "unrelated.txt"
        )
        function = {"name": "read_file", "arguments": json.dumps({"path": str(path)})}
    messages[1:1] = [
        {
            "session_id": evidence["intent"]["session_id"],
            "role": "assistant",
            "tool_calls": [{"id": "extra-read", "function": function}],
        },
        {
            "session_id": evidence["intent"]["session_id"],
            "role": "tool",
            "tool_call_id": "extra-read",
            "content": '{"content":"non-secret synthetic reference"}',
        },
    ]
    page["pagination"]["returned"] = len(messages)
    checks, _ = checker.score_case(evidence, file)
    assert checks["reads_downloaded_file"] is (mode != "duplicate")
    assert checks["reader_targets_allowed"] is (mode != "unrelated")
    if mode == "reference":
        assert all(checks.values()), checks


@pytest.mark.parametrize("task", ["pdf", "image"])
def test_path_in_other_reader_argument_is_not_evidence(checker, case, task):
    evidence, file = case(task)
    function = evidence["responses"]["04"]["body"]["data"][3]["tool_calls"][0]["function"]
    args = json.loads(function["arguments"])
    correct = "path" if task == "pdf" else "image_url"
    function["arguments"] = json.dumps({correct: "unrelated", "prompt": args[correct]})
    checks, _ = checker.score_case(evidence, file)
    assert not checks["reads_downloaded_file"]
    assert not checks["reader_targets_allowed"]


def test_skill_view_remains_unapproved(checker, case):
    evidence, file = case()
    page = evidence["responses"]["04"]["body"]
    page["data"][1:1] = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "skill",
                    "function": {
                        "name": "skill_view",
                        "arguments": '{"name":"ocr-and-documents"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "skill", "content": '{"success":true}'},
    ]
    checks, _ = checker.score_case(evidence, file)
    assert checks["reads_downloaded_file"]
    assert not checks["allowed_tools_only"]


def test_download_receipt_and_digest_checked(checker, case):
    evidence, file = case()
    file["sha256"] = "0" * 64
    checks, _ = checker.score_case(evidence, file)
    assert not checks["download_sha256"]
    assert not checks["agent_download_receipt"]


def test_failed_terminal_receipt_is_not_download_proof(checker, case):
    evidence, file = case()
    tool = evidence["responses"]["04"]["body"]["data"][2]
    content = json.loads(tool["content"])
    content["exit_code"] = 1
    tool["content"] = json.dumps(content)
    assert not checker.score_case(evidence, file)[0]["agent_download_receipt"]


@pytest.mark.parametrize("key,value", [("extracted_document", False), ("truncated", True)])
def test_pdf_native_read_is_required(checker, case, key, value):
    evidence, file = case()
    tool = evidence["responses"]["04"]["body"]["data"][4]
    result = json.loads(tool["content"])
    result[key] = value
    tool["content"] = json.dumps(result)
    assert not checker.score_case(evidence, file)[0]["pdf_native_extraction"]


def test_marker_in_final_cannot_replace_actual_vision_result(checker, case):
    evidence, file = case("image")
    evidence["responses"]["04"]["body"]["data"][4]["content"] = "Tool failed"
    evidence["final"] += " Image loaded into your context [screenshot]"
    assert not checker.score_case(evidence, file)[0]["native_tool_projection"]


def test_explicit_session_image_parts_hash_but_not_provider_wire(checker, case):
    evidence, file = case("image")
    raw = (evidence["case"] / "work" / "offline.png").read_bytes()
    tool = evidence["responses"]["04"]["body"]["data"][4]
    tool["content"] = [
        {"type": "text", "text": "Image loaded into your context"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()},
        },
    ]
    checks, visibility = checker.score_case(evidence, file)
    assert checks["session_image_digest"]
    assert visibility["model_transport_image_bytes"] == "unobserved"
    file["sha256"] = "0" * 64
    assert not checker.score_case(evidence, file)[0]["session_image_digest"]


def test_text_base64_is_not_image_evidence(checker):
    text = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode()
    assert checker.image_parts({"type": "text", "text": text}) == []
    assert checker.image_parts({"type": "image_url", "image_url": {"url": text[:-3]}}) == []


@pytest.mark.parametrize("mode", ["modify", "add", "remove", "nested_manifest"])
def test_frozen_tree_tampering_rejected(checker, case, mode):
    evidence, _ = case()
    root = evidence["case"]
    if mode == "modify":
        (root / "final.txt").write_text("changed", encoding="utf-8")
    elif mode == "add":
        (root / "new.txt").write_text("changed", encoding="utf-8")
    elif mode == "remove":
        (root / "final.txt").unlink()
    else:
        (root / "work" / "frozen.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen evidence"):
        checker.load_frozen(root)


def test_evidence_escape_rejected(checker, case):
    evidence, file = case()
    root = evidence["case"]
    write(root / "downloads.json", {"../final.txt": {"bytes": 1, "sha256": "0" * 64}})
    with pytest.raises(ValueError, match="escapes"):
        checker.score_case(evidence, file)


def test_both_frozen_cases_checked_before_verifier_access(checker, case, tmp_path, monkeypatch):
    case()
    image, _ = case("image")
    (image["case"] / "final.txt").write_text("changed", encoding="utf-8")
    verifier = tmp_path / "must-not-open.json"
    original = Path.read_bytes

    def read(path):
        assert path != verifier, "Answer key was read before evidence integrity passed"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(ValueError, match="Frozen evidence bytes"):
        checker.evaluate(tmp_path, verifier, "0" * 64)


def test_existing_answer_scorer_reused_without_real_key(checker):
    expected = {
        "pdf": {"required_strings": ["OFFLINE"], "table_rows": [["X", 3]]},
        "image": {"visible_code": "TEST-ONLY", "regions": {}, "arrows": [["A", "B"]]},
    }
    assert all(checker.score_answers(expected, "OFFLINE\nX | 3", "TEST-ONLY A → B").values())


def test_junction_case_rejected_before_any_file_read(checker, tmp_path, monkeypatch):
    case = tmp_path / "junction-case"
    monkeypatch.setattr(Path, "is_junction", lambda path: path == case)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Must reject junction before reading manifest")

    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(ValueError, match="symbolic link"):
        checker.load_frozen(case)


@pytest.mark.parametrize("format", ["path", "scoped_path", "both", "conflict"])
def test_full_offline_report_keeps_visibility_gap(checker, case, tmp_path, format):
    pdf, pdf_file = case()
    image, image_file = case("image")
    for file in (pdf_file, image_file):
        if format != "path":
            file["scoped_path"] = file["path"]
        if format == "scoped_path":
            del file["path"]
        elif format == "conflict":
            file["scoped_path"] = "/different" + file["path"]
    verifier = tmp_path / "synthetic-verifier.json"
    write(
        verifier,
        {
            "files": [pdf_file, image_file],
            "verifier_only_expected": {
                "pdf": {"required_strings": ["Synthetic final answer."], "table_rows": []},
                "image": {"visible_code": "Synthetic final answer.", "regions": {}, "arrows": []},
            },
        },
    )
    if format == "conflict":
        with pytest.raises(ValueError, match="path fields conflict"):
            checker.evaluate(tmp_path, verifier, checker.sha(verifier.read_bytes()))
        return
    report = checker.evaluate(tmp_path, verifier, checker.sha(verifier.read_bytes()))
    assert report["passed"] == report["total"]
    assert report["failed"] == []
    assert report["answers_sha256"] == {
        "pdf": checker.sha(pdf["final"].encode()),
        "image": checker.sha(image["final"].encode()),
    }
    assert report["visibility"]["image"]["model_transport_image_bytes"] == "unobserved"
    with pytest.raises(ValueError, match="verifier digest differs"):
        checker.evaluate(tmp_path, verifier, "0" * 64)
