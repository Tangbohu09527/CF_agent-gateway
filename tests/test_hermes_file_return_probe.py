"""No model/service is launched by these probe contract regressions."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).parent / "hermes_file_return_probe/model.py"
spec = importlib.util.spec_from_file_location("file_return_model_contract", MODULE)
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


def definition(name):
    if name == model.READ:
        required = ["attachment_id"]
        props = {
            "attachment_id": {"type": "integer", "minimum": 1},
            "prepare_return": {"type": "boolean"},
        }
    else:
        required = ["file_ref", "kind"]
        props = {
            "file_ref": {"type": "string"},
            "kind": {"enum": ["file", "image"]},
            "filename": {"type": "string"},
        }
    return {
        "name": name,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": props,
        },
    }


def request(*results, direct=True, label="pdf"):
    names = (model.READ, model.RETURN) if direct else ("tool_describe", "tool_call")
    definitions = [
        definition(name)
        if name in {model.READ, model.RETURN}
        else {
            "name": name,
            "parameters": {
                "properties": {"names" if name == "tool_describe" else "calls": {"type": "array"}}
            },
        }
        for name in names
    ]
    return {
        "tools": [{"type": "function", "function": value} for value in definitions],
        "messages": [{"role": "user", "content": f'file-return-case:{label} {{"attachment_id":5}}'}]
        + [{"role": "tool", "content": json.dumps(item)} for item in results],
    }


def called(value):
    function = value["tool_calls"][0]["function"]
    return function["name"], json.loads(function["arguments"])


def descriptor():
    return {
        "file_ref": "inbound-" + "a" * 32 + ".pdf",
        "kind": "file",
        "filename": "original.pdf",
        "size_bytes": 42,
        "sha256": "b" * 64,
    }


@pytest.mark.parametrize("name", [model.READ, model.RETURN])
def test_real_schema_required_not_registered_name(name):
    assert len(model.verify_schema(name, definition(name))) == 64
    missing = definition(name)
    missing.pop("parameters")
    with pytest.raises(AssertionError):
        model.verify_schema(name, missing)


@pytest.mark.parametrize("bad", ["string", "integer", None])
def test_prepare_return_schema_must_be_actual_boolean(bad):
    value = definition(model.READ)
    value["parameters"]["properties"]["prepare_return"]["type"] = bad
    with pytest.raises(AssertionError):
        model.verify_schema(model.READ, value)


def test_deferred_tools_described_before_any_known_argument_is_used():
    planner = model.Planner()
    name, arguments = called(planner.decide(request(direct=False)))
    assert name == "tool_describe" and arguments == {"names": [model.READ, model.RETURN]}
    descriptions = {"tools": {name: definition(name) for name in (model.READ, model.RETURN)}}
    name, arguments = called(planner.decide(request(descriptions, direct=False)))
    assert name == "tool_call"
    assert arguments["calls"] == [
        {"name": model.READ, "arguments": {"attachment_id": 100005, "prepare_return": True}}
    ]
    assert set(planner.schema_hashes) == {model.READ, model.RETURN}


def test_missing_actual_describe_bridge_is_rejected():
    body = request(direct=False)
    body["tools"] = [item for item in body["tools"] if item["function"]["name"] != "tool_describe"]
    with pytest.raises(AssertionError, match="describe"):
        model.Planner().decide(body)


def test_return_uses_only_tool_descriptor_and_preserves_repeated_identity():
    planner = model.Planner()
    failure = {"ok": False, "error": {"code": "attachment_unavailable"}}
    read = {"ok": True, "return_preparation": {"ok": True, **descriptor()}}
    assert called(planner.decide(request(failure)))[1] == {
        "attachment_id": 5,
        "prepare_return": True,
    }
    assert called(planner.decide(request(failure, read)))[0] == model.READ
    name, args = called(planner.decide(request(failure, read, read)))
    assert name == model.RETURN
    assert args == {key: descriptor()[key] for key in ("file_ref", "kind", "filename")}
    assert planner.repeated_descriptors_equal
    receipt = {"success": True, "status": "pending_delivery", "artifact_id": "synthetic"}
    assert "tool_calls" not in planner.decide(request(failure, read, read, receipt))


@pytest.mark.parametrize(
    "reference", ["inbound:" + "a" * 48, "/tmp/guessed.pdf", "\\root\\guessed.pdf"]
)
def test_internal_handle_or_absolute_guess_cannot_be_returned(reference):
    value = descriptor()
    value["file_ref"] = reference
    with pytest.raises(AssertionError):
        model.Planner().decide(request({"ok": True, "return_preparation": value}))


def test_repeat_descriptor_change_fails_instead_of_sending_new_path():
    planner = model.Planner()
    first = {"ok": True, "return_preparation": descriptor()}
    planner.decide(request(first))
    second = copy.deepcopy(first)
    second["return_preparation"]["sha256"] = "c" * 64
    with pytest.raises(AssertionError, match="repeat changed"):
        planner.decide(request(first, second))


def test_replay_failure_must_be_actual_return_tool_result():
    planner = model.Planner()
    planner.prepared[5] = descriptor()
    assert called(planner.decide(request(label="replay")))[0] == model.RETURN
    assert "tool_calls" not in planner.decide(
        request({"success": False, "error": "return_file_unavailable"}, label="replay")
    )


def test_multimodal_and_deferred_descriptors_are_read_from_http_content():
    nested = {
        "results": [
            {
                "result": json.dumps(
                    {
                        "_multimodal": True,
                        "text_summary": json.dumps({"return_preparation": descriptor()}),
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                            }
                        ],
                    }
                )
            }
        ]
    }
    assert descriptor() in list(model.objects(nested))


def test_followup_is_ordinary_text_without_another_tool_or_descriptor():
    planner = model.Planner()
    body = request(label="followup")
    body["tools"] = []
    answer = planner.decide(body)
    assert isinstance(answer["content"], str) and answer["content"]
    assert "tool_calls" not in answer
    assert planner.prepared == {}


def test_audit_denies_source_parent_manifest_without_reading_it(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "file_return_audit_contract", MODULE.with_name("probe.py")
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    callbacks = []
    monkeypatch.setattr(probe.sys, "addaudithook", callbacks.append)

    class IO:
        @staticmethod
        def audit_path_within(path, roots, **_):
            return any(Path(path).resolve().is_relative_to(root) for root in roots)

    class Support:
        @staticmethod
        def _local_module(*_):
            return IO

    case, source = tmp_path / "case", tmp_path / "source"
    violations, metadata = probe.install_host_audit(case, (source,), tmp_path / "worker", Support)
    with pytest.raises(FileNotFoundError):
        callbacks[0]("open", (str(source.parent / "manifest.json"), "r", 0))
    assert metadata == ["source_parent_manifest"] and violations == []
    with pytest.raises(PermissionError):
        callbacks[0]("open", (str(source / ".env"), "r", 0))
    assert len(violations) == 1


@pytest.fixture
def guarded_events(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "file_return_spawn_contract", MODULE.with_name("probe.py")
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    callbacks = []
    monkeypatch.setattr(probe.sys, "addaudithook", callbacks.append)

    class IO:
        @staticmethod
        def audit_path_within(path, roots, **_):
            return any(Path(path).resolve().is_relative_to(root) for root in roots)

    class Support:
        @staticmethod
        def _local_module(*_):
            return IO

        @staticmethod
        def content_parser_launch_allowed(case, executable, command, cwd, env):
            return command == ["fixed-parser", "--fixed"] and executable == "fixed-parser"

    failures, _ = probe.install_host_audit(
        tmp_path / "case", (tmp_path / "source",), tmp_path / "worker", Support
    )
    return probe, callbacks[0], failures


def test_posix_spawn_requires_matching_vetted_popen_once(guarded_events):
    _, guard, failures = guarded_events
    command, env = ["fixed-parser", "--fixed"], {"PATH": "test-only"}
    with pytest.raises(PermissionError):
        guard("os.posix_spawn", ("fixed-parser", command, env))
    guard("subprocess.Popen", ("fixed-parser", command, None, env))
    guard("os.posix_spawn", ("fixed-parser", command, env.copy()))
    with pytest.raises(PermissionError):
        guard("os.posix_spawn", ("fixed-parser", command, env))
    assert len(failures) == 2


@pytest.mark.parametrize("change", ["executable", "arguments", "environment"])
def test_posix_spawn_rejects_modified_popen_ticket(guarded_events, change):
    _, guard, _ = guarded_events
    command, env = ["fixed-parser", "--fixed"], {"PATH": "test-only"}
    guard("subprocess.Popen", ("fixed-parser", command, None, env))
    with pytest.raises(PermissionError):
        guard(
            "os.posix_spawn",
            (
                "other" if change == "executable" else "fixed-parser",
                command + ["extra"] if change == "arguments" else command,
                {"PATH": "other"} if change == "environment" else env,
            ),
        )
    with pytest.raises(PermissionError):
        guard("os.posix_spawn", ("fixed-parser", command, env))


def test_proc_metadata_does_not_walk_ancestor_processes(guarded_events):
    probe, guard, _ = guarded_events
    with pytest.raises(PermissionError):
        guard("open", (f"/proc/{probe.os.getppid()}/stat", "r", 0))
    with pytest.raises(PermissionError):
        guard("open", ("/proc/999999/environ", "r", 0))
    with pytest.raises(PermissionError):
        guard("open", ("/proc/999999/stat", "r", 0))


def test_host_declares_official_isolation_and_never_db_guard_bypass(guarded_events, tmp_path):
    probe, _, _ = guarded_events
    before = {"HERMES_STATE_DB_GUARD_BYPASS": "1", "PATH": "synthetic"}
    home = tmp_path / "profile"
    result = probe.isolated_host_environment(before, home)
    assert result == {"PATH": "synthetic", "HERMES_HOME": str(home), "HERMES_TEST_ISOLATION": "1"}
    assert "HERMES_STATE_DB_GUARD_BYPASS" in before
