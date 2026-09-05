#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Run via tests/run-ui.sh: actual application ownership and chooser handoff."""

from pathlib import Path
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gi.repository import Gio, GLib

from kukni.application import KukniApplication
from kukni.nautilus_previewer import BUS_NAME, CURRENT_INTERFACE, OBJECT_PATH
from kukni.session import Direction
from test_navigation_integration import spin_until


def main():
    # Like the other UI smokes, require the isolated display/bus wrapper. Do
    # not accidentally register our previewer on the developer's desktop.
    if not all(os.environ.get(key) == value for key, value in (
        ("GSETTINGS_BACKEND", "memory"), ("GIO_USE_VFS", "local"),
        ("GTK_A11Y", "none"), ("GDK_BACKEND", "x11"),
    )):
        raise SystemExit("Run this smoke with tests/run-ui.sh")
    failures = []
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory) / "sample.txt"
        sample.write_text("A local preview file.\n")
        chosen = Path(directory) / "chosen.txt"
        chosen.write_text("A standalone chooser selection.\n")
        app = KukniApplication()

        def verify():
            client = None
            try:
                spin_until(lambda: app._previewer.owns_name)
                client = Gio.DBusConnection.new_for_address_sync(
                    Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None,
                )

                def call(method, parameters=None):
                    results = []

                    def finished(connection, result):
                        try:
                            results.append(connection.call_finish(result))
                        except Exception as error:
                            results.append(error)

                    client.call(BUS_NAME, OBJECT_PATH, CURRENT_INTERFACE, method,
                                parameters, None, Gio.DBusCallFlags.NO_AUTO_START,
                                2000, None, finished)
                    spin_until(lambda: results)
                    if isinstance(results[0], Exception):
                        raise results[0]

                def show(handle="x11:12345", toggle=False):
                    call("ShowFile", GLib.Variant("(ssb)", (sample.as_uri(), handle, toggle)))

                def assert_navigation(expected):
                    assert app._previewer.navigation_available is expected
                    for direction in Direction:
                        assert app._window.lookup_action(f"navigate-{direction.value}").get_enabled() is expected

                original = app._window
                assert_navigation(False)
                show()
                assert_navigation(True)
                assert app._window is original
                show("x11:67890", toggle=True)
                assert app._window is original and original.get_visible()
                assert original._external_parent_handle == "x11:67890"

                app.do_open([Gio.File.new_for_path(str(chosen))], 1, "")
                assert_navigation(False)
                assert original._external_parent_handle == ""
                assert app._previewer.parent_handle == ""
                call("Close")
                assert original.get_visible(), "stale manager closed direct-open preview"

                show()
                assert_navigation(True)
                chooser = mock.Mock()
                chooser.open_finish.return_value = Gio.File.new_for_path(str(chosen))
                original._on_file_chosen(chooser, None)
                assert_navigation(False)
                assert original.session.snapshot.current_uri == chosen.as_uri()
                assert original._external_parent_handle == ""

                show()
                app.do_activate()
                assert_navigation(False)
                assert original._external_parent_handle == ""
                assert original._stack.get_visible_child_name() == "empty"
                assert not original.session.is_open

                show()
                client.close_sync(None)
                spin_until(lambda: not app._previewer.navigation_available)
                assert_navigation(False)
                assert original.get_visible(), "source loss should retain the preview"
                assert original._external_parent_handle == ""
            except Exception as error:
                import traceback
                traceback.print_exc()
                failures.append(str(error))
            finally:
                if client is not None and not client.is_closed():
                    client.close_sync(None)
                if app._window is not None:
                    app._window.close()
                app.quit()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(verify)
        status = app.run(["kukni-navigation-smoke", str(sample)])
    if failures or status:
        return 1
    print("GTK navigation ownership smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
