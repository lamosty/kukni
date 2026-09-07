# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded, immediate-child summaries for native folders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import errno
import os
import stat
import threading
import time
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango

from .base import ErrorCallback, ReadyCallback
from .text import sanitize_display_label


MAX_FOLDER_ENTRIES = 4096
# @constraint This is a cooperative work budget checked between entries, not a
# hard I/O deadline: one filesystem syscall can outlive it. Two admission slots
# bound readers that are stuck inside the kernel or a filesystem implementation.
MAX_FOLDER_SECONDS = 0.35
MAX_SAMPLE_ITEMS = 10
_WORKER_SLOTS = threading.BoundedSemaphore(2)


class FolderPreviewError(RuntimeError):
    """A folder could not be safely enumerated."""


class FolderPreviewCancelled(Exception):
    """Folder enumeration stopped because the selection changed."""


@dataclass(frozen=True, slots=True)
class FolderItem:
    """Display-safe metadata for one immediate child."""

    name: str
    kind: str
    size: int | None = None


@dataclass(frozen=True, slots=True)
class FolderSummary:
    """A bounded snapshot; counts are lower bounds when ``truncated``."""

    items: tuple[FolderItem, ...]
    total_count: int
    file_count: int
    folder_count: int
    hidden_count: int
    link_count: int
    other_count: int
    direct_file_size: int
    unknown_file_size_count: int
    truncated: bool = False
    unavailable_message: str = ""

    @classmethod
    def unavailable(cls, message: str) -> FolderSummary:
        return cls((), 0, 0, 0, 0, 0, 0, 0, 0, unavailable_message=message)


CancellationCheck = Callable[[], bool]
Clock = Callable[[], float]


def scan_folder(
    file: Gio.File,
    *,
    cancellable: Gio.Cancellable | None = None,
    cancelled: CancellationCheck | None = None,
    max_entries: int = MAX_FOLDER_ENTRIES,
    time_limit_seconds: float = MAX_FOLDER_SECONDS,
    sample_limit: int = MAX_SAMPLE_ITEMS,
    clock: Clock = time.monotonic,
) -> FolderSummary:
    """Enumerate immediate children without following links or reading content."""

    if max_entries <= 0 or time_limit_seconds <= 0 or sample_limit <= 0:
        raise ValueError("folder scan limits must be positive")
    if not file.is_native() or file.get_path() is None:
        raise FolderPreviewError("Folder previews support local folders only.")
    _check_cancelled(cancellable, cancelled)

    gio_cancellable = cancellable or Gio.Cancellable()
    started = clock()
    descriptor = -1
    entries: list[FolderItem] = []
    total_count = file_count = folder_count = hidden_count = 0
    link_count = other_count = direct_file_size = unknown_file_size_count = 0
    truncated = False

    try:
        # @security Pin the selected directory before reading entries. A Gio
        # query followed by enumeration would leave a path-replacement race;
        # O_NOFOLLOW plus descriptor-relative scandir/stat keeps the snapshot
        # on the directory actually opened. Children are never opened or
        # followed, and immediate directories/mount points are only counted.
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NOCTTY", 0)
        )
        descriptor = os.open(file.get_path(), flags)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise FolderPreviewError("This item is not a local folder.")
        with os.scandir(descriptor) as iterator:
            while total_count < max_entries:
                _check_cancelled(gio_cancellable, cancelled)
                if clock() - started >= time_limit_seconds:
                    truncated = True
                    break
                try:
                    entry = next(iterator)
                except StopIteration:
                    break

                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    # The child may have disappeared. Keep a safe inert row and
                    # count it as other rather than retrying or following it.
                    # It could have been a regular file, so the known direct
                    # byte sum is now only a lower bound.
                    metadata = None
                    unknown_file_size_count += 1
                total_count += 1
                hidden_count += int(entry.name.startswith("."))
                kind = _item_kind(metadata)
                size: int | None = None
                if kind == "folder":
                    folder_count += 1
                elif kind == "file":
                    file_count += 1
                    if metadata is not None:
                        size = max(0, metadata.st_size)
                        direct_file_size += size
                    else:
                        unknown_file_size_count += 1
                elif kind == "link":
                    link_count += 1
                else:
                    other_count += 1

                entries.append(
                    FolderItem(
                        _bounded_label(entry.name, "Unnamed item"),
                        kind,
                        size,
                    )
                )
            else:
                # Do not ask for an extra entry just to distinguish an exact
                # limit. The strict cap wins; counts become honest lower bounds.
                truncated = True
    except OSError as error:
        raise FolderPreviewError(_read_error_message(error)) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass

    _check_cancelled(gio_cancellable, cancelled)
    entries.sort(key=lambda item: (item.name.casefold(), item.name))
    return FolderSummary(
        tuple(entries[:sample_limit]),
        total_count,
        file_count,
        folder_count,
        hidden_count,
        link_count,
        other_count,
        direct_file_size,
        unknown_file_size_count,
        truncated,
    )


