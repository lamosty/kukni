# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Headless, bounded installation checks; never inspect the user's documents."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import posixpath
import re
import struct
import subprocess
import tempfile
import zlib


SYSTEM_ROOT = Path('/usr/lib/kukni')
SYSTEM_DATA_ROOT = Path('/usr/share')
DBUS_NAMES = ('io.github.lamosty.Kukni', 'org.gnome.NautilusPreviewer')
DBUS_DAEMON = 'org.freedesktop.DBus'
DBUS_PATH = '/org/freedesktop/DBus'
DBUS_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class Check:
    name: str
    ready: bool
    detail: str
    required: bool = True
    kind: str = 'self-test'


def system_integration_check(
    *, project_root: Path | None = None, home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Check:
    """Fail when packaged activation is hidden by earlier XDG entries.

    Presence is enough to diagnose D-Bus/desktop precedence. Deliberately do
    not read, resolve, or print any user-owned file or environment value. XDG
    ignores relative data paths, so diagnostics do too.
    """

    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]
    if project_root != SYSTEM_ROOT:
        return Check(
            'Installed activation', True,
            'Source-checkout runtime only; installed desktop and D-Bus activation '
            'were not inspected.',
            required=False, kind='source',
        )
    if home is None:
        home = Path.home()
    if environ is None:
        environ = os.environ

    data_home_value = environ.get('XDG_DATA_HOME', '')
    data_home = Path(data_home_value) if data_home_value and Path(data_home_value).is_absolute() \
        else home / '.local/share'
    data_dirs_value = environ.get('XDG_DATA_DIRS', '')
    if data_dirs_value:
        data_dirs = tuple(
            Path(value) for value in data_dirs_value.split(':')
            if value and Path(value).is_absolute()
        )
    else:
        data_dirs = (Path('/usr/local/share'), SYSTEM_DATA_ROOT)

    desktop_roots = (data_home, *data_dirs)
    try:
        desktop_system_position = desktop_roots.index(SYSTEM_DATA_ROOT)
    except ValueError:
        return Check(
            'Installed activation', False,
            'The active XDG data search configuration excludes the packaged '
            'desktop entry.',
            kind='warning',
        )

    runtime_value = environ.get('XDG_RUNTIME_DIR', '')
    runtime_root = Path(runtime_value) if runtime_value and Path(runtime_value).is_absolute() \
        else None
    # @constraint dbus-daemon searches the runtime directory first, followed by
    # XDG data directories, then its compiled data directory (normally
    # /usr/share) even if XDG_DATA_DIRS omitted it. Desktop lookup has no such
    # final fallback.
    service_roots = tuple(
        root for root in (runtime_root, data_home, *data_dirs, SYSTEM_DATA_ROOT)
        if root is not None
    )
    service_system_position = service_roots.index(SYSTEM_DATA_ROOT)
    service_metadata = (
        Path('dbus-1/services/io.github.lamosty.Kukni.service'),
        Path('dbus-1/services/org.gnome.NautilusPreviewer.service'),
    )
    desktop = Path('applications/io.github.lamosty.Kukni.desktop')
    shadowed_desktop = any(
        (root / desktop).exists() or (root / desktop).is_symlink()
        for root in desktop_roots[:desktop_system_position]
    )
    shadowed_service = any(
        (root / relative).exists() or (root / relative).is_symlink()
        for root in service_roots[:service_system_position] for relative in service_metadata
    )
    local_launcher = home / '.local/bin/kukni'
    shadowed_launcher = local_launcher.exists() or local_launcher.is_symlink()
    if shadowed_launcher or shadowed_desktop or shadowed_service:
        return Check(
            'Installed activation', False,
            'A launcher, desktop entry, or D-Bus service earlier in the user search '
            'order may override the packaged Kukni install. If it belongs to the old '
            'per-user Kukni installation, close it and run its user-owned uninstaller '
            'as your normal user; otherwise review the conflicting registration. '
            'Never remove home-directory files as root.',
            kind='warning',
        )
    return Check(
        'Installed activation', True,
        'No earlier launcher, desktop entry, or D-Bus service shadows the packaged install.',
        kind='inspection',
    )


class _BusUnavailable(Exception):
    pass


class _OwnerUnverifiable(Exception):
    pass


