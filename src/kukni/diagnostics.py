# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Headless, bounded installation checks; never inspect the user's documents."""

from dataclasses import dataclass
from pathlib import Path
import struct
import tempfile
import zlib


SYSTEM_ROOT = Path('/usr/lib/kukni')


@dataclass(frozen=True)
class Check:
    name: str
    ready: bool
    detail: str
    required: bool = True
    kind: str = 'self-test'


def system_integration_check(
    *, project_root: Path | None = None, home: Path | None = None,
) -> Check | None:
    """Warn when a packaged executable is hidden by a per-user preview.

    Presence is enough to diagnose D-Bus/desktop precedence. Deliberately do
    not read, resolve, or print any user-owned file or environment value.
    """

    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]
    if project_root != SYSTEM_ROOT:
        return None
    if home is None:
        home = Path.home()
    local = home / '.local'
    overrides = (
        local / 'bin/kukni',
        local / 'share/applications/io.github.lamosty.Kukni.desktop',
        local / 'share/dbus-1/services/io.github.lamosty.Kukni.service',
        local / 'share/dbus-1/services/org.gnome.NautilusPreviewer.service',
    )
    shadowed = any(path.exists() or path.is_symlink() for path in overrides)
    if shadowed:
        return Check(
            'System integration', False,
            'A per-user Kukni preview install may take precedence. Close the preview '
            'and run its user-owned uninstaller as your normal user; never remove '
            'home-directory files as root.',
            required=False, kind='warning',
        )
    return Check(
        'System integration', True,
        'No default per-user Kukni launcher or activation override was found.',
        required=False, kind='inspection',
    )


def sample_png() -> bytes:
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\x33\x66\x99\xff'))
            + chunk(b'IEND', b''))


def sample_pdf() -> bytes:
    stream = b'BT /F1 24 Tf 72 720 Td (Kukni installation check) Tj ET\n'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'endstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    output = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')
    xref = len(output)
    output.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


def check_runtime() -> list[Check]:
    # @decision These probes run only in an explicit headless CLI process, never
    # on GTK's main thread. Passing requires real pixels/pages, not mere imports
    # or an accepted fallback. No user files or environment values are printed.
    from .renderers.image import run_image_worker
    from .renderers.pdf import pdf_runtime_unavailable_reason, render_pdf_first_page
    from .renderers.html import webkit_runtime_available
    import gi
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf

    checks = []
    with tempfile.TemporaryDirectory(prefix='kukni-check-') as temporary:
        image = Path(temporary) / 'check.png'
        image.write_bytes(sample_png())
        try:
            result = run_image_worker(image)
            if result.pixels != b'\x33\x66\x99\xff':
                raise ValueError('Wrong synthetic PNG pixels')
            loaders = {item.get_name() for item in GdkPixbuf.Pixbuf.get_formats()}
            missing = {'png', 'jpeg', 'webp'} - loaders
            if missing:
                checks.append(Check('Images self-test', False,
                                    'Missing image loaders: ' + ', '.join(sorted(missing))))
            else:
                checks.append(Check('Images self-test', True,
                                    'Core loaders present; a real PNG worker returned correct pixels.'))
        except Exception:
            checks.append(Check('Images self-test', False,
                                'The bounded image decoder failed its synthetic PNG check.'))
        problem = pdf_runtime_unavailable_reason()
        if problem:
            checks.append(Check('PDF self-test', False, problem))
        else:
            pdf = Path(temporary) / 'check.pdf'
            pdf.write_bytes(sample_pdf())
            try:
                page = render_pdf_first_page(pdf)
                if not page.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise ValueError('No rendered PDF page')
                checks.append(Check('PDF self-test', True,
                                    'A real PDF page rendered inside the required sandbox.'))
            except Exception:
                checks.append(Check('PDF self-test', False,
                                    'The sandboxed PDF renderer failed its synthetic document check.'))
        html = webkit_runtime_available()
        checks.append(Check(
            'HTML prerequisites', html,
            'Prerequisite check only; the engine and required sandbox are available, '
            'but no HTML page was rendered.' if html else
            'Prerequisite check only; the optional engine or its required sandbox is unavailable.',
            required=False, kind='prerequisite',
        ))
    integration = system_integration_check()
    if integration is not None:
        checks.append(integration)
    return checks


def main() -> int:
    try:
        checks = check_runtime()
    except (ImportError, ValueError):
        print('Kukni is missing its core GTK/Python runtime dependencies.')
        return 1
    for check in checks:
        if check.kind == 'warning':
            state = 'Warning'
        elif check.kind == 'prerequisite':
            state = 'Available' if check.ready else 'Unavailable'
        elif check.kind == 'self-test':
            state = 'Passed' if check.ready else 'Failed'
        else:
            state = 'Clear' if check.ready else 'Warning'
        optional = ' (optional)' if not check.required else ''
        print(f'{check.name}{optional}: {state}\n  {check.detail}')
    ready = all(check.ready for check in checks if check.required)
    if not ready:
        print('Core preview setup is incomplete. See the installation instructions; do not disable system security.')
    return 0 if ready else 1
