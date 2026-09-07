#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Prove a private D-Bus activation launches the packaged Kukni runtime."""

from pathlib import Path
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from gi.repository import Gio, GLib

from kukni.diagnostics import SYSTEM_ROOT, running_owner_check, sample_png
from kukni.nautilus_previewer import BUS_NAME, CURRENT_INTERFACE, OBJECT_PATH


SYSTEM_LAUNCHER = Path('/usr/bin/kukni')
SYSTEM_SERVICES = (
    Path('/usr/share/dbus-1/services/io.github.lamosty.Kukni.service'),
    Path('/usr/share/dbus-1/services/org.gnome.NautilusPreviewer.service'),
)
PRIVATE_CHILD_MARKER = 'KUKNI_INSTALLED_ACTIVATION_CHILD'
CALL_TIMEOUT_MILLISECONDS = 3000
CHILD_TIMEOUT_SECONDS = 25
STAGES = frozenset(('activation', 'origin', 'show', 'visible', 'close'))


def require_ui_wrapper() -> None:
    required = (
        ('KUKNI_ISOLATED_UI_TEST', '1'),
        ('GSETTINGS_BACKEND', 'memory'),
        ('GIO_USE_VFS', 'local'),
        ('GTK_A11Y', 'none'),
        ('GDK_BACKEND', 'x11'),
    )
    if not os.environ.get('DISPLAY') or not all(
        os.environ.get(name) == value for name, value in required
    ):
        raise SystemExit('Run this smoke with tests/run-ui.sh')


def call(
    connection: Gio.DBusConnection,
    destination: str,
    object_path: str,
    interface: str,
    method: str,
    parameters: GLib.Variant,
    reply_type: str,
    *,
    flags: Gio.DBusCallFlags = Gio.DBusCallFlags.NO_AUTO_START,
) -> GLib.Variant:
    return connection.call_sync(
        destination, object_path, interface, method, parameters,
        GLib.VariantType.new(reply_type), flags, CALL_TIMEOUT_MILLISECONDS, None,
    )


def visible(connection: Gio.DBusConnection) -> bool:
    reply = call(
        connection,
        BUS_NAME,
        OBJECT_PATH,
        'org.freedesktop.DBus.Properties',
        'Get',
        GLib.Variant('(ss)', (CURRENT_INTERFACE, 'Visible')),
        '(v)',
    )
    return reply.get_child_value(0).get_variant().get_boolean()


def private_child(sample: Path) -> int:
    if os.environ.get(PRIVATE_CHILD_MARKER) != '1':
        raise AssertionError('private child marker is missing')

    address = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    expected_socket = sample.parent / 'session-bus'
    expected_address = f'unix:path={expected_socket}'
    if (
        not address
        or 'autolaunch:' in address
        or address.split(',guid=', 1)[0] != expected_address
    ):
        raise AssertionError('dedicated private bus address is unavailable')
    connection = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )
    try:
        (sample.parent / 'stage').write_text('activation')
        activation = call(
            connection,
            'org.freedesktop.DBus',
            '/org/freedesktop/DBus',
            'org.freedesktop.DBus',
            'StartServiceByName',
            GLib.Variant('(su)', (BUS_NAME, 0)),
            '(u)',
            flags=Gio.DBusCallFlags.NONE,
        ).unpack()[0]
        if activation not in (1, 2):
            raise AssertionError('private bus did not activate the previewer service')

        (sample.parent / 'stage').write_text('origin')
        origin = running_owner_check(project_root=SYSTEM_ROOT)
        if not origin.ready or origin.kind != 'inspection':
            raise AssertionError('activated owner did not have the packaged launch path')

        (sample.parent / 'stage').write_text('show')
        call(
            connection,
            BUS_NAME,
            OBJECT_PATH,
            CURRENT_INTERFACE,
            'ShowFile',
            GLib.Variant('(ssb)', (sample.as_uri(), '', False)),
            '()',
        )
        (sample.parent / 'stage').write_text('visible')
        deadline = time.monotonic() + 5
        while not visible(connection):
            if time.monotonic() >= deadline:
                raise AssertionError('installed preview window did not become visible')
            time.sleep(0.05)

        # Visible proves the installed D-Bus/UI path handled ShowFile. Renderer
        # pixels are intentionally covered by the separate installed --check
        # and real GTK image tests, not inferred here.
        (sample.parent / 'stage').write_text('close')
        call(
            connection,
            BUS_NAME,
            OBJECT_PATH,
            CURRENT_INTERFACE,
            'Close',
            GLib.Variant('()', ()),
            '()',
        )
    finally:
        connection.close_sync(None)
    return 0


