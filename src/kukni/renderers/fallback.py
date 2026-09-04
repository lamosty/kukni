# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Universal metadata plus bounded text/hex fallback renderer."""

from __future__ import annotations

import string

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango


TEXT_SAMPLE_BYTES = 64 * 1024
HEX_SAMPLE_BYTES = 4 * 1024
HEX_WIDTH = 16


def is_probably_text(data: bytes, content_type: str | None) -> bool:
    if content_type and Gio.content_type_is_a(content_type, "text/plain"):
        return True
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def format_hex_sample(data: bytes, limit: int = HEX_SAMPLE_BYTES) -> str:
    lines: list[str] = []
    printable = frozenset(string.printable.encode("ascii"))
    for offset in range(0, min(len(data), limit), HEX_WIDTH):
        chunk = data[offset : offset + HEX_WIDTH]
        hexadecimal = " ".join(f"{byte:02x}" for byte in chunk)
        hexadecimal = f"{hexadecimal:<{HEX_WIDTH * 3 - 1}}"
        characters = "".join(
            chr(byte) if byte in printable and byte not in b"\r\n\t\x0b\x0c" else "."
            for byte in chunk
        )
        lines.append(f"{offset:08x}  {hexadecimal}  |{characters}|")
    return "\n".join(lines)


def format_modified(info: Gio.FileInfo) -> str:
    modified = info.get_modification_date_time()
    if modified is None:
        return "Unknown"
    return modified.to_local().format("%Y-%m-%d %H:%M:%S") or "Unknown"


class FallbackView(Gtk.Box):
    """Always-useful preview content for files without a rich renderer."""

    def __init__(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
    ) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=36,
            margin_bottom=32,
            margin_start=48,
            margin_end=48,
        )
        self.add_css_class("fallback-view")
        self._file = file
        self._info = info
        self._cancellable = cancellable
        self._stream: Gio.InputStream | None = None

        identity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        identity.set_halign(Gtk.Align.CENTER)

        icon = Gtk.Image(gicon=info.get_icon(), pixel_size=72)
        icon.add_css_class("fallback-icon")
        identity.append(icon)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        labels.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=info.get_display_name(), xalign=0)
        name.add_css_class("title-2")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        labels.append(name)

        content_type = info.get_content_type()
        description = (
            Gio.content_type_get_description(content_type) if content_type else None
        )
        kind = Gtk.Label(label=description or "Unknown file type", xalign=0)
        kind.add_css_class("dim-label")
        labels.append(kind)
        identity.append(labels)
        self.append(identity)

        metadata = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        metadata.set_halign(Gtk.Align.CENTER)
        metadata.append(self._metadata_item("Size", GLib.format_size(info.get_size())))
        metadata.append(self._metadata_item("Modified", format_modified(info)))
        metadata.append(
            self._metadata_item("MIME type", info.get_content_type() or "Unknown")
        )
        self.append(metadata)

        self._sample_title = Gtk.Label(label="Inspecting file…", xalign=0)
        self._sample_title.add_css_class("heading")
        self.append(self._sample_title)

        scroller = Gtk.ScrolledWindow(
            hexpand=True,
            vexpand=True,
            has_frame=True,
            min_content_height=220,
        )
        scroller.add_css_class("fallback-sample")
        self._text = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.NONE,
            top_margin=16,
            bottom_margin=16,
            left_margin=18,
            right_margin=18,
        )
        self._text.set_accessible_role(Gtk.AccessibleRole.GROUP)
        scroller.set_child(self._text)
        self.append(scroller)

        if info.get_file_type() == Gio.FileType.REGULAR:
            self._load_sample()
        else:
            self._show_message("Content sample unavailable for this file type")

    @staticmethod
    def _metadata_item(label: str, value: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(140, -1)
        value_label = Gtk.Label(
            label=value,
            ellipsize=Pango.EllipsizeMode.END,
        )
        value_label.add_css_class("caption-heading")
        value_label.set_tooltip_text(value)
        box.append(value_label)
        key_label = Gtk.Label(label=label)
        key_label.add_css_class("caption")
        key_label.add_css_class("dim-label")
        box.append(key_label)
        return box

    def _load_sample(self) -> None:
        self._file.read_async(
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            self._on_file_opened,
        )

    def _on_file_opened(self, file: Gio.File, result: Gio.AsyncResult) -> None:
        try:
            self._stream = file.read_finish(result)
            self._stream.read_bytes_async(
                TEXT_SAMPLE_BYTES,
                GLib.PRIORITY_DEFAULT,
                self._cancellable,
                self._on_sample_read,
            )
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_message("Content sample could not be read")

    def _on_sample_read(
        self,
        stream: Gio.InputStream,
        result: Gio.AsyncResult,
    ) -> None:
        try:
            sample = bytes(stream.read_bytes_finish(result).get_data())
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_message("Content sample could not be read")
            return
        finally:
            self._close_stream()

        if not sample:
            self._show_message("This file is empty")
        elif is_probably_text(sample, self._info.get_content_type()):
            self._sample_title.set_label("Text sample · first 64 KiB")
            self._text.get_buffer().set_text(sample.decode("utf-8", errors="replace"))
        else:
            self._sample_title.set_label("Hex sample · first 4 KiB")
            self._text.get_buffer().set_text(format_hex_sample(sample))

    def _show_message(self, message: str) -> None:
        self._sample_title.set_label("Universal preview")
        self._text.get_buffer().set_text(message)

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.close(None)
        except GLib.Error:
            pass
