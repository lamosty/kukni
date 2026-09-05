# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Calm, metadata-only states when a file cannot be previewed."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from .text import sanitize_display_label


def unavailable_message(info: Gio.FileInfo) -> str:
    """Explain the missing preview without exposing file bytes or internals."""

    if info.get_file_type() == Gio.FileType.DIRECTORY:
        return "Folder previews aren't available yet."
    if info.get_file_type() != Gio.FileType.REGULAR:
        return "This item doesn't have a preview."
    if info.get_size() == 0:
        return "This file is empty."
    return "A preview isn't available for this file type yet."


class FallbackView(Gtk.Box):
    """An unavailable preview is not a byte inspector or a second file manager."""

    def __init__(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        *,
        detail: str = "",
    ) -> None:
        # @decision Never open the selected file in the fallback. Text belongs
        # to the text renderer; binary data and document source are not useful
        # substitutes for a picture or page. The title bar already names it.
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=32,
            margin_bottom=32,
            margin_start=32,
            margin_end=32,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self.add_css_class("fallback-view")
        self.append(Gtk.Image(gicon=info.get_icon(), pixel_size=64))
        heading = Gtk.Label(label="Preview unavailable")
        heading.add_css_class("title-2")
        self.append(heading)
        message = Gtk.Label(
            label=unavailable_message(info),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=44,
        )
        self.append(message)

        content_type = info.get_content_type()
        kind = Gio.content_type_get_description(content_type) if content_type else None
        summary = kind or "File"
        if info.get_file_type() == Gio.FileType.REGULAR:
            summary += f" · {GLib.format_size(info.get_size())}"
        caption = Gtk.Label(label=summary, wrap=True)
        caption.add_css_class("dim-label")
        caption.add_css_class("caption")
        self.append(caption)

        # Keep actionable failure information available without making decoder
        # diagnostics the main content or showing a toast on every selection.
        if detail:
            expander = Gtk.Expander(label="Details")
            diagnostic = Gtk.Label(
                label=sanitize_display_label(detail)[:512],
                wrap=True,
                selectable=True,
                max_width_chars=44,
                margin_top=8,
            )
            expander.set_child(diagnostic)
            self.append(expander)
