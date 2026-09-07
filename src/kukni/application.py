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
DEVELOPMENT_APPLICATION_ID = "io.github.lamosty.Kukni.Devel"
DEVELOPMENT_APPLICATION_NAME = "Kukni (Development)"


class KukniApplication(Adw.Application):
    def __init__(self, *, development: bool = False) -> None:
        self.development = bool(development)
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if self.development:
            # @decision A development process always handles its own arguments.
            # NON_UNIQUE prevents forwarding into either an obsolete source
            # process or the installed production application.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(
            application_id=(
                DEVELOPMENT_APPLICATION_ID if self.development else APPLICATION_ID
            ),
            flags=flags,
        )
        self._window: PreviewWindow | None = None
        # @constraint Development is only a local, standalone preview. Do not
        # construct the service, export its objects, request its well-known
        # name, or react to a live file-manager session in this mode.
        self._previewer = (
            None
            if self.development
            else NautilusPreviewerService(
                self._show_file_from_previewer,
                self._close_from_previewer,
                self._on_previewer_session_changed,
            )
        )

    def do_dbus_register(
        self,
        connection: Gio.DBusConnection,
        object_path: str,
    ) -> bool:
        if not Adw.Application.do_dbus_register(self, connection, object_path):
            return False
        if self._previewer is not None:
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
        if self._previewer is not None:
            self._previewer.unregister()
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(
            DEVELOPMENT_APPLICATION_NAME if self.development else "Kukni"
        )
        self._load_styles()

    def do_activate(self) -> None:
        if self._previewer is not None:
            self._previewer.detach_session()
        self._ensure_window().show_empty()

    def do_open(
        self,
        files: list[Gio.File],
        _number_of_files: int,
        _hint: str,
    ) -> None:
        if self._previewer is not None:
            self._previewer.detach_session()
        window = self._ensure_window()
        if files:
            window.show_file(files[0])
        else:
            window.show_empty()

    def _ensure_window(self) -> PreviewWindow:
        if self._window is None:
            self._window = PreviewWindow(self, development=self.development)
            if self.development:
                self._window.set_title(DEVELOPMENT_APPLICATION_NAME)
            self._window.connect("destroy", self._on_window_destroyed)
            self._window.connect("navigation-requested", self._on_navigation_requested)
            if self._previewer is not None:
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
        if self._window is not None and self._previewer is not None:
            self._window.set_external_parent_handle(self._previewer.parent_handle)
            self._window.set_navigation_available(self._previewer.navigation_available)

    def _on_window_destroyed(self, window: PreviewWindow) -> None:
        if self._window is window:
            self._window = None
        if self._previewer is not None:
            self._previewer.set_visible(False)

    def _on_navigation_requested(
        self,
        window: PreviewWindow,
        direction: str,
    ) -> None:
        if self._previewer is None or not self._previewer.emit_selection(Direction(direction)):
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


def main(argv: list[str] | None = None, *, development: bool = False) -> int:
    application = KukniApplication(development=development)
    try:
        return application.run(argv if argv is not None else sys.argv)
    except KeyboardInterrupt:
        return 130
