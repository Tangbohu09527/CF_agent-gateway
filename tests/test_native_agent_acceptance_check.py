"""Adversarial offline scorer fixtures, unrelated to either approved sample."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CHECK_PATH = Path(__file__).parent / "native_agent_acceptance" / "check.py"


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location("native_acceptance_check_offline", CHECK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def answers():
    expected = {
        "pdf": {
            "required_strings": ["SYNTHETIC REPORT", "END-OFFLINE"],
            "table_rows": [["ITEM-A", 12, 3.5], ["ITEM-B", 27, 9.25]],
        },
        "image": {
            "visible_code": "OFFLINE-NOT-A-SAMPLE",
            "regions": {
                "A": {"position": "top-left", "shape": "circle", "color": "red", "count": 2},
                "B": {"position": "top-right", "shape": "square", "color": "blue", "count": 4},
                "C": {
                    "position": "bottom-left",
                    "shape": "triangle",
                    "color": "green",
                    "count": 6,
                },
                "D": {
                    "position": "bottom-right",
                    "shape": "star",
                    "color": "yellow",
                    "count": 8,
                },
            },
            "arrows": [["A", "B"], ["B", "D"], ["D", "C"], ["C", "A"]],
        },
    }
    pdf = "SYNTHETIC REPORT\n| ITEM-A | 12 | 3.5 |\n| ITEM-B | 27 | 9.25 |\nEND-OFFLINE"
    image = """OFFLINE-NOT-A-SAMPLE
| A | top-left | circle | red | 2 |
| B | top-right | square | blue | 4 |
| C | bottom-left | triangle | green | 6 |
| D | bottom-right | star | yellow | 8 |
Arrows: A → B; B → D; D → C; C → A
"""
    return expected, pdf, image


def test_synthetic_complete_answer_passes(checker, answers):
    assert all(checker.score_answers(*answers).values())


@pytest.mark.parametrize(
    "old,new,failed_check",
    [
        ("top-left", "bottom-left", "image.region.A.position"),
        ("circle", "triangle", "image.region.A.shape"),
        ("red", "blue", "image.region.A.color"),
        ("red", "red or blue", "image.region.A.color"),
        ("| 2 |", "| 12 |", "image.region.A.count"),
        ("| 2 |", "| -2 |", "image.region.A.count"),
        ("| 2 |", "| 2.2 |", "image.region.A.count"),
        ("| 2 |", "| 2 or 3 |", "image.region.A.count"),
        ("A → B", "B → A", "image.arrow.0"),
        ("OFFLINE-NOT-A-SAMPLE", "wrong-code", "image.visible_code"),
    ],
)
def test_wrong_attributes_and_reversed_arrow_cannot_pass(checker, answers, old, new, failed_check):
    expected, pdf, image = answers
    checks = checker.score_answers(expected, pdf, image.replace(old, new))
    assert checks[failed_check] is False
    assert not all(checks.values())


def test_unexpected_additional_arrow_is_rejected(checker, answers):
    expected, pdf, image = answers
    checks = checker.score_answers(expected, pdf, image + "\nA → D\n")
    assert all(checks[f"image.arrow.{index}"] for index in range(4))
    assert checks["image.exact_arrow_set"] is False


@pytest.mark.parametrize("wrong", ["112", "120", "12.5", "0.12", "12e3", "12kg", "-12"])
def test_table_numeric_boundary_rejects_different_numbers(checker, answers, wrong):
    expected, pdf, image = answers
    changed = pdf.replace("| 12 |", f"| {wrong} |")
    assert checker.score_answers(expected, changed, image)["pdf.table.0.1"] is False


def test_table_value_from_another_item_does_not_satisfy_row(checker, answers):
    expected, pdf, image = answers
    changed = pdf.replace("| ITEM-A | 12 |", "| ITEM-A | 13 |").replace(
        "| ITEM-B | 27 |", "| ITEM-B | 12 |"
    )
    assert checker.score_answers(expected, changed, image)["pdf.table.0.1"] is False


def test_unknown_answer_key_vocabulary_fails_closed(checker):
    with pytest.raises(ValueError, match="Unknown answer-key vocabulary"):
        checker.attribute_matches("color", "synthetic-ultraviolet", "synthetic-ultraviolet")


@pytest.fixture
def frozen_case(tmp_path, checker):
    case = tmp_path / "synthetic-case"
    case.mkdir()
    material = {
        "result.json": json.dumps({"final_response": "offline answer"}).encode(),
        "events.jsonl": b'{"kind":"synthetic"}\n',
        "started.json": b'{"session_id":"offline-session","task_id":"offline-task"}',
        "work/download.bin": b"public synthetic file only",
    }
    manifest = {}
    for relative, data in material.items():
        destination = case / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        manifest[str(destination.relative_to(case))] = {
            "bytes": len(data),
            "sha256": checker.sha(data),
        }
    (case / "frozen.json").write_text(json.dumps(manifest), encoding="utf-8")
    return case


def test_original_frozen_synthetic_evidence_loads(checker, frozen_case):
    result, events, started = checker.load_frozen(frozen_case)
    assert result["final_response"] == "offline answer"
    assert events == [{"kind": "synthetic"}]
    assert started["session_id"] == "offline-session"


def test_same_length_content_tampering_is_rejected(checker, frozen_case):
    destination = frozen_case / "result.json"
    original = destination.read_bytes()
    changed = original.replace(b"offline answer", b"altered answer")
    assert len(changed) == len(original) and changed != original
    destination.write_bytes(changed)
    with pytest.raises(ValueError, match="Frozen evidence bytes changed"):
        checker.load_frozen(frozen_case)


@pytest.mark.parametrize("change", ["added", "removed", "renamed", "nested_manifest"])
def test_frozen_file_set_tampering_is_rejected(checker, frozen_case, change):
    destination = frozen_case / "work/download.bin"
    if change == "added":
        (frozen_case / "extra.json").write_text("{}", encoding="utf-8")
    elif change == "removed":
        destination.unlink()
    elif change == "renamed":
        destination.rename(destination.with_name("renamed.bin"))
    else:
        (frozen_case / "work/frozen.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen evidence file set changed"):
        checker.load_frozen(frozen_case)
