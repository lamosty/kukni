# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Compatibility implementation of Nautilus' previewer D-Bus contract."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from .session import Direction


BUS_NAME = "org.gnome.NautilusPreviewer"
OBJECT_PATH = "/org/gnome/NautilusPreviewer"
LEGACY_INTERFACE = "org.gnome.NautilusPreviewer"
CURRENT_INTERFACE = "org.gnome.NautilusPreviewer2"

INTERFACE_XML = """
<node>
  <interface name="org.gnome.NautilusPreviewer">
    <method name="ShowFile">
      <arg type="s" direction="in" name="uri"/>
      <arg type="i" direction="in" name="xid"/>
      <arg type="b" direction="in" name="closeIfAlreadyShown"/>
    </method>
    <method name="Close"/>
  </interface>
  <interface name="org.gnome.NautilusPreviewer2">
    <method name="ShowFile">
      <arg type="s" direction="in" name="uri"/>
      <arg type="s" direction="in" name="windowHandle"/>
      <arg type="b" direction="in" name="closeIfAlreadyShown"/>
    </method>
    <method name="Close"/>
    <property name="ParentHandle" type="s" access="read"/>
    <property name="Visible" type="b" access="read"/>
    <signal name="SelectionEvent">
      <arg type="u" name="direction"/>
    </signal>
  </interface>
</node>
"""

DIRECTION_VALUES = {
    Direction.LEFT: int(Gtk.DirectionType.LEFT),
    Direction.RIGHT: int(Gtk.DirectionType.RIGHT),
    Direction.UP: int(Gtk.DirectionType.UP),
    Direction.DOWN: int(Gtk.DirectionType.DOWN),
}


