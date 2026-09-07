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

    def test_passing_renderer_self_tests_cannot_mask_shadowed_activation(self):
        checks = [
            diagnostics.Check('Images', True, 'ok'),
            diagnostics.Check('PDF', True, 'ok'),
            diagnostics.Check('Installed activation', False, 'shadowed', kind='warning'),
        ]
        with mock.patch.object(diagnostics, 'check_runtime', return_value=checks), \
                mock.patch('builtins.print') as output:
            self.assertEqual(diagnostics.main(), 1)
        rendered = '\n'.join(call.args[0] for call in output.call_args_list)
        self.assertIn('Installed activation: Failed', rendered)
        self.assertNotIn('Installed activation: Warning', rendered)

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
        self.assertIn('may override', check.detail)
        self.assertIn('otherwise review', check.detail)

    def test_nondefault_xdg_data_home_override_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_data = root / 'private-data-name'
            override = private_data / 'applications/io.github.lamosty.Kukni.desktop'
            override.parent.mkdir(parents=True)
            override.write_text('private desktop contents')
            check = diagnostics.system_integration_check(
                project_root=diagnostics.SYSTEM_ROOT,
                home=root / 'home',
                environ={
                    'XDG_DATA_HOME': str(private_data),
                    'XDG_DATA_DIRS': '/usr/share',
                },
            )
        self.assertFalse(check.ready)
        self.assertTrue(check.required)
        self.assertNotIn('private-data-name', check.detail)
        self.assertNotIn('private desktop contents', check.detail)

    def test_nondefault_xdg_data_dir_before_system_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            earlier = root / 'private-search-name'
            override = earlier / 'dbus-1/services/io.github.lamosty.Kukni.service'
            override.parent.mkdir(parents=True)
            override.write_text('private service contents')
            check = diagnostics.system_integration_check(
                project_root=diagnostics.SYSTEM_ROOT,
                home=root / 'home',
                environ={
                    'XDG_DATA_HOME': 'relative-path-is-invalid',
                    'XDG_DATA_DIRS': f'relative-also-invalid:{earlier}:/usr/share',
                },
            )
        self.assertFalse(check.ready)
        self.assertNotIn('private-search-name', check.detail)
        self.assertNotIn('private service contents', check.detail)

    def test_xdg_runtime_service_override_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'private-runtime-name'
            override = runtime / 'dbus-1/services/org.gnome.NautilusPreviewer.service'
            override.parent.mkdir(parents=True)
            override.write_text('private runtime service contents')
            check = diagnostics.system_integration_check(
                project_root=diagnostics.SYSTEM_ROOT,
                home=root / 'home',
                environ={
                    'XDG_RUNTIME_DIR': str(runtime),
                    'XDG_DATA_DIRS': '/usr/share',
                },
            )
        self.assertFalse(check.ready)
        self.assertNotIn('private-runtime-name', check.detail)
        self.assertNotIn('private runtime service contents', check.detail)

    def test_xdg_directory_after_system_does_not_shadow_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            later = root / 'later'
            override = later / 'dbus-1/services/org.gnome.NautilusPreviewer.service'
            override.parent.mkdir(parents=True)
            override.touch()
            check = diagnostics.system_integration_check(
                project_root=diagnostics.SYSTEM_ROOT,
                home=root / 'home',
                environ={'XDG_DATA_DIRS': f'/usr/share:{later}'},
            )
        self.assertTrue(check.ready)

    def test_xdg_search_without_system_data_hides_only_desktop_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = str(Path(temporary) / 'private-only-search')
            check = diagnostics.system_integration_check(
                project_root=diagnostics.SYSTEM_ROOT,
                home=Path(temporary) / 'home',
                environ={'XDG_DATA_DIRS': private},
            )
        self.assertFalse(check.ready)
        self.assertIn('desktop entry', check.detail)
        self.assertNotIn('D-Bus', check.detail)
        self.assertNotIn(private, check.detail)

    def test_source_checkout_explicitly_limits_integration_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            check = diagnostics.system_integration_check(
                project_root=Path('/tmp/source-checkout'), home=Path(temporary),
            )
        self.assertTrue(check.ready)
        self.assertEqual(check.kind, 'source')
        self.assertIn('not inspected', check.detail)

    def test_absent_bus_and_absent_owner_are_distinct_nonfailures(self):
        def unavailable(_name):
            raise diagnostics._BusUnavailable

        no_bus = diagnostics.running_owner_check(
            project_root=diagnostics.SYSTEM_ROOT, owner_pid=unavailable,
        )
        not_running = diagnostics.running_owner_check(
            project_root=diagnostics.SYSTEM_ROOT, owner_pid=lambda _name: None,
        )
        self.assertTrue(no_bus.ready)
        self.assertEqual(no_bus.kind, 'unavailable')
        self.assertTrue(not_running.ready)
        self.assertEqual(not_running.kind, 'not-running')

    def test_running_owner_origin_is_checked_without_disclosing_command_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            process = proc_root / '42'
            process.mkdir()
            secret = '/home/private-person/secret-checkout/bin/kukni'
            (process / 'cmdline').write_bytes(
                b'/usr/bin/python3\0' + secret.encode() + b'\0--gapplication-service\0'
            )
            check = diagnostics.running_owner_check(
                project_root=diagnostics.SYSTEM_ROOT,
                owner_pid=lambda _name: 42,
                proc_root=proc_root,
            )
        self.assertFalse(check.ready)
        self.assertNotIn('private-person', check.detail)
        self.assertNotIn('secret-checkout', check.detail)
        with mock.patch.object(diagnostics, 'check_runtime', return_value=[check]), \
                mock.patch('builtins.print') as output:
            self.assertEqual(diagnostics.main(), 1)
        rendered = '\n'.join(call.args[0] for call in output.call_args_list)
        self.assertNotIn('private-person', rendered)
        self.assertNotIn('secret-checkout', rendered)

    def test_running_installed_owner_is_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            process = proc_root / '7'
            process.mkdir()
            (process / 'cmdline').write_bytes(
                b'/usr/bin/python3\0-I\0-B\0'
                b'/usr/lib/kukni/launcher/../bin/kukni\0--gapplication-service\0'
            )
            check = diagnostics.running_owner_check(
                project_root=diagnostics.SYSTEM_ROOT,
                owner_pid=lambda _name: 7,
                proc_root=proc_root,
            )
        self.assertTrue(check.ready)
        self.assertEqual(check.kind, 'inspection')
        self.assertIn('launched from', check.detail)
        self.assertIn('updated while', check.detail)

    def test_file_argument_cannot_spoof_installed_launch_origin(self):
        cases = {
            'old launcher previewing installed path': (
                b'/usr/bin/python3\0/home/private/old/bin/kukni\0'
                b'/usr/lib/kukni/bin/kukni\0'
            ),
            'inline code with installed-looking argument': (
                b'/usr/bin/python3\0-c\0private-code\0/usr/lib/kukni/bin/kukni\0'
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            for pid, (label, command_line) in enumerate(cases.items(), 20):
                with self.subTest(label):
                    process = proc_root / str(pid)
                    process.mkdir()
                    (process / 'cmdline').write_bytes(command_line)
                    check = diagnostics.running_owner_check(
                        project_root=diagnostics.SYSTEM_ROOT,
                        owner_pid=lambda _name, process_id=pid: process_id,
                        proc_root=proc_root,
                    )
                    self.assertFalse(check.ready)
                    if label.startswith('old'):
                        self.assertIn('path other than', check.detail)
                    else:
                        self.assertIn('could not be verified', check.detail)

    def test_running_owner_with_missing_process_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as temporary:
            check = diagnostics.running_owner_check(
                project_root=diagnostics.SYSTEM_ROOT,
                owner_pid=lambda _name: 99,
                proc_root=Path(temporary),
            )
        self.assertFalse(check.ready)
        self.assertIn('could not be verified', check.detail)

    def test_dbus_probe_is_bounded_and_never_activates_application(self):
        replies = [
            subprocess_result('(true,)'),
            subprocess_result('(uint32 123,)'),
        ]
        with mock.patch.dict(
            diagnostics.os.environ,
            {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'}, clear=True,
        ), mock.patch.object(diagnostics.subprocess, 'run', side_effect=replies) as run:
            self.assertEqual(diagnostics._session_name_pid(diagnostics.DBUS_NAMES[0]), 123)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertNotIn('StartServiceByName', ' '.join(command))
            self.assertEqual(call.kwargs['timeout'], diagnostics.DBUS_TIMEOUT_SECONDS)

    def test_dbus_probe_refuses_autolaunch_without_explicit_address(self):
        with mock.patch.dict(diagnostics.os.environ, {'DISPLAY': ':0'}, clear=True), \
                mock.patch.object(diagnostics.subprocess, 'run') as run:
            check = diagnostics.running_owner_check(
                project_root=diagnostics.SYSTEM_ROOT,
            )
        self.assertTrue(check.ready)
        self.assertEqual(check.kind, 'unavailable')
        run.assert_not_called()

    def test_dbus_probe_rejects_explicit_autolaunch_transports(self):
        addresses = (
            'autolaunch:',
            'unix:path=/run/user/1000/bus;autolaunch:',
        )
        for address in addresses:
            with self.subTest('direct' if address.startswith('auto') else 'fallback'), \
                    mock.patch.dict(
                        diagnostics.os.environ,
                        {'DBUS_SESSION_BUS_ADDRESS': address}, clear=True,
                    ), mock.patch.object(diagnostics.subprocess, 'run') as run:
                check = diagnostics.running_owner_check(
                    project_root=diagnostics.SYSTEM_ROOT,
                )
            self.assertTrue(check.ready)
            self.assertEqual(check.kind, 'unavailable')
            run.assert_not_called()

    def test_dbus_timeout_and_failure_are_private_nonfatal_unavailability(self):
        private_address = 'unix:path=/private-address-token'
        failures = (
            ('timeout', {'side_effect': diagnostics.subprocess.TimeoutExpired(
                ['/usr/bin/gdbus'], 2,
            )}),
            ('failure', {'return_value': diagnostics.subprocess.CompletedProcess(
                [], 1, stdout='private output',
            )}),
        )
        for label, mock_result in failures:
            with self.subTest(label), mock.patch.dict(
                diagnostics.os.environ,
                {'DBUS_SESSION_BUS_ADDRESS': private_address}, clear=True,
            ), mock.patch.object(diagnostics.subprocess, 'run', **mock_result):
                check = diagnostics.running_owner_check(
                    project_root=diagnostics.SYSTEM_ROOT,
                )
            self.assertTrue(check.ready)
            self.assertEqual(check.kind, 'unavailable')
            self.assertNotIn('private-address-token', check.detail)
            self.assertNotIn('private output', check.detail)


def subprocess_result(stdout):
    return diagnostics.subprocess.CompletedProcess([], 0, stdout=stdout)


if __name__ == '__main__':
    unittest.main()
