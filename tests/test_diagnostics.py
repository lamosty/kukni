# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kukni import diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_required_unavailable_preview_returns_failure_not_success(self):
        checks = [diagnostics.Check('Images', True, 'ok'), diagnostics.Check('PDF', False, 'sandbox unavailable')]
        with mock.patch.object(diagnostics, 'check_runtime', return_value=checks), mock.patch('builtins.print'):
            self.assertEqual(diagnostics.main(), 1)

    def test_optional_engine_does_not_block_core(self):
        checks = [diagnostics.Check('Images', True, 'ok'), diagnostics.Check('PDF', True, 'ok'),
                  diagnostics.Check('HTML', False, 'optional', required=False)]
        with mock.patch.object(diagnostics, 'check_runtime', return_value=checks), mock.patch('builtins.print'):
            self.assertEqual(diagnostics.main(), 0)

    def test_unavailable_pdf_is_reported_without_attempting_unconfined_render(self):
        with (
            mock.patch('kukni.renderers.pdf.pdf_runtime_unavailable_reason', return_value='Required sandbox unavailable'),
            mock.patch('kukni.renderers.pdf.render_pdf_first_page') as render,
        ):
            checks = diagnostics.check_runtime()
        self.assertTrue(next(check for check in checks if check.name == 'Images self-test').ready)
        self.assertFalse(next(check for check in checks if check.name == 'PDF self-test').ready)
        render.assert_not_called()

    def test_html_result_is_explicitly_prerequisite_only(self):
        checks = [diagnostics.Check(
            'HTML prerequisites', True, 'no render', required=False, kind='prerequisite',
        )]
        with mock.patch.object(diagnostics, 'check_runtime', return_value=checks), \
                mock.patch('builtins.print') as output:
            self.assertEqual(diagnostics.main(), 0)
        rendered = '\n'.join(call.args[0] for call in output.call_args_list)
        self.assertIn('HTML prerequisites (optional): Available', rendered)
        self.assertNotIn('Passed', rendered)

    def test_packaged_check_warns_about_user_preview_without_disclosing_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'private-home-name'
            override = home / '.local/share/dbus-1/services/org.gnome.NautilusPreviewer.service'
            override.parent.mkdir(parents=True)
            override.write_text('private contents must not be read')
            check = diagnostics.system_integration_check(
                project_root=diagnostics.SYSTEM_ROOT, home=home,
            )
        self.assertIsNotNone(check)
        self.assertEqual(check.kind, 'warning')
        self.assertFalse(check.ready)
        self.assertNotIn('private-home-name', check.detail)
        self.assertNotIn('private contents', check.detail)

    def test_source_checkout_does_not_report_itself_as_a_user_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(diagnostics.system_integration_check(
                project_root=Path('/tmp/source-checkout'), home=Path(temporary),
            ))


if __name__ == '__main__':
    unittest.main()
