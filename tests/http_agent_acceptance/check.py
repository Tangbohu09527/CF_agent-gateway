"""Offline checks for frozen, once-only Hermes HTTP acceptance evidence.

The answer key is opened only after both frozen case trees pass integrity checks.
HTTP session projections are not model transport captures: image visibility gaps
remain explicit even when the independent answer checks pass.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

PNG_RULES = Path(__file__).with_name("png-rules-v2.json")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def inside(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Evidence path escapes its task directory")
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError("Evidence contains a symbolic link")
    return path


def load_frozen(case: Path) -> dict:
    if case.is_symlink() or case.is_junction():
        raise ValueError("Evidence case is a symbolic link")
    frozen = read_json(case / "frozen.json")
    paths = list(case.rglob("*"))
    for path in paths:
        inside(case, str(path.relative_to(case)))
    actual = {str(p.relative_to(case)) for p in paths if p.is_file() and p != case / "frozen.json"}
    if set(frozen) != actual:
        raise ValueError("Frozen evidence file set changed")
    for relative, proof in frozen.items():
        data = inside(case, relative).read_bytes()
        if len(data) != proof["bytes"] or sha(data) != proof["sha256"]:
            raise ValueError("Frozen evidence bytes changed")
    return {
        "case": case,
        "intent": read_json(case / "intent.json"),
        "result": read_json(case / "result.json"),
        "final": (case / "final.txt").read_text(encoding="utf-8")
        if (case / "final.txt").exists()
        else "",
        "requests": [(p.stem[8:], read_json(p)) for p in sorted(case.glob("request-*.json"))],
        "responses": {p.stem[9:]: read_json(p) for p in sorted(case.glob("response-*.json"))},
    }


def as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def text_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(p.get("text", "")) for p in value if isinstance(p, dict))
    return ""


def image_parts(value) -> list[dict]:
    """Hash only explicit typed image data, never arbitrary base64 in text."""
    images = []
    if isinstance(value, list):
        for part in value:
            images.extend(image_parts(part))
    elif isinstance(value, dict):
        kind = value.get("type")
        url = value.get("image_url", value.get("url"))
        if isinstance(url, dict):
            url = url.get("url")
        if (
            kind in ("image_url", "input_image")
            and isinstance(url, str)
            and url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,"))
        ):
            try:
                raw = base64.b64decode(url.split(",", 1)[1], validate=True)
            except ValueError:
                raw = b""
            if raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                images.append({"bytes": len(raw), "sha256": sha(raw)})
        for child in value.values():
            if isinstance(child, (list, dict)):
                images.extend(image_parts(child))
    return images


def response_complete(response: dict) -> bool:
    body = as_dict(response.get("body"))
    headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
    choices = body.get("choices", [])
    flags = body.get("hermes", {})
    # Pinned official success omits hermes/Completed headers. An explicit
    # contrary value still fails, including HTTP 200 with hermes.failed=true.
    return (
        response.get("status") == 200
        and len(choices) == 1
        and choices[0].get("finish_reason") == "stop"
        and choices[0].get("message", {}).get("role") == "assistant"
        and isinstance(flags, dict)
        and flags.get("completed", True) is True
        and flags.get("failed", False) is False
        and flags.get("partial", False) is False
        and not flags.get("error")
        and not body.get("error")
        and headers.get("x-hermes-completed", "true").lower() == "true"
        and headers.get("x-hermes-partial", "false").lower() == "false"
        and not headers.get("x-hermes-error")
    )


def verifier_file_path(file: dict) -> str:
    """Use the scoped production format, retaining older synthetic fixtures."""
    if "scoped_path" in file and "path" in file and file["scoped_path"] != file["path"]:
        raise ValueError("Verifier file path fields conflict")
    path = file.get("scoped_path", file.get("path"))
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Verifier file needs an absolute scoped path")
    return path


def allowed_tools(case: Path, intent: dict, calls: list, by_id: dict) -> tuple[bool, dict]:
    """Versioned evidence check, not a permissions boundary for the live Agent."""
    basic = {"terminal", "read_file", "vision_analyze"}
    version = intent.get("rules_version")
    if "rules_version" not in intent:
        # Missing version is the original strict contract, including the frozen PDF.
        return bool(calls) and all(c.get("function", {}).get("name") in basic for c in calls), {}
    valid = False
    rules, proof = {}, {}
    try:
        rules_raw = (case / "rules.json").read_bytes()
        rules = json.loads(rules_raw)
        pinned = read_json(PNG_RULES)
        proof = read_json(case / "skill-preflight.json")
        valid = (
            version == "hermes-http-png-v2"
            and intent.get("task") == "image"
            and rules == pinned
            and intent.get("rules_sha256") == proof.get("rules_sha256") == sha(rules_raw)
            and proof.get("sha256") == {"skill": rules["skill"]["sha256"], **rules["loader_sha256"]}
            and isinstance(proof.get("skill_file"), str)
            and bool(proof["skill_file"])
            and isinstance(proof.get("source_root"), str)
            and bool(proof["source_root"])
            and isinstance(proof.get("config_sha256"), str)
            and len(proof["config_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in proof["config_sha256"])
            and proof.get("resolver")
            == "default profile skills; no external or trusted project roots"
            and read_json(case / "skill-postflight.json") == proof
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    skills = [c for c in calls if c.get("function", {}).get("name") == "skill_view"]

    def pinned_description(call: dict) -> bool:
        if not valid:
            return False
        result = as_dict(by_id.get(call.get("id"), {}).get("content"))
        content = result.get("content")
        return (
            as_dict(call["function"].get("arguments")) == {"name": rules["skill"]["name"]}
            and result.get("success") is True
            and result.get("name") == rules["skill"]["name"]
            and isinstance(result.get("_source_path"), str)
            and result.get("_source_path", "").replace("\\", "/")
            == proof["skill_file"].replace("\\", "/")
            and isinstance(content, str)
            and sha(content.encode("utf-8")) == rules["skill"]["content_sha256"]
            and result.get("readiness_status") == "available"
            and result.get("setup_needed") is False
            and result.get("setup_skipped") is False
            and all(
                result.get(key) == []
                for key in (
                    "required_environment_variables",
                    "required_commands",
                    "missing_required_environment_variables",
                    "missing_credential_files",
                    "missing_required_commands",
                )
            )
            and not any(
                result.get(key)
                for key in ("deps_note", "setup_note", "setup_help", "gateway_setup_hint", "error")
            )
            and "file" not in result
        )

    descriptions = len(skills) <= 1 and all(pinned_description(c) for c in skills)
    allowed = (
        valid
        and descriptions
        and bool(calls)
        and all(c.get("function", {}).get("name") in basic | {"skill_view"} for c in calls)
    )
    return allowed, {"png_v2_pinned_rule": valid, "png_v2_skill_descriptions_only": descriptions}


def score_case(evidence: dict, expected_file: dict) -> tuple[dict[str, bool], dict]:
    case, intent = evidence["case"], evidence["intent"]
    requests, responses = evidence["requests"], evidence["responses"]
    session = intent["session_id"]
    session_endpoint = "api/sessions/" + session
    posts = [(number, req) for number, req in requests if req.get("method") == "POST"]
    checks = {"one_post": len(posts) == 1}
    post_number, post = posts[0] if posts else ("", {})
    response = responses.get(post_number, {})
    checks["gateway_chat_entry"] = (
        intent.get("entry") == "cf_agent_gateway.hermes.client.HermesClient.chat"
        and post.get("endpoint") == "v1/chat/completions"
    )
    checks["fresh_get_404_before_post"] = any(
        number < post_number
        and req == {"method": "GET", "endpoint": session_endpoint, "body": None}
        and responses.get(number, {}).get("status") == 404
        for number, req in requests
    )
    checks["get_only_after_post"] = all(
        req.get("method") == "GET" for number, req in requests if number > post_number
    )
    checks["client_accepted"] = evidence["result"].get("client_accepted") is True
    checks["http_completed"] = response_complete(response)
    headers = {k.lower(): v for k, v in response.get("headers", {}).items()}
    checks["response_session"] = (
        headers.get("x-hermes-session-id") == session
        and evidence["result"].get("session_id") == session
    )
    choices = response.get("body", {}).get("choices", [])
    checks["final_matches_http"] = (
        len(choices) == 1
        and bool(evidence["final"])
        and choices[0].get("message", {}).get("content") == evidence["final"]
    )
    prompt = (case / "prompt.txt").read_text(encoding="utf-8")
    checks["request_one_user_task"] = post.get("body", {}).get("messages") == [
        {"role": "user", "content": prompt}
    ]
    pages = [
        responses.get(number, {})
        for number, req in requests
        if number > post_number
        and req.get("method") == "GET"
        and req.get("endpoint") == session_endpoint + "/messages?limit=500&offset=0&order=oldest"
    ]
    page = pages[0].get("body", {}) if len(pages) == 1 else {}
    messages = page.get("data", [])
    pagination = page.get("pagination", {})
    checks["complete_session_page"] = (
        len(pages) == 1
        and pages[0].get("status") == 200
        and page.get("session_id") == session
        and pagination.get("order") == "oldest"
        and pagination.get("offset") == 0
        and pagination.get("limit") == 500
        and pagination.get("returned") == len(messages) < 500
        and all(m.get("session_id") == session for m in messages)
    )
    users = [m for m in messages if m.get("role") == "user"]
    checks["session_one_new_user"] = len(users) == 1 and users[0].get("content") == prompt
    final_assistants = [
        m for m in messages if m.get("role") == "assistant" and not m.get("tool_calls")
    ]
    checks["final_matches_session"] = (
        bool(final_assistants) and final_assistants[-1].get("content") == evidence["final"]
    )
    calls = [c for m in messages for c in (m.get("tool_calls") or [])]
    replies = [m for m in messages if m.get("role") == "tool"]
    call_ids = [c.get("id") for c in calls]
    reply_ids = [m.get("tool_call_id") for m in replies]
    checks["unique_paired_tools"] = (
        bool(calls)
        and None not in call_ids
        and len(set(call_ids)) == len(call_ids)
        and len(set(reply_ids)) == len(reply_ids)
        and set(call_ids) == set(reply_ids)
    )
    by_id = {m.get("tool_call_id"): m for m in replies}
    checks["allowed_tools_only"], rule_checks = allowed_tools(case, intent, calls, by_id)
    checks.update(rule_checks)
    checks["terminal_used"] = any(c.get("function", {}).get("name") == "terminal" for c in calls)
    checks["tool_results_after_calls"] = all(
        next((i for i, m in enumerate(messages) if c in (m.get("tool_calls") or [])), -1)
        < next((i for i, m in enumerate(messages) if m.get("tool_call_id") == c.get("id")), -1)
        for c in calls
    )
    downloads = read_json(case / "downloads.json")
    checks["one_download"] = len(downloads) == 1
    checks["approved_remote_path"] = intent.get("remote_path") == verifier_file_path(expected_file)
    visibility = {"model_transport_image_bytes": "unobserved"}
    if len(downloads) != 1:
        return checks, visibility
    relative, proof = next(iter(downloads.items()))
    path = inside(case / "work", relative)
    data = path.read_bytes()
    checks["download_bytes"] = proof["bytes"] == expected_file["bytes"] == len(data)
    checks["download_sha256"] = proof["sha256"] == expected_file["sha256"] == sha(data)
    checks["download_limit"] = 0 < len(data) <= 1048576
    checks["download_manifest_exact"] = set(downloads) == {
        str(p.relative_to(case / "work")) for p in (case / "work").rglob("*") if p.is_file()
    }
    receipts = []
    for call in calls:
        if call.get("function", {}).get("name") != "terminal":
            continue
        content = as_dict(by_id.get(call.get("id"), {}).get("content"))
        if content.get("exit_code") != 0:
            continue
        for line in content.get("output", "").splitlines():
            receipt = as_dict(line)
            if "local_path" in receipt:
                receipts.append(receipt)
    checks["agent_download_receipt"] = any(
        isinstance(receipt["local_path"], str)
        and Path(receipt["local_path"]).resolve() == path.resolve()
        and receipt.get("bytes") == expected_file["bytes"]
        and receipt.get("sha256") == expected_file["sha256"]
        and receipt.get("http_status") == 200
        for receipt in receipts
    )
    reader = "read_file" if intent["task"] == "pdf" else "vision_analyze"

    def reader_target(call: dict) -> Path | None:
        function = call.get("function", {})
        argument = "path" if function.get("name") == "read_file" else "image_url"
        value = as_dict(function.get("arguments")).get(argument)
        return Path(value).resolve() if isinstance(value, str) and value else None

    reads = [
        c
        for c in calls
        if c.get("function", {}).get("name") == reader and reader_target(c) == path.resolve()
    ]
    checks["reader_targets_allowed"] = all(
        reader_target(c)
        in (
            {path.resolve(), (case / "http-reference.json").resolve()}
            if c.get("function", {}).get("name") == "read_file"
            else {path.resolve()}
        )
        for c in calls
        if c.get("function", {}).get("name") in {"read_file", "vision_analyze"}
    )
    read = reads[0] if len(reads) == 1 else {}
    checks["reads_downloaded_file"] = len(reads) == 1
    reply = by_id.get(read.get("id"), {})
    result = as_dict(reply.get("content"))
    if reader == "read_file":
        checks["pdf_native_extraction"] = (
            result.get("extracted_document") is True and result.get("truncated") is False
        )
        checks["pdf_content_present"] = bool(result.get("content")) and "\ufffd" not in result.get(
            "content", ""
        )
    else:
        projected = text_content(reply.get("content"))
        markers = (
            "Image loaded into your context",
            "Image attached natively for the main model",
        )
        checks["native_tool_projection"] = result.get("meta", {}).get(
            "native_vision"
        ) is True or any(marker in projected for marker in markers)
        visibility["native_vision_metadata"] = result.get("meta", {}).get("native_vision") is True
        visibility["persisted_screenshot_placeholder"] = "[screenshot]" in projected
        images = image_parts(result or reply.get("content"))
        visibility["session_image_parts"] = "present" if images else "absent_text_projection"
        if images:
            checks["session_image_digest"] = any(
                image == {"bytes": expected_file["bytes"], "sha256": expected_file["sha256"]}
                for image in images
            )
        # Even actual session image parts are not an independent provider-wire capture.
    return checks, visibility


def score_answers(expected: dict, pdf: str, image: str) -> dict[str, bool]:
    path = Path(__file__).parents[1] / "native_agent_acceptance" / "check.py"
    spec = importlib.util.spec_from_file_location("native_media_answer_scorer", path)
    assert spec is not None and spec.loader is not None
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    return scorer.score_answers(expected, pdf, image)


def evaluate(
    root: Path, verifier_path: Path, verifier_sha256: str, task: str | None = None
) -> dict:
    # Do not move answer-key access before this full freeze verification.
    if task not in (None, "pdf", "image"):
        raise ValueError("Unknown case")
    cases = {name: load_frozen(root / name) for name in ((task,) if task else ("pdf", "image"))}
    raw = verifier_path.read_bytes()
    if sha(raw) != verifier_sha256:
        raise ValueError("Original verifier digest differs")
    verifier = json.loads(raw)
    checks = score_answers(
        verifier["verifier_only_expected"],
        cases.get("pdf", {}).get("final", ""),
        cases.get("image", {}).get("final", ""),
    )
    checks = {key: value for key, value in checks.items() if key.split(".")[0] in cases}
    visibility = {}
    for name, evidence in cases.items():
        expected_files = [
            f
            for f in verifier["files"]
            if verifier_file_path(f) == evidence["intent"]["remote_path"]
        ]
        if len(expected_files) != 1:
            raise ValueError("No unique approved sample for this case")
        execution, visibility[name] = score_case(evidence, expected_files[0])
        checks.update({f"{name}.execution.{key}": value for key, value in execution.items()})
    return {
        "verifier_sha256": sha(raw),
        "answers_sha256": {name: sha(case["final"].encode()) for name, case in cases.items()},
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "failed": [name for name, passed in checks.items() if not passed],
        "visibility": visibility,
        "rules_versions": {
            name: case["intent"].get("rules_version", "original-strict-v1")
            for name, case in cases.items()
        },
        "scope": "Windows to installed Hermes HTTP; CFserver and WeChat unverified",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--verifier-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--task", choices=("pdf", "image"))
    args = parser.parse_args()
    report = evaluate(args.root, args.verifier, args.verifier_sha256, args.task)
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("passed", "total", "failed", "visibility")}))
    raise SystemExit(1 if report["failed"] else 0)


if __name__ == "__main__":
    main()
