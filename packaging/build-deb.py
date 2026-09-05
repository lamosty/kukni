#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Build an inspectable Ubuntu package without root or system modifications."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_LAUNCHER = '/usr/bin/kukni'


def source_version(root: Path) -> str:
    # A clean versioned checkout makes upgrades monotonic and the installed
    # payload traceable. Tests call build_package with their explicit version.
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=root)
    if status.strip():
        raise ValueError('Commit source changes before building a release package')
    count = subprocess.check_output(['git', 'rev-list', '--count', 'HEAD'], cwd=root).decode().strip()
    revision = subprocess.check_output(['git', 'rev-parse', '--short=12', 'HEAD'], cwd=root).decode().strip()
    return f'{(root / "VERSION").read_text().strip()}~alpha.{count}+g{revision}'


def build_package(root: Path, destination: Path, version: str) -> Path:
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~:-]*', version):
        raise ValueError('Invalid Debian package version')
    destination.mkdir(parents=True, exist_ok=True)
    output = destination.resolve() / f'kukni_{version}_all.deb'
    if output.exists():
        raise ValueError('Refusing to overwrite an existing package artifact')

    with tempfile.TemporaryDirectory(prefix='kukni-deb-') as temporary:
        stage = Path(temporary)

        def copy(source: Path, target: str, mode=0o644):
            out = stage / target
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, out)
            out.chmod(mode)

        copy(root / 'packaging/kukni-launcher', 'usr/lib/kukni/launcher/kukni', 0o755)
        copy(root / 'bin/kukni', 'usr/lib/kukni/bin/kukni', 0o755)
        copy(root / 'VERSION', 'usr/lib/kukni/VERSION')
        for folder in ('src/kukni', 'helpers'):
            for source in sorted((root / folder).rglob('*')):
                if source.is_file() and source.suffix in ('.py', '.css') and '__pycache__' not in source.parts:
                    relative = source.relative_to(root)
                    copy(source, 'usr/lib/kukni/' + str(relative), 0o755 if folder == 'helpers' else 0o644)
        (stage / 'usr/bin').mkdir(parents=True)
        (stage / 'usr/bin/kukni').symlink_to('../lib/kukni/launcher/kukni')
        for template, kind, target in (
            ('io.github.lamosty.Kukni.desktop.in', 'desktop', 'applications/io.github.lamosty.Kukni.desktop'),
            ('io.github.lamosty.Kukni.service.in', 'dbus', 'dbus-1/services/io.github.lamosty.Kukni.service'),
            ('org.gnome.NautilusPreviewer.service.in', 'dbus', 'dbus-1/services/org.gnome.NautilusPreviewer.service'),
        ):
            rendered = subprocess.check_output([
                '/usr/bin/python3', '-B', str(root / 'packaging/render-template.py'),
                kind, SYSTEM_LAUNCHER, str(root / 'packaging' / template),
            ])
            target_path = stage / 'usr/share' / target
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(rendered)
            target_path.chmod(0o644)
        copy(root / 'LICENSE', 'usr/share/doc/kukni/copyright')
        copy(root / 'README.md', 'usr/share/doc/kukni/README.md')
        copy(root / 'packaging/debian/io.github.lamosty.Kukni.apparmor',
             'etc/apparmor.d/io.github.lamosty.Kukni')
        for name in ('postinst', 'postrm'):
            copy(root / 'packaging/debian' / name, 'DEBIAN/' + name, 0o755)
        (stage / 'DEBIAN/control').write_text(
            f'Package: kukni\nVersion: {version}\nArchitecture: all\n'
            'Maintainer: Kukni contributors <noreply@github.com>\n'
            'Section: utils\nPriority: optional\n'
            'Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0 (>= 4.10), '
            'gir1.2-adw-1 (>= 1.4), util-linux, poppler-utils, bubblewrap, '
            'webp-pixbuf-loader, apparmor (>= 4.0)\n'
            'Recommends: gir1.2-webkit-6.0\n'
            'Suggests: nautilus\nConflicts: gnome-sushi\nReplaces: gnome-sushi\n'
            'Homepage: https://github.com/lamosty/kukni\n'
            'Description: Keyboard-first file previews for GNOME Files\n'
            ' Preview pictures, documents and text in a native GTK window.\n'
            ' Includes Nautilus Space-key integration and app-scoped namespace\n'
            ' permission for the mandatory PDF and HTML process sandboxes.\n'
        )
        (stage / 'DEBIAN/conffiles').write_text('/etc/apparmor.d/io.github.lamosty.Kukni\n')
        (stage / 'usr/lib/kukni/VERSION').write_text(version + '\n')
        # TemporaryDirectory starts private; package directory modes must not
        # inherit that mode or a caller's restrictive/group-writable umask.
        stage.chmod(0o755)
        for directory in stage.rglob('*'):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o755)
        for name in ('control', 'conffiles'):
            (stage / 'DEBIAN' / name).chmod(0o644)
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(output)], check=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    try:
        package = build_package(ROOT, args.output_dir, source_version(ROOT))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'{error}\n')
    print(package)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
