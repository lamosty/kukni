# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Kukni GTK application entry point."""

from __future__ import annotations

from pathlib import Path
import sys

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .nautilus_previewer import NautilusPreviewerService
from .session import Direction
from .window import PreviewWindow


APPLICATION_ID = "io.github.lamosty.Kukni"


class KukniApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self._window: PreviewWindow | None = None
        self._previewer = NautilusPreviewerService(
            self._show_file_from_previewer,
            self._close_from_previewer,
            self._on_previewer_session_changed,
        )

    def do_dbus_register(
        self,
        connection: Gio.DBusConnection,
        object_path: str,
    ) -> bool:
        if not Adw.Application.do_dbus_register(self, connection, object_path):
            return False
        try:
            self._previewer.register(connection)
        except GLib.Error as error:
            print(f"Kukni previewer integration unavailable: {error.message}", file=sys.stderr)
        return True

    def do_dbus_unregister(
        self,
        connection: Gio.DBusConnection,
        object_path: str,
    ) -> None:
        self._previewer.unregister()
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name("Kukni")
        self._load_styles()

    def do_activate(self) -> None:
        self._previewer.detach_session()
        self._ensure_window().show_empty()

    def do_open(
        self,
        files: list[Gio.File],
        _number_of_files: int,
        _hint: str,
    ) -> None:
        self._previewer.detach_session()
        window = self._ensure_window()
        if files:
            window.show_file(files[0])
        else:
            window.show_empty()

    def _ensure_window(self) -> PreviewWindow:
        if self._window is None:
            self._window = PreviewWindow(self)
            self._window.connect("destroy", self._on_window_destroyed)
            self._window.connect("navigation-requested", self._on_navigation_requested)
            self._window.connect(
                "file-chosen", lambda *_args: self._previewer.detach_session()
            )
            self._window.connect(
                "notify::visible",
                lambda window, _parameter: self._previewer.set_visible(
                    window.get_visible()
                ),
            )
            self._on_previewer_session_changed()
        return self._window

    def _on_previewer_session_changed(self) -> None:
        if self._window is not None:
            self._window.set_external_parent_handle(self._previewer.parent_handle)
            self._window.set_navigation_available(self._previewer.navigation_available)

    def _on_window_destroyed(self, window: PreviewWindow) -> None:
        if self._window is window:
            self._window = None
        self._previewer.set_visible(False)

    def _on_navigation_requested(
        self,
        window: PreviewWindow,
        direction: str,
    ) -> None:
        if not self._previewer.emit_selection(Direction(direction)):
            window.set_navigation_available(False)

    def _show_file_from_previewer(
        self,
        uri: str,
        parent_handle: str,
        close_if_already_shown: bool,
    ) -> None:
        window = self._ensure_window()
        window.set_external_parent_handle(parent_handle)
        window.show_file(
            Gio.File.new_for_uri(uri),
            close_if_already_shown=close_if_already_shown,
        )

    def _close_from_previewer(self) -> None:
        if self._window is not None:
            self._window.close()

    @staticmethod
    def _load_styles() -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )


def main(argv: list[str] | None = None) -> int:
    application = KukniApplication()
    try:
        return application.run(argv if argv is not None else sys.argv)
    except KeyboardInterrupt:
        return 130
