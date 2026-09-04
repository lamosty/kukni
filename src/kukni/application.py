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

from .window import PreviewWindow


APPLICATION_ID = "io.github.lamosty.Kukni"


class KukniApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self._window: PreviewWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name("Kukni")
        self._load_styles()

    def do_activate(self) -> None:
        self._ensure_window().show_empty()

    def do_open(
        self,
        files: list[Gio.File],
        _number_of_files: int,
        _hint: str,
    ) -> None:
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
        return self._window

    def _on_window_destroyed(self, window: PreviewWindow) -> None:
        if self._window is window:
            self._window = None

    def _on_navigation_requested(
        self,
        window: PreviewWindow,
        _direction: str,
    ) -> None:
        window.show_toast("File-manager navigation is not connected yet")

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
