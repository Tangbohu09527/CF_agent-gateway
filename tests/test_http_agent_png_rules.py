"""Synthetic PNG v2 rules; never read installed Skills, credentials, or samples."""

from __future__ import annotations

import copy
import importlib.util
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def modules():
    root = Path(__file__).parent
    checker = load_module(root / "http_agent_acceptance/check.py", "png_v2_checker")
    driver = load_module(root / "http_agent_acceptance/run.py", "png_v2_driver")
    fixtures = load_module(root / "test_http_agent_acceptance_check.py", "png_v2_fixtures")
    return checker, driver, fixtures


@pytest.fixture
def pinned(modules, tmp_path, monkeypatch):
    checker, driver, _ = modules
    content = "# Synthetic read-only description\nDo not execute scripts.\n"
    home = tmp_path / "synthetic-hermes"
    source = home / "hermes-agent"
    source.mkdir(parents=True)
    config = home / "config.yaml"
    config.write_text("skills: {}\n", encoding="utf-8")
    loader = source / "synthetic-loader.py"
    # If a test accidentally imports this source, it must fail immediately.
    loader.write_bytes(b"raise AssertionError('Synthetic loader must never execute')\n")
    skill_path = "skills/productivity/ocr-and-documents/SKILL.md"
    skill = home / skill_path
    skill.parent.mkdir(parents=True)
    skill.write_bytes(content.encode())
    rules = {
        "version": "hermes-http-png-v2",
        "skill": {
            "name": "ocr-and-documents",
            "path": skill_path,
            "sha256": checker.sha(skill.read_bytes()),
            "content_sha256": checker.sha(content.encode()),
            "source": "https://official.invalid/pinned/synthetic-SKILL.md",
        },
        "loader_sha256": {loader.name: checker.sha(loader.read_bytes())},
    }
    path = tmp_path / "trusted-synthetic-rules.json"
    write_json(path, rules)
    monkeypatch.setattr(checker, "PNG_RULES", path)
    monkeypatch.setattr(driver, "PNG_RULES", path)
    args = SimpleNamespace(
        task="image", hermes_source=source, skill_file=skill, hermes_config=config
    )
    raw, proof = driver.verify_png_skill(args)
    return SimpleNamespace(
        rules=rules,
        path=path,
        raw=raw,
        proof=proof,
        content=content,
        args=args,
        loader=loader,
        skill=skill,
        config=config,
    )


@pytest.fixture
def description(modules, pinned, tmp_path):
    checker, _, _ = modules
    case = tmp_path / "rules-case"
    case.mkdir()
    (case / "rules.json").write_bytes(pinned.raw)
    write_json(case / "skill-preflight.json", pinned.proof)
    write_json(case / "skill-postflight.json", pinned.proof)
    intent = {
        "task": "image",
        "rules_version": "hermes-http-png-v2",
        "rules_sha256": checker.sha(pinned.raw),
    }
    call = {
        "id": "synthetic-skill",
        "function": {"name": "skill_view", "arguments": '{"name":"ocr-and-documents"}'},
    }
    result = {
        "success": True,
        "name": "ocr-and-documents",
        "content": pinned.content,
        "_source_path": pinned.proof["skill_file"],
        "readiness_status": "available",
        "setup_needed": False,
        "setup_skipped": False,
        "required_environment_variables": [],
        "required_commands": [],
        "missing_required_environment_variables": [],
        "missing_credential_files": [],
        "missing_required_commands": [],
    }
    return SimpleNamespace(case=case, intent=intent, call=call, result=result)


def allowed(checker, item, calls=None):
    return checker.allowed_tools(
        item.case,
        item.intent,
        calls or [item.call],
        {item.call["id"]: {"content": json.dumps(item.result)}},
    )


def test_exact_pinned_description_is_allowed(modules, description):
    value, checks = allowed(modules[0], description)
    assert value and all(checks.values())


def test_missing_version_retains_original_pdf_strict_contract(modules, description):
    checker = modules[0]
    description.intent = {"task": "pdf"}
    assert allowed(checker, description) == (False, {})
    description.call["function"]["name"] = "terminal"
    assert allowed(checker, description) == (True, {})


@pytest.mark.parametrize("version", [None, "", "hermes-http-png-v3", 2, []])
def test_explicit_unknown_or_malformed_version_fails_closed(modules, description, version):
    description.intent["rules_version"] = version
    description.call["function"]["name"] = "terminal"
    assert not allowed(modules[0], description)[0]