def _check_cancelled(
    cancellable: Gio.Cancellable | None,
    cancelled: CancellationCheck | None,
) -> None:
    if (cancellable is not None and cancellable.is_cancelled()) or (
        cancelled is not None and cancelled()
    ):
        raise FolderPreviewCancelled("folder preview cancelled")


def _item_kind(metadata: os.stat_result | None) -> str:
    if metadata is None:
        return "other"
    if stat.S_ISLNK(metadata.st_mode):
        return "link"
    if stat.S_ISDIR(metadata.st_mode):
        return "folder"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def _bounded_label(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        # os.scandir decodes undecodable bytes with surrogate escapes. GTK
        # requires valid UTF-8, so make those bytes visible as inert text.
        value = "".join(
            f"\\u{ord(character):04x}"
            if 0xD800 <= ord(character) <= 0xDFFF
            else character
            for character in value
        )
    label = sanitize_display_label(value, fallback)
    return label if len(label) <= 256 else f"{label[:255]}…"


def _read_error_message(error: OSError) -> str:
    if isinstance(error, PermissionError):
        return "Permission was denied while reading this folder."
    if isinstance(error, FileNotFoundError):
        return "This folder is no longer available."
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return "Symbolic-link folders are not enumerated."
    return "The folder’s contents could not be read."


class FolderPreviewView(Gtk.Box):
    """Compact, non-interactive overview of immediate folder children."""

    preview_geometry = ("folder", 0, 0)

    def __init__(self, summary: FolderSummary, icon: Gio.Icon | None = None) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=22,
            margin_bottom=22,
            margin_start=26,
            margin_end=26,
        )
        self.set_focusable(False)
        self.summary = summary

        overview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        overview_icon = Gtk.Image(pixel_size=48)
        if icon is not None:
            overview_icon.set_from_gicon(icon)
        else:
            overview_icon.set_from_icon_name("folder-symbolic")
        overview.append(overview_icon)
        overview_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        heading = Gtk.Label(label=self._heading(summary), xalign=0, use_markup=False)
        heading.add_css_class("title-2")
        overview_text.append(heading)
        explanation = Gtk.Label(
            label=self._explanation(summary),
            xalign=0,
            wrap=True,
            use_markup=False,
        )
        explanation.add_css_class("dim-label")
        overview_text.append(explanation)
        overview.append(overview_text)
        self.append(overview)

        if summary.unavailable_message:
            detail = Gtk.Label(
                label=summary.unavailable_message,
                xalign=0,
                wrap=True,
                selectable=True,
                use_markup=False,
            )
            detail.add_css_class("card")
            detail.set_margin_top(8)
            detail.set_margin_bottom(8)
            detail.set_margin_start(2)
            detail.set_margin_end(2)
            self.append(detail)
            return

        self.append(self._stats(summary))
        size_label = Gtk.Label(
            label=self._direct_size_text(summary),
            xalign=0,
            wrap=True,
            use_markup=False,
        )
        size_label.add_css_class("dim-label")
        self.append(size_label)

        if not summary.items:
            empty = Gtk.Label(
                label=self._empty_message(summary),
                xalign=0,
                margin_top=10,
                use_markup=False,
            )
            empty.add_css_class("title-4")
            self.append(empty)
            return

        items_heading = Gtk.Label(label="Items", xalign=0, use_markup=False)
        items_heading.add_css_class("heading")
        self.append(items_heading)
        self.scroller = Gtk.ScrolledWindow(
            hexpand=True,
            vexpand=True,
            has_frame=True,
            focusable=False,
            min_content_height=140,
        )
        self.item_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            focusable=False,
        )
        self.item_list.add_css_class("boxed-list")
        for item in summary.items:
            self.item_list.append(self._item_row(item))
        self.scroller.set_child(self.item_list)
        self.append(self.scroller)

        remaining = summary.total_count - len(summary.items)
        if summary.truncated:
            note = (
                f"More items are not shown; counts cover at least "
                f"{summary.total_count} scanned items."
            )
        elif remaining > 0:
            note = f"{remaining} more {'item' if remaining == 1 else 'items'} not shown."
        else:
            note = ""
        if note:
            note_label = Gtk.Label(label=note, xalign=0, wrap=True, use_markup=False)
            note_label.add_css_class("dim-label")
            note_label.add_css_class("caption")
            self.append(note_label)

    @staticmethod
    def _heading(summary: FolderSummary) -> str:
        if summary.unavailable_message:
            return "Folder contents couldn’t be listed"
        if summary.truncated and summary.total_count == 0:
            return "Partial folder summary"
        if summary.total_count == 0:
            return "Empty folder"
        prefix = "At least " if summary.truncated else ""
        noun = "item" if summary.total_count == 1 else "items"
        return f"{prefix}{summary.total_count} {noun}"

    @staticmethod
    def _explanation(summary: FolderSummary) -> str:
        if summary.unavailable_message:
            return "The folder itself is available, but the items inside are not."
        if summary.truncated:
            return "Partial summary of items directly inside this folder; includes hidden items."
        return "Items directly inside this folder; includes hidden items."

    @staticmethod
    def _direct_size_text(summary: FolderSummary) -> str:
        direct_size = GLib.format_size(summary.direct_file_size)
        if summary.truncated or summary.unknown_file_size_count:
            direct_size = f"at least {direct_size} known"
        return f"Files directly in this folder: {direct_size}"

    @staticmethod
    def _empty_message(summary: FolderSummary) -> str:
        if summary.truncated:
            return "The scan stopped before any items could be listed."
        return "No items in this folder."

    @staticmethod
    def _stats(summary: FolderSummary) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=22, row_spacing=4)
        stats = (
            ("Folders", summary.folder_count),
            ("Files", summary.file_count),
            ("Hidden items", summary.hidden_count),
            ("Links", summary.link_count),
            ("Other items", summary.other_count),
        )
        for index, (label, value) in enumerate(stats):
            prefix = "≥ " if summary.truncated else ""
            widget = Gtk.Label(
                label=f"{label}: {prefix}{value}",
                xalign=0,
                use_markup=False,
            )
            grid.attach(widget, index % 3, index // 3, 1, 1)
        return grid

    @staticmethod
    def _item_row(item: FolderItem) -> Gtk.Widget:
        row = Gtk.ListBoxRow(focusable=False, activatable=False, selectable=False)
        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        icon_name = {
            "folder": "folder-symbolic",
            "file": "text-x-generic-symbolic",
            "link": "emblem-symbolic-link-symbolic",
            "other": "unknown-symbolic",
        }[item.kind]
        content.append(Gtk.Image(icon_name=icon_name, pixel_size=20))
        name = Gtk.Label(
            label=item.name,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            use_markup=False,
        )
        name.set_hexpand(True)
        content.append(name)
        description = {
            "folder": "Folder",
            "file": GLib.format_size(item.size) if item.size is not None else "File",
            "link": "Symbolic link",
            "other": "Other item",
        }[item.kind]
        kind = Gtk.Label(label=description, xalign=1, use_markup=False)
        kind.add_css_class("dim-label")
        kind.add_css_class("caption")
        content.append(kind)
        row.set_child(content)
        return row


class FolderRenderer:
    """Prepare a bounded folder summary away from GTK's main context."""

    id = "folder"

    def __init__(self) -> None:
        self._pending_id = 0

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        return file.is_native() and info.get_file_type() == Gio.FileType.DIRECTORY

    def render(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        if self._pending_id:
            GLib.source_remove(self._pending_id)
            self._pending_id = 0
        if cancellable.is_cancelled():
            return
        icon = (
            info.get_icon()
            if info.has_attribute(Gio.FILE_ATTRIBUTE_STANDARD_ICON)
            else None
        )
        if not _WORKER_SLOTS.acquire(blocking=False):
            # @decision Keep only the latest waiting selection while the two
            # bounded readers unwind. Rapid browsing must neither create an
            # unbounded thread queue nor require the user to select again.
            self._pending_id = GLib.timeout_add(
                25,
                self._retry_pending,
                file,
                info,
                cancellable,
                on_ready,
                on_error,
            )
            return

        def worker() -> None:
            try:
                summary = scan_folder(file, cancellable=cancellable)
            except FolderPreviewCancelled:
                return
            except FolderPreviewError as error:
                summary = FolderSummary.unavailable(str(error))
            except Exception:
                summary = FolderSummary.unavailable(
                    "The folder’s contents could not be read safely."
                )
            finally:
                _WORKER_SLOTS.release()

            GLib.idle_add(
                self._create_view,
                summary,
                icon,
                cancellable,
                on_ready,
                on_error,
            )

        thread = threading.Thread(
            target=worker,
            name="kukni-folder-reader",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            _WORKER_SLOTS.release()
            GLib.idle_add(
                self._create_view,
                FolderSummary.unavailable(
                    "The folder preview worker could not be started."
                ),
                icon,
                cancellable,
                on_ready,
                on_error,
            )

    def _retry_pending(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> bool:
        self._pending_id = 0
        if not cancellable.is_cancelled():
            self.render(file, info, cancellable, on_ready, on_error)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _create_view(
        summary: FolderSummary,
        icon: Gio.Icon | None,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> bool:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        try:
            view = FolderPreviewView(summary, icon)
        except Exception:
            on_error("The folder summary could not be displayed.")
            return GLib.SOURCE_REMOVE
        if summary.unavailable_message:
            subtitle = "Folder · contents unavailable"
        else:
            prefix = "at least " if summary.truncated else ""
            noun = "item" if summary.total_count == 1 else "items"
            subtitle = f"Folder · {prefix}{summary.total_count} {noun}"
        on_ready(view, subtitle)
        return GLib.SOURCE_REMOVE
