"""Portable input/journal checks; real installation is the separate Debian job."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "unified_server", Path(__file__).resolve().parents[1] / "deploy/install-server.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def inputs():
    value = json.loads((module.SOURCE / "deploy/server-inputs.example.json").read_bytes())
    for key in ("certificate_file", "private_key_file", "ca_file"):
        value["https"][key] = str(Path(__file__).resolve())
    return value


class InputTests(unittest.TestCase):
    def test_explicit_small_input_template(self):
        self.assertEqual(module.validate_inputs(inputs())["manager"], "deployoperator")

    def test_unsupported_identity_network_mode_and_business_onboarding_rejected(self):
        mutations = [
            lambda x: x.update(version=True),
            lambda x: x.update(manager="root"),
            lambda x: x.update(storage_evidence="format-disk"),
            lambda x: x["https"].update(allowed_cidrs=["0.0.0.0/0"]),
            lambda x: x["https"].update(bind_address="0.0.0.0"),
            lambda x: x["https"].update(gateway_port=x["https"]["filebrowser_port"]),
            lambda x: x["https"].update(server_name="valid; shell-injection"),
            lambda x: x["filebrowser"].update(mode="candidate", image=None),
            lambda x: x["filebrowser"].update(image="arbitrary:latest"),
            lambda x: x["gateway"].update(initial_identity_file="approve-everyone.json"),
        ]
        for mutate in mutations:
            candidate = inputs()
            mutate(candidate)
            with self.subTest(value=candidate), self.assertRaises((module.SuiteError, ValueError)):
                module.validate_inputs(candidate)


class JournalTests(unittest.TestCase):
    def test_real_file_failure_resume_rerun_and_changed_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"

            def write(target, data):
                target.write_bytes(data)

            journal = module.Journal(path, "same-input-and-sources", write)
            steps = []
            journal.stage("prepare", lambda: steps.append("prepared"))

            def unavailable():
                raise OSError("temporary dependency unavailable")

            with self.assertRaises(OSError):
                journal.stage("start", unavailable)
            saved = json.loads(path.read_bytes())
            self.assertEqual(saved["completed"], ["prepare"])
            self.assertEqual(saved["events"][-1]["result"], "failed-or-interrupted")
            resumed = module.Journal(path, "same-input-and-sources", write)
            resumed.stage("prepare", lambda: self.fail("successful initialization repeated"))
            resumed.stage("start", lambda: steps.append("started"))
            resumed.stage("start", lambda: steps.append("reconciled"), reconcile=True)
            self.assertEqual(steps, ["prepared", "started", "reconciled"])
            before = path.read_bytes()
            with self.assertRaises(module.SuiteError):
                module.Journal(path, "changed-input-or-upgrade", write)
            self.assertEqual(path.read_bytes(), before)

    def test_configuration_fingerprint_covers_each_component_and_inputs(self):
        original = {"inputs": inputs(), "versions": {"gateway": "a" * 40, "filebrowser": "b" * 40}}
        for key in ("gateway", "filebrowser"):
            changed = copy.deepcopy(original)
            changed["versions"][key] = "c" * 40
            self.assertNotEqual(module.fingerprint(original), module.fingerprint(changed))


class ImageResumeTests(unittest.TestCase):
    def test_recorded_image_is_reused_after_interruption_before_stage_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            revision = "b" * 40
            reference = "cf-filebrowser-unified:" + revision
            saved = {"source_sha": revision, "reference": reference, "image_id": "sha256:old"}
            (state / "filebrowser-image.json").write_text(json.dumps(saved))
            installer = object.__new__(module.Installer)
            installer.lock = {"filebrowser": {"revision": revision}}
            installer.raw = {"filebrowser": {"image": None}}
            installer.core = SimpleNamespace(read_json=lambda p: json.loads(p.read_bytes()))
            commands = []

            def inspect_only(command):
                commands.append(command)
                self.assertEqual(command, ["docker", "image", "inspect", reference])
                return json.dumps(
                    [
                        {
                            "Id": "sha256:old",
                            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
                        }
                    ]
                )

            installer.command = inspect_only
            with patch.object(module, "STATE", state):
                installer.filebrowser_image()
                self.assertEqual(len(commands), 1)
                saved["image_id"] = "sha256:different"
                (state / "filebrowser-image.json").write_text(json.dumps(saved))
                with self.assertRaises(module.SuiteError):
                    installer.filebrowser_image()
            self.assertEqual(len(commands), 2)


if __name__ == "__main__":
    unittest.main()