def _gdbus_call(method: str, name: str) -> str:
    """Call only the D-Bus daemon, with a hard deadline and private output."""

    address = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    if not address or any(
        entry.startswith('autolaunch:') for entry in address.split(';')
    ):
        # `gdbus --session` may auto-launch a bus when DISPLAY is available.
        # An explicit address can also contain an autolaunch transport, including
        # as a fallback after another address. Diagnostics must never use either.
        raise _BusUnavailable
    command = [
        '/usr/bin/gdbus', 'call', '--address', address, '--dest', DBUS_DAEMON,
        '--object-path', DBUS_PATH, '--method', f'{DBUS_DAEMON}.{method}', name,
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=DBUS_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _BusUnavailable from error
    if result.returncode != 0:
        raise _BusUnavailable
    return result.stdout.strip()


def _session_name_pid(name: str) -> int | None:
    # @security NameHasOwner and GetConnectionUnixProcessID are daemon methods;
    # unlike StartServiceByName or an application method, neither activates or
    # closes an application. Captured daemon output is parsed but never printed.
    has_owner = _gdbus_call('NameHasOwner', name)
    if has_owner == '(false,)':
        return None
    if has_owner != '(true,)':
        raise _BusUnavailable
    try:
        reply = _gdbus_call('GetConnectionUnixProcessID', name)
    except _BusUnavailable as error:
        # The owner may have exited between the bounded daemon calls. We know an
        # owner existed but cannot make an origin claim about it.
        raise _OwnerUnverifiable from error
    match = re.fullmatch(r'\(uint32 ([1-9][0-9]*),\)', reply)
    if match is None:
        raise _OwnerUnverifiable
    return int(match.group(1))


def _installed_origin_for_pid(pid: int, *, proc_root: Path = Path('/proc')) -> bool | None:
    """Return installed/wrong/unknown launch path without disclosing argv."""

    try:
        with (proc_root / str(pid) / 'cmdline').open('rb') as command_line:
            private_command = command_line.read(131073)
    except OSError:
        return None
    if len(private_command) > 131072:
        return None
    arguments = private_command.rstrip(b'\0').split(b'\0')
    if not arguments or not posixpath.basename(arguments[0]).startswith(b'python3'):
        return None

    script = None
    options_ended = False
    for argument in arguments[1:]:
        if argument == b'--' and not options_ended:
            options_ended = True
            continue
        if not options_ended and argument in (b'-I', b'-B', b'-IB', b'-BI'):
            continue
        if not options_ended and (
            argument in (b'-c', b'-m') or argument.startswith((b'-c', b'-m'))
        ):
            return None
        if not options_ended and argument.startswith(b'-'):
            return None
        script = argument
        break
    if script is None or not script.startswith(b'/'):
        return None

    installed_script = os.fsencode(SYSTEM_ROOT / 'bin/kukni')
    normalized_script = posixpath.normpath(script)
    if normalized_script == installed_script:
        return True
    # Both the old per-user installer and source checkout execute a bin/kukni
    # script through Python. Inspect only the interpreter's launch-script slot,
    # never later file arguments that Kukni was asked to preview.
    if normalized_script.endswith(b'/bin/kukni'):
        return False
    return None


def running_owner_check(
    *, project_root: Path | None = None,
    owner_pid: Callable[[str], int | None] = _session_name_pid,
    proc_root: Path = Path('/proc'),
) -> Check:
    """Inspect an already-running owner without activating Kukni."""

    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]
    if project_root != SYSTEM_ROOT:
        return Check(
            'Running activation owner', True,
            'Source-checkout runtime only; no installed process-origin claim was made.',
            required=False, kind='source',
        )

    pids: set[int] = set()
    try:
        for name in DBUS_NAMES:
            pid = owner_pid(name)
            if pid is not None:
                pids.add(pid)
    except _BusUnavailable:
        if pids:
            return Check(
                'Running activation owner', False,
                'A running owner was found, but the complete D-Bus owner check became unavailable.',
                kind='warning',
            )
        return Check(
            'Running activation owner', True,
            'The session D-Bus is unavailable; no running owner origin was checked.',
            kind='unavailable',
        )
    except _OwnerUnverifiable:
        return Check(
            'Running activation owner', False,
            'A running Kukni activation owner exists, but its process origin could not be verified.',
            kind='warning',
        )

    if not pids:
        return Check(
            'Running activation owner', True,
            'Neither Kukni D-Bus name currently has a running owner.',
            kind='not-running',
        )
    origins = [_installed_origin_for_pid(pid, proc_root=proc_root) for pid in pids]
    if any(origin is False for origin in origins):
        return Check(
            'Running activation owner', False,
            'A running Kukni D-Bus owner was launched from a path other than the packaged install. '
            'Close it before testing installed activation.',
            kind='warning',
        )
    if any(origin is None for origin in origins):
        return Check(
            'Running activation owner', False,
            'A running Kukni D-Bus owner exists, but its process origin could not be verified.',
            kind='warning',
        )
    return Check(
        'Running activation owner', True,
        'Every running Kukni D-Bus owner was launched from the packaged path. '
        'If the package was updated while Kukni was running, close it before '
        'testing activation.',
        kind='inspection',
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
    checks.append(system_integration_check())
    checks.append(running_owner_check())
    return checks


def main() -> int:
    try:
        checks = check_runtime()
    except (ImportError, ValueError):
        print('Kukni is missing its core GTK/Python runtime dependencies.')
        return 1
    for check in checks:
        if check.required and not check.ready:
            state = 'Failed'
        elif check.kind == 'warning':
            state = 'Warning'
        elif check.kind == 'prerequisite':
            state = 'Available' if check.ready else 'Unavailable'
        elif check.kind == 'unavailable':
            state = 'Unavailable'
        elif check.kind == 'not-running':
            state = 'Not running'
        elif check.kind == 'source':
            state = 'Source only'
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