def test_v2_cannot_reclassify_pdf(modules, description):
    description.intent["task"] = "pdf"
    assert not allowed(modules[0], description)[0]


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "another-skill"},
        {"name": "OCR-and-documents"},
        {"name": "../ocr-and-documents"},
        {"name": "plugin:ocr-and-documents"},
        {"name": "ocr-and-documents", "file_path": "SKILL.md"},
        {"name": "ocr-and-documents", "file_path": "scripts/setup.py"},
        {"name": "ocr-and-documents", "preprocess": False},
        {"name": "ocr-and-documents", "preprocess": True},
        {"name": "ocr-and-documents", "setup": False},
        {"name": "ocr-and-documents", "args": ""},
        {"name": "ocr-and-documents", "file_path": None},
        {},
        [],
        None,
    ],
)
def test_skill_view_is_not_a_blanket_tool_allowlist(modules, description, arguments):
    description.call["function"]["arguments"] = json.dumps(arguments)
    assert not allowed(modules[0], description)[0]


def test_duplicate_approved_description_is_rejected(modules, description):
    second = copy.deepcopy(description.call)
    second["id"] = "second-skill"
    assert not allowed(modules[0], description, [description.call, second])[0]


@pytest.mark.parametrize(
    "key,value",
    [
        ("success", False),
        ("name", "another-skill"),
        ("content", "Changed description"),
        ("_source_path", "different/SKILL.md"),
        ("_source_path", None),
        ("readiness_status", "needs_setup"),
        ("setup_needed", True),
        ("setup_skipped", True),
        ("required_environment_variables", ["SYNTHETIC_ENV"]),
        ("required_commands", ["synthetic-command"]),
        ("missing_required_environment_variables", ["SYNTHETIC_ENV"]),
        ("missing_credential_files", ["synthetic-reference"]),
        ("missing_required_commands", ["synthetic-command"]),
        ("deps_note", "installed something"),
        ("setup_note", "setup was attempted"),
        ("setup_help", "run a script"),
        ("gateway_setup_hint", "configure something"),
        ("error", "failed"),
        ("file", "SKILL.md"),
    ],
)
def test_description_result_must_prove_identity_and_no_setup(modules, description, key, value):
    description.result[key] = value
    assert not allowed(modules[0], description)[0]


@pytest.mark.parametrize(
    "mode", ["intent_hash", "rule_source", "skill_hash", "loader_hash", "postflight", "missing"]
)
def test_rule_and_source_proofs_must_agree(modules, description, pinned, mode):
    if mode == "intent_hash":
        description.intent["rules_sha256"] = "0" * 64
    elif mode == "rule_source":
        changed = copy.deepcopy(pinned.rules)
        changed["skill"]["source"] = "https://unapproved.invalid/other"
        write_json(description.case / "rules.json", changed)
    elif mode in ("skill_hash", "loader_hash", "postflight"):
        proof = copy.deepcopy(pinned.proof)
        field = "skill" if mode == "skill_hash" else pinned.loader.name
        proof["sha256"][field] = "0" * 64
        target = "skill-postflight.json" if mode == "postflight" else "skill-preflight.json"
        write_json(description.case / target, proof)
    else:
        (description.case / "skill-preflight.json").unlink()
    assert not allowed(modules[0], description)[0]


@pytest.mark.parametrize("field", ["source_root", "config_sha256", "resolver"])
def test_matching_proofs_cannot_omit_resolver_evidence(modules, description, pinned, field):
    proof = copy.deepcopy(pinned.proof)
    del proof[field]
    for name in ("skill-preflight.json", "skill-postflight.json"):
        write_json(description.case / name, proof)
    assert not allowed(modules[0], description)[0]


