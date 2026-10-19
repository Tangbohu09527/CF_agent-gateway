"""Portable input/journal checks; real installation is the separate Debian job."""

import copy
import importlib.util
import json
import os
import stat
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
            lambda x: x["https"].update(allowed_cidrs=["127.0.0.0/8"]),
            lambda x: x["https"].update(allowed_cidrs=["169.254.0.0/16"]),
            lambda x: x["https"].update(allowed_cidrs=["192.0.2.0/24", "192.0.2.0/24"]),
            lambda x: x["https"].update(bind_address="0.0.0.0"),
            lambda x: x["https"].update(gateway_port=x["https"]["filebrowser_port"]),
            lambda x: x["https"].update(server_name="valid; shell-injection"),
            lambda x: x["https"].update(server_name="empty..label"),
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


class BaseStatusTests(unittest.TestCase):
    def test_base_readiness_does_not_require_an_external_hermes_connection(self):
        installer = object.__new__(module.Installer)
        installer.container_test = True
        calls = []
        installer.fb = lambda action: calls.append(("filebrowser", action))
        installer.gateway = lambda stage: calls.append(("gateway", stage))
        with patch.object(module, "FB", Path(__file__).parent):
            installer.status()
        self.assertEqual(calls, [("filebrowser", "validate"), ("gateway", "diagnose-base")])


@unittest.skipUnless(os.name == "posix", "requires actual POSIX file modes; exercised by Linux CI")
class CheckoutModeTests(unittest.TestCase):
    def test_https_mount_is_traversable_under_protected_installer_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            state.chmod(0o700)
            installer = object.__new__(module.Installer)
            installer.lock = {"images": {"https": "nginx:fixed"}}
            installer.raw = inputs()

            def once(path, content, *, owner=(0, 0), mode=0o600):
                path.write_bytes(content.encode() if isinstance(content, str) else content)
                path.chmod(mode)

            installer.core = SimpleNamespace(
                safe_parents=lambda path: None,
                pull_record=lambda *args: {"digest_reference": "nginx@sha256:" + "a" * 64},
                administrator_input=lambda *args: b"synthetic test input",
                once=once,
            )
            installer.manager = "deployoperator"
            previous = os.umask(0o077)
            try:
                with (
                    patch.object(module, "STATE", state),
                    patch.object(module.os, "chown") as chown,
                ):
                    installer.https_prepare()
                    chown.assert_called_once_with(state / "https", 0, 10001)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE((state / "https").stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            for name in ("server.key", "server.crt", "ca.crt", "nginx.conf"):
                self.assertEqual(stat.S_IMODE((state / "https" / name).stat().st_mode), 0o440)

    def test_only_reviewed_entrypoint_becomes_executable_by_service_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            entrypoint = root / "scripts/container-entrypoint.sh"
            entrypoint.write_text("#!/bin/sh\nexit 0\n")
            entrypoint.chmod(0o700)
            private = root / "private"
            private.write_text("synthetic fixture secret")
            private.chmod(0o600)
            installer = object.__new__(module.Installer)
            installer.lock = {
                "filebrowser": {"repository": "fixed-repository", "revision": "b" * 40}
            }
            checks = []

            def regular(path, *, mode):
                checks.append((path, mode))
                self.assertFalse(path.is_symlink())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                return path.read_bytes()

            installer.core = SimpleNamespace(fixed_checkout=lambda *a, **k: None, regular=regular)
            with patch.object(module, "FB", root):
                installer.checkout_filebrowser()
                self.assertEqual(entrypoint.stat().st_mode & 0o777, 0o755)
                installer.checkout_filebrowser()
                self.assertEqual(private.stat().st_mode & 0o777, 0o600)
                self.assertEqual(private.read_text(), "synthetic fixture secret")
                before = len(checks)
                entrypoint.chmod(0o777)
                with self.assertRaises(module.SuiteError):
                    installer.checkout_filebrowser()
                self.assertEqual(len(checks), before)
                self.assertEqual(entrypoint.stat().st_mode & 0o777, 0o777)


if __name__ == "__main__":
    unittest.main()
