# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('kukni_deb_builder', ROOT / 'packaging/build-deb.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@unittest.skipUnless(shutil.which('dpkg-deb'), 'Debian package tooling unavailable')
class PackageTests(unittest.TestCase):
    def test_package_layout_ownership_dependencies_and_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = builder.build_package(ROOT, Path(temporary), '0.1.0~test1')
            dependencies = subprocess.check_output(['dpkg-deb', '--field', str(output), 'Depends']).decode()
            for required in ('bubblewrap', 'poppler-utils', 'webp-pixbuf-loader', 'apparmor'):
                self.assertIn(required, dependencies)
            self.assertEqual(
                subprocess.check_output(['dpkg-deb', '--field', str(output), 'Conflicts']).decode().strip(),
                'gnome-sushi',
            )
            self.assertEqual(
                subprocess.check_output(['dpkg-deb', '--field', str(output), 'Replaces']).decode().strip(),
                'gnome-sushi',
            )
            data = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(output)])
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive.getmembers():
                    self.assertEqual((member.uid, member.gid), (0, 0))
                    if not member.issym():
                        self.assertFalse(member.mode & 0o022, member.name)
                    if member.isdir():
                        self.assertEqual(member.mode, 0o755, member.name)
                launcher = archive.getmember('./usr/bin/kukni')
                self.assertTrue(launcher.issym())
                self.assertEqual(launcher.linkname, '../lib/kukni/launcher/kukni')
                for name in ('kukni-cr2-worker.py', 'kukni-image-worker.py'):
                    self.assertTrue(archive.getmember('./usr/lib/kukni/helpers/' + name).isfile())
                profile = archive.extractfile('./etc/apparmor.d/io.github.lamosty.Kukni').read().decode()
                self.assertIn('/usr/lib/kukni/launcher/kukni', profile)
                self.assertIn('flags=(unconfined)', profile)
                self.assertIn('userns,', profile)
                self.assertNotIn('sysctl', '\n'.join(line for line in profile.splitlines() if not line.startswith('#')))
                self.assertNotIn('/**', profile)
                service = archive.extractfile('./usr/share/dbus-1/services/org.gnome.NautilusPreviewer.service').read().decode()
                self.assertIn('Exec="/usr/bin/kukni" --gapplication-service', service)
                version = archive.extractfile('./usr/lib/kukni/VERSION').read().decode().strip()
                self.assertEqual(version, '0.1.0~test1')
            control = subprocess.check_output(['dpkg-deb', '--ctrl-tarfile', str(output)])
            with tarfile.open(fileobj=io.BytesIO(control)) as archive:
                conffiles = archive.extractfile('./conffiles').read().decode().splitlines()
                self.assertEqual(conffiles, ['/etc/apparmor.d/io.github.lamosty.Kukni'])
                for name in ('./postinst', './postrm'):
                    content = archive.extractfile(name).read()
                    subprocess.run(['sh', '-n'], input=content, check=True)
                    self.assertEqual(archive.getmember(name).mode, 0o755)
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                builder.build_package(ROOT, Path(temporary), '0.1.0~test1')

    def test_version_cannot_escape_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            for version in ('../bad', '0.1/../../bad', '0.1\nDepends: unsafe', '-bad'):
                with self.assertRaises(ValueError):
                    builder.build_package(ROOT, Path(temporary), version)

    def test_launcher_ignores_inherited_path_and_python_module_search_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            output = builder.build_package(ROOT, temporary / 'output', '0.1.0~test2')
            stage = temporary / 'stage'
            subprocess.run(['dpkg-deb', '--extract', str(output), str(stage)], check=True)
            hostile = temporary / 'hostile'
            hostile.mkdir()
            marker = temporary / 'sitecustomize-ran'
            (hostile / 'sitecustomize.py').write_text(
                f'from pathlib import Path\nPath({str(marker)!r}).write_text("loaded")\n'
            )
            fake_readlink = hostile / 'readlink'
            fake_readlink.write_text(f'#!/bin/sh\ntouch {str(marker)!r}\nexit 99\n')
            fake_readlink.chmod(0o755)
            environment = {
                'PATH': str(hostile),
                'PYTHONPATH': str(hostile),
                'HOME': str(temporary / 'home'),
            }
            result = subprocess.run(
                [str(stage / 'usr/bin/kukni'), '--version'], env=environment,
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(result.stdout.strip(), 'Kukni 0.1.0~test2')
            self.assertFalse(marker.exists())

    def test_maintainer_scripts_handle_conffile_lifecycle_without_system_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            fake_bin = temporary / 'bin'
            fake_bin.mkdir()
            log = temporary / 'parser.log'
            for name, body in (
                ('aa-enabled', '#!/bin/sh\nexit 0\n'),
                ('apparmor_parser', f'#!/bin/sh\nprintf "%s\\n" "$*" >> {str(log)!r}\n'),
            ):
                command = fake_bin / name
                command.write_text(body)
                command.chmod(0o755)
            profile = temporary / 'profile'
            environment = {'PATH': f'{fake_bin}:/usr/bin:/bin'}

            def script(name):
                content = (ROOT / 'packaging/debian' / name).read_text()
                content = content.replace(
                    '/etc/apparmor.d/io.github.lamosty.Kukni', str(profile),
                )
                target = temporary / name
                target.write_text(content)
                target.chmod(0o755)
                return target

            missing = subprocess.run(
                [str(script('postinst')), 'configure'], env=environment,
                check=True, capture_output=True, text=True,
            )
            self.assertIn('profile is absent', missing.stderr)
            self.assertFalse(log.exists())

            profile.write_text('test profile')
            subprocess.run([str(script('postinst')), 'configure'], env=environment, check=True)
            self.assertEqual(log.read_text().strip(), f'--replace {profile}')
            log.unlink()

            subprocess.run([str(script('postrm')), 'purge'], env=environment, check=True)
            self.assertFalse(log.exists())
            subprocess.run([str(script('postrm')), 'remove'], env=environment, check=True)
            self.assertEqual(log.read_text().strip(), f'--remove {profile}')

    def test_source_version_rejects_dirty_tree_and_uses_commit_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'VERSION').write_text('1.2.3\n')
            with mock.patch.object(builder.subprocess, 'check_output', side_effect=[
                b'', b'42\n', b'abc123def456\n',
            ]):
                self.assertEqual(builder.source_version(root), '1.2.3~alpha.42+gabc123def456')
            with mock.patch.object(builder.subprocess, 'check_output', return_value=b' M changed\n'):
                with self.assertRaisesRegex(ValueError, 'Commit source changes'):
                    builder.source_version(root)

    @unittest.skipUnless(Path('/usr/sbin/apparmor_parser').is_file(), 'AppArmor compiler unavailable')
    def test_policy_compiles_without_loading_or_changing_kernel_state(self):
        subprocess.run([
            '/usr/sbin/apparmor_parser', '--skip-kernel-load', '--skip-cache',
            str(ROOT / 'packaging/debian/io.github.lamosty.Kukni.apparmor'),
        ], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
