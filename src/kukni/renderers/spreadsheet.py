# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native GTK4 previews for bounded XLSX spreadsheet models.

Parsing happens in a cancellable daemon worker.  Only the inert model returned
by :func:`parse_xlsx` crosses onto GTK's main context; workbook formulas,
macros, relationships, and XML are never handed to another application or a
web rendering engine.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk, Pango

from .base import ErrorCallback, ReadyCallback
from .xlsx import (
    DEFAULT_LIMITS,
    XlsxPreview,
    XlsxPreviewCancelled,
    XlsxPreviewError,
    parse_xlsx,
    supports_xlsx,
)


CELL_COLUMN_WIDTH = 168
ROW_NUMBER_COLUMN_WIDTH = 64


class _SheetRow(GObject.Object):
    """One virtualized row consumed by ``Gtk.ColumnView`` factories."""

    def __init__(self, number: int, values: tuple[str, ...]) -> None:
        super().__init__()
        self.number = number
        self.values = values


def column_label(number: int) -> str:
    """Return the familiar spreadsheet label for a one-based column number."""

    if number < 1:
        raise ValueError("spreadsheet columns are one-based")
    characters: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        characters.append(chr(ord("A") + remainder))
    return "".join(reversed(characters))


def preview_subtitle(preview: XlsxPreview) -> str:
    """Build the compact description shown in Kukni's window header."""

    count = len(preview.rows)
    noun = "row" if count == 1 else "rows"
    limited = " · limited" if preview.truncated else ""
    return f"Spreadsheet · {count} visible {noun}{limited}"


def safety_notices(preview: XlsxPreview) -> tuple[str, ...]:
    """Explain which potentially active workbook features stayed inert."""

    notices = [
        "Read-only preview — formulas, macros, and external links never run."
    ]
    if preview.formula_cells:
        noun = "cell" if preview.formula_cells == 1 else "cells"
        notices.append(
            f"{preview.formula_cells} formula {noun}: showing cached values only."
        )
    if preview.external_relationships_ignored:
        count = preview.external_relationships_ignored
        noun = "link was" if count == 1 else "links were"
        notices.append(f"{count} external workbook {noun} ignored.")
    if preview.active_parts_ignored:
        count = preview.active_parts_ignored
        noun = "part was" if count == 1 else "parts were"
        notices.append(f"{count} embedded active-content {noun} ignored.")
    if preview.truncation_reasons:
        notices.append(
            "Preview limited by " + ", ".join(preview.truncation_reasons) + "."
        )
    return tuple(notices)