def test_preflight_hashes_sources_without_loading_or_network(modules, pinned, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Static preflight must not execute network requests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert modules[1].verify_png_skill(pinned.args) == (pinned.raw, pinned.proof)


@pytest.mark.parametrize(
    "mode", ["pdf", "skill", "loader", "source_link", "config_link", "external", "trusted", "path"]
)
def test_changed_preflight_refused_before_http(modules, pinned, monkeypatch, mode):
    if mode == "pdf":
        pinned.args.task = "pdf"
    elif mode == "source_link":
        monkeypatch.setattr(Path, "is_junction", lambda p: p == pinned.args.hermes_source)
    elif mode == "config_link":
        monkeypatch.setattr(Path, "is_junction", lambda p: p == pinned.config)
    elif mode in ("external", "trusted"):
        key = "external_dirs" if mode == "external" else "trusted_project_dirs"
        pinned.config.write_text("skills:\n  " + key + ": [unapproved-synthetic-path]\n")
    elif mode == "path":
        pinned.args.skill_file = pinned.skill.with_name("other-SKILL.md")
    else:
        getattr(pinned, mode).write_bytes(b"changed synthetic source")
    with pytest.raises(ValueError):
        modules[1].verify_png_skill(pinned.args)


def test_png_prompt_does_not_change_original_pdf_prompt(modules, tmp_path):
    driver = modules[1]
    args = ("pdf", "/synthetic.pdf", tmp_path, tmp_path / "reference", tmp_path / "python")
    original = driver.prompt(*args)
    assert "不加载 Skill" in original and "skill_view" not in original
    image = driver.prompt("image", *args[1:], png_v2=True)
    assert 'skill_view({"name":"ocr-and-documents"}) 一次' in image
    assert "不执行或修改 Skill" in image


@pytest.mark.parametrize("valid", [True, False])
def test_driver_wires_rule_preflight_before_any_request(
    modules, pinned, tmp_path, monkeypatch, valid
):
    driver = modules[1]
    fixtures = load_module(
        Path(__file__).with_name("test_http_agent_acceptance.py"), "png_v2_driver_fixtures"
    )
    args = fixtures.args.__wrapped__(tmp_path, monkeypatch)
    args.task = "image"
    args.png_rules_v2 = True
    args.hermes_source = pinned.args.hermes_source
    args.hermes_config = pinned.args.hermes_config
    args.skill_file = pinned.args.skill_file
    requests = fixtures.install_http(monkeypatch, args)
    if not valid:
        pinned.loader.write_bytes(b"changed synthetic loader")
        with pytest.raises(ValueError, match="Pinned Skill/source bytes changed"):
            driver.run(args)
        assert requests == []
        assert not (args.output / "image").exists()
        return
    driver.run(args)
    assert [request.method for request in requests] == ["GET", "POST", "GET", "GET"]
    intent = fixtures.read_case(args, "intent.json")
    assert intent["rules_version"] == "hermes-http-png-v2"
    assert intent["rules_sha256"] == pinned.proof["rules_sha256"]
    assert fixtures.read_case(args, "skill-preflight.json") == pinned.proof
    assert fixtures.read_case(args, "skill-postflight.json") == pinned.proof
    assert "rules.json" in fixtures.read_case(args, "frozen.json")
    assert "ocr-and-documents" in json.loads(requests[1].content)["messages"][0]["content"]


def prepare_image(modules, description, tmp_path):
    checker, _, fixtures = modules
    image, expected_file = fixtures.case.__wrapped__(checker, tmp_path)("image")
    root = image["case"]
    image["intent"].update(description.intent)
    write_json(root / "intent.json", image["intent"])
    for name in ("rules.json", "skill-preflight.json", "skill-postflight.json"):
        (root / name).write_bytes((description.case / name).read_bytes())
    fixtures.freeze(checker, root)
    verifier = tmp_path / "synthetic-verifier.json"
    write_json(
        verifier,
        {
            "files": [expected_file],
            "verifier_only_expected": {
                "pdf": {"required_strings": ["Do not score this historical PDF"], "table_rows": []},
                "image": {"visible_code": "Synthetic final answer.", "regions": {}, "arrows": []},
            },
        },
    )
    return image, verifier


@pytest.mark.parametrize("pdf_exists", [False, True])
def test_image_only_evaluation_never_rescores_or_opens_pdf(
    modules, description, tmp_path, monkeypatch, pdf_exists
):
    checker = modules[0]
    _, verifier = prepare_image(modules, description, tmp_path)
    if pdf_exists:
        old_pdf = tmp_path / "pdf"
        old_pdf.mkdir()
        (old_pdf / "frozen.json").write_text("intentionally invalid historical fixture")
    read_bytes = Path.read_bytes
    read_text = Path.read_text

    def guarded_read_bytes(path, *args, **kwargs):
        assert not path.is_relative_to(tmp_path / "pdf")
        return read_bytes(path, *args, **kwargs)

    def guarded_read_text(path, *args, **kwargs):
        assert not path.is_relative_to(tmp_path / "pdf")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    report = checker.evaluate(tmp_path, verifier, checker.sha(verifier.read_bytes()), task="image")
    assert report["passed"] == report["total"]
    assert all(key.startswith("image.") for key in report["checks"])
    assert set(report["answers_sha256"]) == {"image"}
    assert report["rules_versions"] == {"image": "hermes-http-png-v2"}
    assert report["visibility"]["image"]["model_transport_image_bytes"] == "unobserved"


def test_image_freeze_is_verified_before_answer_key(modules, description, tmp_path, monkeypatch):
    checker = modules[0]
    image, verifier = prepare_image(modules, description, tmp_path)
    (image["case"] / "final.txt").write_text("tampered after freeze", encoding="utf-8")
    original = Path.read_bytes

    def guarded(path):
        assert path != verifier, "Must reject altered evidence before reading answer key"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    with pytest.raises(ValueError, match="Frozen evidence bytes"):
        checker.evaluate(tmp_path, verifier, "0" * 64, task="image")
