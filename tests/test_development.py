# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Contracts for the source-only development launcher."""

import importlib.util
from importlib.machinery import SourceFileLoader
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from gi.repository import Gio


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_loader(
    "kukni_source_launcher",
    SourceFileLoader("kukni_source_launcher", str(ROOT / "bin/kukni")),
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class DevelopmentLauncherTests(unittest.TestCase):
    def test_development_argument_is_removed_and_file_is_preserved(self):
        path = "/tmp/quoted ' file;$(not-shell).png"
        with mock.patch.object(launcher, "_application_main", return_value=17) as run:
            self.assertEqual(
                launcher.main(["kukni", "--development", path], environ={}),
                17,
            )
        run.assert_called_once_with(["kukni", "--", path], development=True)

    def test_make_file_environment_is_literal_and_development_only(self):
        path = "/tmp/a $file; literal %24 and %25; 100% should-not-run.png"
        environment = {
            "KUKNI_MAKE_DEVELOPMENT_FILE": (
                "/tmp/a %24file; literal %2524 and %2525; "
                "100%25 should-not-run.png"
            )
        }
        with mock.patch.object(launcher, "_application_main", return_value=0) as run:
            self.assertEqual(
                launcher.main(["kukni", "--development"], environ=environment),
                0,
            )
        run.assert_called_once_with(["kukni", "--", path], development=True)

        with mock.patch.object(launcher, "_application_main", return_value=0) as run:
            launcher.main(["kukni", "--gapplication-service"], environ=environment)
        run.assert_called_once_with(
            ["kukni", "--gapplication-service"], development=False
        )

    def test_development_rejects_ambiguous_multiple_files(self):
        with mock.patch.object(launcher, "_application_main") as run:
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(
                    launcher.main(
                        ["kukni", "--development", "one.png", "two.png"],
                        environ={},
                    ),
                    2,
                )
        run.assert_not_called()

    def test_help_explains_isolation_and_unchanged_sandbox_requirement(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", new=output):
            self.assertEqual(launcher.main(["kukni", "--help"], environ={}), 0)
        self.assertIn("without installing or registering", output.getvalue())
        self.assertIn("never provides Nautilus previewer integration", output.getvalue())
        self.assertIn("sandboxes remain mandatory", output.getvalue())
        self.assertIn("/usr/bin/kukni --check", output.getvalue())
        self.assertIn("may remain unavailable even when the package is installed", output.getvalue())

    def test_check_and_version_remain_headless_launcher_actions(self):
        with mock.patch.object(launcher, "_diagnostics_main", return_value=19) as check:
            with mock.patch.object(launcher, "_application_main") as application:
                self.assertEqual(launcher.main(["kukni", "--check"], environ={}), 19)
        check.assert_called_once_with()
        application.assert_not_called()

        output = io.StringIO()
        with mock.patch.object(launcher, "_checkout_version", return_value="1.2.3-test"):
            with mock.patch.object(launcher, "_application_main") as application:
                with mock.patch("sys.stdout", new=output):
                    self.assertEqual(
                        launcher.main(["kukni", "--version"], environ={}), 0
                    )
        application.assert_not_called()
        self.assertEqual(output.getvalue(), "Kukni 1.2.3-test\n")

    def test_packaged_version_is_not_mislabeled_as_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.2.3~package7\n")
            self.assertEqual(launcher._checkout_version(root), "1.2.3~package7")

    def test_make_recipe_never_interpolates_file_into_shell_command(self):
        dangerous = (
            "/tmp/a '$quoted;$(shell touch MAKE-INJECTION)' "
            "literal-%24-%25-100%.png"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "Makefile").write_text((ROOT / "Makefile").read_text())
            result_file = root / "literal-result"
            fake_launcher = root / "bin/kukni"
            fake_launcher.write_text(
                "#!/usr/bin/python3\n"
                "import os, pathlib, sys\n"
                "assert sys.argv == ['./bin/kukni', '--development']\n"
                "pathlib.Path(os.environ['RESULT_FILE']).write_text("
                "os.environ['KUKNI_MAKE_DEVELOPMENT_FILE'])\n"
            )
            fake_launcher.chmod(0o755)
            environment = os.environ.copy()
            environment["RESULT_FILE"] = str(result_file)
            subprocess.run(
                ["make", "dev", f"FILE={dangerous}"],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            encoded = dangerous.replace("%", "%25").replace("$", "%24")
            self.assertEqual(result_file.read_text(), encoded)
            self.assertFalse((root / "MAKE-INJECTION").exists())


class DevelopmentApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys

        sys.path.insert(0, str(ROOT / "src"))
        from kukni import application

        cls.application_module = application

    def test_development_identity_is_non_unique_and_has_no_previewer(self):
        module = self.application_module
        with mock.patch.object(
            module,
            "NautilusPreviewerService",
            side_effect=AssertionError("development constructed live integration"),
        ):
            application = module.KukniApplication(development=True)
        self.assertEqual(
            application.get_application_id(), module.DEVELOPMENT_APPLICATION_ID
        )
        self.assertTrue(application.get_flags() & Gio.ApplicationFlags.NON_UNIQUE)
        self.assertTrue(application.get_flags() & Gio.ApplicationFlags.HANDLES_OPEN)
        self.assertIsNone(application._previewer)

    def test_installed_identity_and_flags_are_unchanged(self):
        module = self.application_module
        application = module.KukniApplication()
        self.assertEqual(application.get_application_id(), module.APPLICATION_ID)
        self.assertFalse(application.get_flags() & Gio.ApplicationFlags.NON_UNIQUE)
        self.assertTrue(application.get_flags() & Gio.ApplicationFlags.HANDLES_OPEN)
        self.assertIsNotNone(application._previewer)


if __name__ == "__main__":
    unittest.main()