class XlsxPreviewView(Gtk.Box):
    """A parentless, virtualized spreadsheet view ready for Kukni's stack."""

    def __init__(self, preview: XlsxPreview) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
            hexpand=True,
            vexpand=True,
        )
        self.add_css_class("spreadsheet-view")
        self.preview = preview
        self.notice_labels: tuple[Gtk.Label, ...] = ()
        self.sheet_name_label: Gtk.Label
        self.column_view: Gtk.ColumnView | None = None
        self._store: Gio.ListStore | None = None
        self._selection: Gtk.NoSelection | None = None

        self.append(self._build_header())
        self.append(self._build_content())

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header.set_margin_top(16)
        header.set_margin_bottom(14)
        header.set_margin_start(18)
        header.set_margin_end(18)
        header.add_css_class("spreadsheet-header")

        icon = Gtk.Image(icon_name="x-office-spreadsheet-symbolic", pixel_size=32)
        icon.set_valign(Gtk.Align.START)
        header.append(icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text.set_hexpand(True)
        header.append(text)

        self.sheet_name_label = Gtk.Label(
            label=self.preview.sheet_name,
            xalign=0.0,
            ellipsize=Pango.EllipsizeMode.END,
            single_line_mode=True,
        )
        self.sheet_name_label.set_tooltip_text(self.preview.sheet_name)
        self.sheet_name_label.add_css_class("title-4")
        text.append(self.sheet_name_label)

        labels: list[Gtk.Label] = []
        for index, message in enumerate(safety_notices(self.preview)):
            label = Gtk.Label(
                label=message,
                xalign=0.0,
                wrap=True,
                selectable=False,
            )
            label.add_css_class("caption")
            if index == 0:
                label.add_css_class("dim-label")
            else:
                label.add_css_class("warning")
            text.append(label)
            labels.append(label)
        self.notice_labels = tuple(labels)
        return header

    def _build_content(self) -> Gtk.Widget:
        if not self.preview.rows:
            empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            empty.set_hexpand(True)
            empty.set_vexpand(True)
            empty.set_halign(Gtk.Align.CENTER)
            empty.set_valign(Gtk.Align.CENTER)
            icon = Gtk.Image(icon_name="view-grid-symbolic", pixel_size=48)
            icon.add_css_class("dim-label")
            empty.append(icon)
            label = Gtk.Label(label="This worksheet has no visible cell values.")
            label.add_css_class("dim-label")
            empty.append(label)
            return empty

        column_count = max(
            1,
            min(self.preview.column_count, DEFAULT_LIMITS.max_columns),
        )
        store = Gio.ListStore.new(_SheetRow)
        for row in self.preview.rows[: DEFAULT_LIMITS.max_rows]:
            values = [""] * column_count
            for cell in row.cells:
                if 1 <= cell.column <= column_count:
                    values[cell.column - 1] = cell.value
            store.append(_SheetRow(row.number, tuple(values)))

        selection = Gtk.NoSelection.new(store)
        view = Gtk.ColumnView.new(selection)
        view.set_hexpand(True)
        view.set_vexpand(True)
        view.set_show_column_separators(True)
        view.set_show_row_separators(True)
        view.set_single_click_activate(False)
        view.set_can_focus(False)
        view.set_focusable(False)
        view.add_css_class("spreadsheet-grid")

        row_factory = self._text_factory(None)
        row_column = Gtk.ColumnViewColumn.new("", row_factory)
        row_column.set_fixed_width(ROW_NUMBER_COLUMN_WIDTH)
        row_column.set_resizable(False)
        view.append_column(row_column)

        for column_index in range(column_count):
            factory = self._text_factory(column_index)
            column = Gtk.ColumnViewColumn.new(
                column_label(column_index + 1),
                factory,
            )
            column.set_fixed_width(CELL_COLUMN_WIDTH)
            column.set_resizable(True)
            view.append_column(column)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )
        scroller.set_can_focus(False)
        scroller.set_focusable(False)
        scroller.set_child(view)

        self._store = store
        self._selection = selection
        self.column_view = view
        return scroller

    @staticmethod
    def _text_factory(column_index: int | None) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, list_item: Gtk.ListItem) -> None:
            label = Gtk.Label(
                xalign=1.0 if column_index is None else 0.0,
                yalign=0.5,
                ellipsize=Pango.EllipsizeMode.END,
                single_line_mode=True,
            )
            label.set_margin_top(5)
            label.set_margin_bottom(5)
            label.set_margin_start(8)
            label.set_margin_end(8)
            if column_index is None:
                label.add_css_class("dim-label")
                label.add_css_class("spreadsheet-row-number")
                label.set_accessible_role(Gtk.AccessibleRole.ROW_HEADER)
            label.set_selectable(False)
            list_item.set_child(label)

        def bind(_factory, list_item: Gtk.ListItem) -> None:
            row = list_item.get_item()
            label = list_item.get_child()
            if not isinstance(row, _SheetRow) or not isinstance(label, Gtk.Label):
                return
            value = (
                str(row.number)
                if column_index is None
                else row.values[column_index]
            )
            label.set_label(value)
            label.set_tooltip_text(value or None)

        def unbind(_factory, list_item: Gtk.ListItem) -> None:
            label = list_item.get_child()
            if isinstance(label, Gtk.Label):
                label.set_label("")
                label.set_tooltip_text(None)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        factory.connect("unbind", unbind)
        return factory


class SpreadsheetRenderer:
    """Render regular local XLSX files without launching executable content."""

    id = "xlsx"

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        if not file.is_native() or info.get_file_type() != Gio.FileType.REGULAR:
            return False
        return supports_xlsx(file.get_basename(), info.get_content_type())

    def render(
        self,
        file: Gio.File,
        _info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        """Parse off-thread and settle once through GTK's default main context."""

        path = file.get_path() if file.is_native() else None
        if path is None:
            self._queue_error(
                cancellable,
                on_error,
                "Spreadsheet preview supports local files only",
            )
            return
        if cancellable.is_cancelled():
            return

        def worker() -> None:
            try:
                preview = parse_xlsx(path, cancelled=cancellable.is_cancelled)
            except XlsxPreviewCancelled:
                return
            except XlsxPreviewError as error:
                self._queue_error(cancellable, on_error, str(error))
                return
            except Exception:
                self._queue_error(
                    cancellable,
                    on_error,
                    "The spreadsheet preview could not be created safely",
                )
                return
            GLib.idle_add(
                self._deliver_preview,
                cancellable,
                on_ready,
                on_error,
                preview,
            )

        try:
            threading.Thread(
                target=worker,
                name="kukni-xlsx-parser",
                daemon=True,
            ).start()
        except RuntimeError:
            self._queue_error(
                cancellable,
                on_error,
                "The spreadsheet preview worker could not be started",
            )

    @staticmethod
    def _queue_error(
        cancellable: Gio.Cancellable,
        on_error: ErrorCallback,
        message: str,
    ) -> None:
        GLib.idle_add(
            SpreadsheetRenderer._deliver_error,
            cancellable,
            on_error,
            message,
        )

    @staticmethod
    def _deliver_error(
        cancellable: Gio.Cancellable,
        on_error: ErrorCallback,
        message: str,
    ) -> bool:
        if not cancellable.is_cancelled():
            on_error(message)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _deliver_preview(
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
        preview: XlsxPreview,
    ) -> bool:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        try:
            view = XlsxPreviewView(preview)
        except Exception:
            on_error("The spreadsheet view could not be created")
            return GLib.SOURCE_REMOVE
        on_ready(view, preview_subtitle(preview))
        return GLib.SOURCE_REMOVE


# Keep the format-specific spelling available to registries and extensions.
XlsxRenderer = SpreadsheetRenderer