def stop_owned_process_group(process: subprocess.Popen[bytes]) -> None:
    # @constraint The nested bus and everything it activates are placed in a
    # fresh process group. Terminate only that owned group so timeout/failure
    # cannot leave an installed preview process behind.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def run_private_session(config: Path, sample: Path) -> None:
    environment = os.environ.copy()
    environment[PRIVATE_CHILD_MARKER] = '1'
    process = subprocess.Popen(
        [
            '/usr/bin/dbus-run-session', f'--config-file={config}', '--',
            sys.executable, str(Path(__file__).resolve()), '--private-child', str(sample),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        status = process.wait(timeout=CHILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        status = None
    finally:
        stop_owned_process_group(process)
    with (sample.parent / 'stage').open() as stage_file:
        stage = stage_file.read(32).strip()
    safe_stage = stage if stage in STAGES else 'activation'
    if timed_out:
        raise AssertionError(
            f'private activation smoke exceeded its deadline at {safe_stage} stage'
        )
    if status != 0:
        raise AssertionError(f'private activation smoke failed at {safe_stage} stage')


def parent() -> int:
    require_ui_wrapper()
    if not SYSTEM_LAUNCHER.is_file() or not os.access(SYSTEM_LAUNCHER, os.X_OK):
        raise AssertionError('packaged /usr/bin/kukni is not installed')
    if not all(service.is_file() for service in SYSTEM_SERVICES):
        raise AssertionError('packaged D-Bus service files are not installed')

    with tempfile.TemporaryDirectory(
        prefix='kukni-installed-activation-', dir='/tmp',
    ) as temporary:
        root = Path(temporary)
        service_directory = root / 'services'
        service_directory.mkdir()
        for service in SYSTEM_SERVICES:
            shutil.copyfile(service, service_directory / service.name)
        if {path.name for path in service_directory.iterdir()} != {
            service.name for service in SYSTEM_SERVICES
        }:
            raise AssertionError('private activation directory contains unexpected files')

        config = root / 'session.conf'
        bus_socket = root / 'session-bus'
        config.write_text(
            '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"\n'
            ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
            '<busconfig>\n'
            '  <type>session</type>\n'
            f'  <listen>unix:path={escape(str(bus_socket))}</listen>\n'
            f'  <servicedir>{escape(str(service_directory))}</servicedir>\n'
            '  <policy context="default">\n'
            '    <allow own="*"/>\n'
            '    <allow send_destination="*"/>\n'
            '    <allow receive_sender="*"/>\n'
            '  </policy>\n'
            '</busconfig>\n'
        )
        sample = root / 'sample.png'
        sample.write_bytes(sample_png())
        (root / 'stage').write_text('activation')
        run_private_session(config, sample)

    print('Installed D-Bus activation smoke test passed')
    return 0


def main() -> int:
    if sys.argv[1:2] == ['--private-child']:
        if len(sys.argv) != 3:
            raise SystemExit('private child requires a sample')
        return private_child(Path(sys.argv[2]))
    if len(sys.argv) != 1:
        raise SystemExit('unexpected arguments')
    return parent()


if __name__ == '__main__':
    raise SystemExit(main())