class NautilusPreviewerService:
    """Exports preview methods while Kukni retains its own application ID."""

    def __init__(
        self,
        show_file: Callable[[str, str, bool], None],
        close: Callable[[], None],
        session_changed: Callable[[], None] | None = None,
    ) -> None:
        self._show_file = show_file
        self._close = close
        self._session_changed = session_changed or (lambda: None)
        self._connection: Gio.DBusConnection | None = None
        self._registration_ids: list[int] = []
        self._owner_id = 0
        self._owns_name = False
        self._parent_handle = ""
        self._visible = False
        self._session_sender: str | None = None
        self._session_interface: str | None = None
        self._sender_watch_id = 0
        self._attachment_generation = 0
        self._node_info = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)

    @property
    def owns_name(self) -> bool:
        return self._owns_name

    @property
    def parent_handle(self) -> str:
        return self._parent_handle

    @property
    def navigation_available(self) -> bool:
        # Owning the previewer bus name does not imply a file-manager session.
        # Only Previewer2 defines SelectionEvent; a legacy X11 caller can still
        # preview and close, but has not negotiated this navigation contract.
        return bool(
            self._connection is not None
            and not self._connection.is_closed()
            and self._owns_name
            and self._session_sender
            and self._session_interface == CURRENT_INTERFACE
            and self._parent_handle
        )

    def register(self, connection: Gio.DBusConnection) -> None:
        if self._connection is not None:
            return
        self._connection = connection

        for interface_name in (LEGACY_INTERFACE, CURRENT_INTERFACE):
            interface_info = self._node_info.lookup_interface(interface_name)
            registration_id = connection.register_object(
                OBJECT_PATH,
                interface_info,
                self._on_method_call,
                self._on_get_property if interface_name == CURRENT_INTERFACE else None,
                None,
            )
            self._registration_ids.append(registration_id)

        # Never steal or queue behind another previewer. Once Kukni's user-level
        # activation file is installed, this name should be free at startup.
        self._owner_id = Gio.bus_own_name_on_connection(
            connection,
            BUS_NAME,
            Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
            self._on_name_acquired,
            self._on_name_lost,
        )

    def unregister(self) -> None:
        self.detach_session()
        connection = self._connection
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        if connection is not None:
            for registration_id in self._registration_ids:
                connection.unregister_object(registration_id)
        self._registration_ids.clear()
        self._connection = None
        self._owns_name = False

    def detach_session(self) -> None:
        """Keep the preview, but release all file-manager ownership."""
        self._attachment_generation += 1
        if self._sender_watch_id:
            Gio.bus_unwatch_name(self._sender_watch_id)
            self._sender_watch_id = 0
        self._session_sender = None
        self._session_interface = None
        self.set_parent_handle("")
        self._session_changed()

    def _attach_session(self, sender: str, interface: str, handle: str) -> None:
        self._attachment_generation += 1
        generation = self._attachment_generation
        if self._sender_watch_id:
            Gio.bus_unwatch_name(self._sender_watch_id)
        self._session_sender = sender
        self._session_interface = interface
        self.set_parent_handle(handle)

        def sender_vanished(_connection, _name) -> None:
            # A queued callback from a replaced window/client must not detach
            # the new association, even when both requests share a bus sender.
            if generation == self._attachment_generation:
                self.detach_session()

        self._sender_watch_id = Gio.bus_watch_name_on_connection(
            self._connection,
            sender,
            Gio.BusNameWatcherFlags.NONE,
            None,
            sender_vanished,
        )
        self._session_changed()

    def set_parent_handle(self, handle: str) -> None:
        if handle == self._parent_handle:
            return
        self._parent_handle = handle
        self._emit_property_changed("ParentHandle", GLib.Variant("s", handle))

    def set_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if not visible:
            self.detach_session()
        if visible == self._visible:
            return
        self._visible = visible
        self._emit_property_changed("Visible", GLib.Variant("b", visible))

    def emit_selection(self, direction: Direction) -> bool:
        if not self.navigation_available:
            return False
        try:
            return self._connection.emit_signal(
                # Only the current caller may navigate. Broadcasting also
                # reaches unrelated file-manager processes on the same bus.
                self._session_sender,
                OBJECT_PATH,
                CURRENT_INTERFACE,
                "SelectionEvent",
                # @constraint Nautilus reads this signal as `(u)`. Sushi 46's
                # historical XML sometimes says `(q)`, unlike its wire call.
                GLib.Variant("(u)", (DIRECTION_VALUES[direction],)),
            )
        except GLib.Error:
            self.detach_session()
            return False

    def _on_method_call(
        self,
        _connection: Gio.DBusConnection,
        sender: str,
        _object_path: str,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        try:
            if method_name == "ShowFile":
                values = parameters.unpack()
                uri = values[0]
                if interface_name == LEGACY_INTERFACE:
                    parent_handle = f"x11:{values[1]}"
                else:
                    parent_handle = values[1]
                if len(parent_handle) > 4096:
                    parent_handle = ""
                # A repeated file in another Nautilus window (or following a
                # standalone open) starts a new session, not a toggle-close.
                same_session = (
                    sender == self._session_sender
                    and interface_name == self._session_interface
                    and parent_handle == self._parent_handle
                )
                close_if_already_shown = values[2] and same_session
                self._attach_session(sender, interface_name, parent_handle)
                self._show_file(uri, parent_handle, close_if_already_shown)
            elif method_name == "Close":
                # A stale manager must not close a newer standalone preview or
                # a preview now owned by a different manager process.
                if sender == self._session_sender:
                    self.detach_session()
                    self._close()
            else:
                invocation.return_dbus_error(
                    "io.github.lamosty.Kukni.UnknownMethod",
                    f"Unknown previewer method: {method_name}",
                )
                return
            invocation.return_value(GLib.Variant("()", ()))
        except Exception as error:
            if method_name == "ShowFile":
                self.detach_session()
            invocation.return_dbus_error(
                "io.github.lamosty.Kukni.PreviewError",
                str(error),
            )

    def _on_get_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        _interface_name: str,
        property_name: str,
    ) -> GLib.Variant | None:
        if property_name == "ParentHandle":
            return GLib.Variant("s", self._parent_handle)
        if property_name == "Visible":
            return GLib.Variant("b", self._visible)
        return None

    def _emit_property_changed(self, name: str, value: GLib.Variant) -> None:
        if (
            self._connection is None
            or self._connection.is_closed()
            or not self._owns_name
        ):
            return
        self._connection.emit_signal(
            None,
            OBJECT_PATH,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant(
                "(sa{sv}as)",
                (CURRENT_INTERFACE, {name: value}, []),
            ),
        )

    def _on_name_acquired(
        self,
        _connection: Gio.DBusConnection,
        _name: str,
    ) -> None:
        self._owns_name = True
        self._session_changed()

    def _on_name_lost(
        self,
        _connection: Gio.DBusConnection,
        _name: str,
    ) -> None:
        self._owns_name = False
        self.detach_session()
