# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the private Nautilus wire contract on a new, isolated test bus."""

from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gi.repository import Gio, GLib

from kukni.nautilus_previewer import (
    BUS_NAME, CURRENT_INTERFACE, LEGACY_INTERFACE, OBJECT_PATH,
    NautilusPreviewerService,
)
from kukni.session import Direction


def spin_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("private D-Bus operation timed out")
        context.iteration(False)
        time.sleep(0.001)


class NavigationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Never register a previewer on the developer's desktop bus, even when
        # this test is launched outside tests/run-ui.sh.
        cls.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        cls.bus.up()

    @classmethod
    def tearDownClass(cls):
        # All explicit connections are closed in tearDown. Avoid down()
        # waiting on GTK's process-global singleton bus reference.
        cls.bus.stop()
        Gio.TestDBus.unset()

    def setUp(self):
        self.connections = []
        self.server = self.new_connection()
        self.client = self.new_connection()
        self.other = self.new_connection()
        self.shown = []
        self.closed = []
        self.changes = []
        self.service = NautilusPreviewerService(
            lambda *args: self.shown.append(args),
            lambda: self.closed.append(True),
            lambda: self.changes.append(self.service.navigation_available),
        )
        self.service.register(self.server)
        spin_until(lambda: self.service.owns_name)

    def tearDown(self):
        self.service.unregister()
        for connection in self.connections:
            if not connection.is_closed():
                connection.close_sync(None)
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def new_connection(self):
        connection = Gio.DBusConnection.new_for_address_sync(
            self.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        self.connections.append(connection)
        return connection

    def call(self, method, parameters=None, *, client=None, interface=CURRENT_INTERFACE):
        results = []

        def finished(connection, result):
            try:
                results.append(connection.call_finish(result))
            except GLib.Error as error:
                results.append(error)

        (client or self.client).call(
            BUS_NAME, OBJECT_PATH, interface, method, parameters,
            None, Gio.DBusCallFlags.NO_AUTO_START, 2000, None, finished,
        )
        spin_until(lambda: results)
        if isinstance(results[0], GLib.Error):
            raise results[0]
        return results[0]

    def show(self, handle="wayland:window-one", *, client=None, toggle=False):
        self.call("ShowFile", GLib.Variant("(ssb)", ("file:///sample.txt", handle, toggle)), client=client)

    def test_attachment_unicast_wire_and_explicit_detachment(self):
        self.assertFalse(self.service.emit_selection(Direction.RIGHT))
        received = []
        unrelated = []
        for client, target in ((self.client, received), (self.other, unrelated)):
            client.signal_subscribe(
                BUS_NAME, CURRENT_INTERFACE, "SelectionEvent", OBJECT_PATH,
                None, Gio.DBusSignalFlags.NONE,
                lambda _c, _s, _p, _i, _n, value, target=target: target.append(value),
            )
            client.flush_sync(None)
        self.show()
        self.assertTrue(self.service.navigation_available)
        self.assertTrue(self.service.emit_selection(Direction.RIGHT))
        spin_until(lambda: received)
        self.assertEqual(received[0].get_type_string(), "(u)")
        self.assertEqual(received[0].unpack(), (5,))
        self.call("Ping", interface="org.freedesktop.DBus.Peer")  # ordered round trip
        self.assertFalse(unrelated)
        self.service.detach_session()
        self.assertFalse(self.service.emit_selection(Direction.RIGHT))
        parent = self.call("Get", GLib.Variant("(ss)", (CURRENT_INTERFACE, "ParentHandle")), interface="org.freedesktop.DBus.Properties")
        self.assertEqual(parent.unpack(), ("",))
        self.call("Close")
        self.assertFalse(self.closed, "detached caller closed standalone content")

    def test_window_and_caller_replacement_do_not_toggle_or_follow_stale_close(self):
        self.show(toggle=True)
        self.assertFalse(self.shown[-1][2])
        self.show(toggle=True)
        self.assertTrue(self.shown[-1][2])
        self.show("wayland:window-two", toggle=True)
        self.assertFalse(self.shown[-1][2])
        self.show("wayland:window-two", client=self.other, toggle=True)
        self.assertFalse(self.shown[-1][2])
        self.client.close_sync(None)
        self.call("Ping", interface="org.freedesktop.DBus.Peer", client=self.other)
        self.assertTrue(self.service.navigation_available)
        self.call("Close", client=self.other)
        self.assertEqual(self.closed, [True])
        self.assertFalse(self.service.navigation_available)

    def test_stale_caller_close_is_ignored(self):
        self.show()
        self.show(client=self.other)
        self.call("Close")
        self.assertFalse(self.closed)
        self.assertTrue(self.service.navigation_available)

    def test_source_disappearance_detaches_without_closing_the_preview(self):
        self.show()
        self.client.close_sync(None)
        spin_until(lambda: not self.service.navigation_available)
        self.assertEqual(self.service.parent_handle, "")
        self.assertFalse(self.closed)
        self.assertFalse(self.service.emit_selection(Direction.LEFT))

    def test_hide_and_name_loss_detach(self):
        self.show()
        self.service.set_visible(False)
        self.assertFalse(self.service.navigation_available)
        self.show()
        # Closing the real service connection drives Gio's name-lost callback.
        self.server.close_sync(None)
        spin_until(lambda: not self.service.owns_name)
        self.assertFalse(self.service.navigation_available)
        self.assertEqual(self.service.parent_handle, "")

    def test_legacy_x11_and_parentless_current_requests_preview_without_navigation(self):
        self.call("ShowFile", GLib.Variant("(sib)", ("file:///legacy.txt", 12345, False)), interface=LEGACY_INTERFACE)
        self.assertEqual(self.shown[-1], ("file:///legacy.txt", "x11:12345", False))
        self.assertFalse(self.service.navigation_available)
        self.call("Close", interface=LEGACY_INTERFACE)
        self.assertEqual(self.closed, [True])
        self.show("")
        self.assertFalse(self.service.navigation_available)
        self.show("x" * 4097)
        self.assertEqual(self.service.parent_handle, "")
        self.assertFalse(self.service.navigation_available)


if __name__ == "__main__":
    unittest.main()
