"""Offline scorer: verify frozen Agent evidence BEFORE opening the answer key.

This never imports Hermes, downloads files, or asks a model to judge itself.
Reports contain check names and booleans, not sample content or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_frozen(case: Path) -> tuple[dict, list[dict], dict]:
    frozen = json.loads((case / "frozen.json").read_text(encoding="utf-8"))
    actual_names = {
        str(p.relative_to(case))
        for p in case.rglob("*")
        if p.is_file() and p != case / "frozen.json"
    }
    if set(frozen) != actual_names:
        raise ValueError("Frozen evidence file set changed")
    for relative, proof in frozen.items():
        path = case / relative
        if not path.resolve().is_relative_to(case.resolve()) or path.is_symlink():
            raise ValueError("Frozen evidence escapes its private case")
        data = path.read_bytes()
        if len(data) != proof["bytes"] or sha(data) != proof["sha256"]:
            raise ValueError("Frozen evidence bytes changed")
    return (
        json.loads((case / "result.json").read_text(encoding="utf-8")),
        [
            json.loads(line)
            for line in (case / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ],
        json.loads((case / "started.json").read_text(encoding="utf-8")),
    )


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("**", "").replace("`", "")).strip()


def cell_present(value, line: str) -> bool:
    return re.search(r"(?<![\w.+-])" + re.escape(str(value)) + r"(?![\w.])", line) is not None


ALIASES = {
    "position": [
        ["top-left", "upper-left", "left-top", "左上", "左上角"],
        ["top-right", "upper-right", "right-top", "右上", "右上角"],
        ["bottom-left", "lower-left", "left-bottom", "左下", "左下角"],
        ["bottom-right", "lower-right", "right-bottom", "右下", "右下角"],
    ],
    "shape": [
        ["circle", "circles", "圆形", "圆"],
        ["square", "squares", "正方形", "方形"],
        ["triangle", "triangles", "三角形"],
        ["star", "stars", "five-point star", "five-pointed star", "五角星", "星形"],
    ],
    "color": [
        ["red", "红色", "红"],
        ["blue", "蓝色", "蓝"],
        ["green", "绿色", "绿"],
        ["yellow", "黄色", "黄"],
    ],
}


def compact(text: str) -> str:
    return re.sub(r"[\s_\-]", "", text.lower())


def attribute_matches(category: str, expected: str, text: str) -> bool:
    groups = ALIASES[category]
    expected_groups = {
        i for i, group in enumerate(groups) if compact(expected) in map(compact, group)
    }
    if len(expected_groups) != 1:
        raise ValueError("Unknown answer-key vocabulary; do not silently accept")
    observed = {
        i for i, group in enumerate(groups) if any(compact(a) in compact(text) for a in group)
    }
    return observed == expected_groups


def region_text(answer: str, label: str) -> str:
    header = r"^\s*(?:#{1,6}\s*)?(?:\|\s*|[-*]\s*)?(?:区域\s*)?\*{0,2}"
    match = re.search(header + re.escape(label) + r"(?![A-Za-z0-9])[^\n]*", answer, re.M)
    if not match:
        return ""
    if "|" in match.group():
        return match.group()
    end = re.search(
        header + r"[A-D](?![A-Za-z0-9])|^.*(?:箭头|Arrows)", answer[match.end() :], re.M | re.I
    )
    return answer[match.start() : match.end() + end.start()] if end else answer[match.start() :]


def count_matches(expected: int, text: str) -> bool:
    observed = set(re.findall(r"(?<![\d.])[+-]?\d+(?:\.\d+)?(?![\d.])", text))
    digits = dict(zip("一二三四五六七八九十", range(1, 11), strict=True))
    observed.update(
        str(digits[v]) for v in re.findall(r"([一二三四五六七八九十])\s*[个枚颗只]", text)
    )
    return observed == {str(expected)}


def directed_arrows(answer: str) -> set[tuple[str, str]]:
    text = answer.replace("**", "").replace("`", "")
    pattern = r"(?=([A-D])\s*(?:[（(][^()（）\n]*[)）])?\s*(?:→|->|➡|⟶|指向|到)\s*([A-D]))"
    return {(m[1], m[2]) for m in re.finditer(pattern, text)}


def score_answers(expected: dict, pdf: str, image: str) -> dict[str, bool]:
    checks = {}
    for i, value in enumerate(expected["pdf"]["required_strings"]):
        checks[f"pdf.required_string.{i}"] = normalized(value) in normalized(pdf)
    for i, row in enumerate(expected["pdf"]["table_rows"]):
        lines = [line for line in pdf.splitlines() if cell_present(row[0], line)]
        for j, value in enumerate(row):
            checks[f"pdf.table.{i}.{j}"] = any(cell_present(value, line) for line in lines)
    checks["image.visible_code"] = expected["image"]["visible_code"] in image
    for label, region in expected["image"]["regions"].items():
        block = region_text(image, label)
        for key in ("position", "shape", "color"):
            checks[f"image.region.{label}.{key}"] = attribute_matches(key, region[key], block)
        checks[f"image.region.{label}.count"] = count_matches(region["count"], block)
    arrows = directed_arrows(image)
    for i, pair in enumerate(expected["image"]["arrows"]):
        checks[f"image.arrow.{i}"] = tuple(pair) in arrows
    checks["image.exact_arrow_set"] = arrows == set(map(tuple, expected["image"]["arrows"]))
    return checks


def decode_result(value):
    return json.loads(value) if isinstance(value, str) else value


def score_execution(
    case: Path, result: dict, events: list[dict], started: dict, file: dict
) -> dict:
    checks = {}
    checks["completed"] = result.get("completed") is True and all(
        result.get(key) is False for key in ("failed", "partial", "interrupted")
    )
    checks["session_matches"] = result.get("session_id") == started["session_id"]
    users = [m for m in result["messages"] if m["role"] == "user"]
    checks["fresh_task_only"] = len(users) == 1 and users[0]["content"] == (
        case / "prompt.txt"
    ).read_text(encoding="utf-8")
    calls = {c["id"]: c for m in result["messages"] for c in m.get("tool_calls", [])}
    starts = {e["call_id"]: e for e in events if e["kind"] == "tool_start"}
    ends = {e["call_id"]: e for e in events if e["kind"] == "tool_complete"}
    replies = {m["tool_call_id"]: m for m in result["messages"] if m["role"] == "tool"}
    checks["all_calls_paired"] = bool(calls) and set(calls) == set(starts) == set(ends) == set(
        replies
    )
    checks["actual_tool_names"] = checks["all_calls_paired"] and all(
        c["function"]["name"] == starts[cid]["name"] == ends[cid]["name"]
        for cid, c in calls.items()
    )
    checks["actual_tool_arguments"] = checks["all_calls_paired"] and all(
        json.loads(c["function"]["arguments"]) == starts[cid]["args"] == ends[cid]["args"]
        for cid, c in calls.items()
    )
    runtime = next(e for e in events if e["kind"] == "runtime")
    requests = [e for e in events if e["kind"] == "model_request" and e.get("model")]
    checks["approved_model_only"] = bool(requests) and all(
        e["model"] == runtime["model"] for e in requests
    )
    checks["plugins_disabled"] = runtime["plugin_count"] == 0
    checks["no_temporary_aux_mapping"] = runtime["auxiliary_vision"] is None
    downloads = json.loads((case / "downloads.json").read_text(encoding="utf-8"))
    checks["one_download"] = len(downloads) == 1
    if len(downloads) != 1:
        return checks
    relative, proof = next(iter(downloads.items()))
    path = case / "work" / relative
    checks["download_bytes"] = proof["bytes"] == file["bytes"] == path.stat().st_size
    checks["download_sha256"] = proof["sha256"] == file["sha256"] == sha(path.read_bytes())
    checks["private_destination"] = (
        path.resolve().is_relative_to((case / "work").resolve()) and not path.is_symlink()
    )
    checks["download_limit"] = path.stat().st_size <= 1048576
    terminal = [e for e in ends.values() if e["name"] == "terminal"]
    receipts = []
    for e in terminal:
        output = decode_result(e["result"])
        for line in output.get("output", "").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "local_path" in row:
                receipts.append(row)
    checks["agent_download_receipt"] = any(
        Path(r["local_path"]).resolve() == path.resolve()
        and r.get("sha256") == file["sha256"]
        and r.get("bytes") == file["bytes"]
        and r.get("http_status", r.get("download_http_status")) == 200
        for r in receipts
    )
    reader = "read_file" if file["media_type"] == "application/pdf" else "vision_analyze"
    reads = [e for e in ends.values() if e["name"] == reader]
    checks["read_downloaded_file"] = len(reads) == 1 and any(
        isinstance(v, str) and Path(v).resolve() == path.resolve()
        for v in reads[0]["args"].values()
    )
    if reader == "read_file" and reads:
        data = decode_result(reads[0]["result"])
        checks["pdf_native_extraction"] = (
            data.get("extracted_document") is True and data.get("truncated") is False
        )
        checks["pdf_not_empty"] = bool(data.get("content")) and "\ufffd" not in data.get(
            "content", ""
        )
    elif reads:
        data = decode_result(reads[0]["result"])
        checks["native_vision_path"] = data.get("meta", {}).get("native_vision") is True
        checks["real_image_request_after_tool"] = any(
            e["time"] > reads[0]["time"]
            and any(
                i["sha256"] == file["sha256"] and i["bytes"] == file["bytes"]
                for i in e.get("images", [])
            )
            for e in requests
        )
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--verifier-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    # Deliberate ordering: neither expected contents nor even its hash is opened
    # until every answer and its transcript has been frozen and verified.
    cases = {name: load_frozen(args.root / name) for name in ("pdf", "image")}
    raw = args.verifier.read_bytes()
    if sha(raw) != args.verifier_sha256:
        raise ValueError("Original verifier digest differs")
    verifier = json.loads(raw)
    answers = {name: case[0]["final_response"] for name, case in cases.items()}
    checks = score_answers(verifier["verifier_only_expected"], answers["pdf"], answers["image"])
    for name, (result, events, started) in cases.items():
        media = "application/pdf" if name == "pdf" else "image/png"
        file = next(f for f in verifier["files"] if f["media_type"] == media)
        checks.update(
            {
                name + ".execution." + k: v
                for k, v in score_execution(args.root / name, result, events, started, file).items()
            }
        )
    report = {
        "verifier_sha256": sha(raw),
        "answers_sha256": {k: sha(v.encode()) for k, v in answers.items()},
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "failed": [k for k, passed in checks.items() if not passed],
    }
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ("passed", "total", "failed")}))
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
