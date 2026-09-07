#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the native folder summary in an isolated GTK session."""

import argparse
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from kukni.renderers.folder import FolderPreviewView, FolderRenderer
from kukni.renderers.registry import RendererRegistry
from kukni.session import PreviewState
from kukni.window import PreviewWindow
from smoke_images import capture


def labels_below(widget: Gtk.Widget) -> list[Gtk.Label]:
    labels: list[Gtk.Label] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            labels.append(child)
        labels.extend(labels_below(child))
        child = child.get_next_sibling()
    return labels


class FolderSmokeApplication(Adw.Application):
    def __init__(self, mixed: Path, empty: Path, screenshots: Path | None) -> None:
        super().__init__(application_id="io.github.lamosty.Kukni.FolderSmoke")
        self.mixed = mixed
        self.empty = empty
        self.screenshots = screenshots
        self.failures: list[str] = []
        self.window = None
        self.polls = 0

    def do_activate(self) -> None:
        self.window = PreviewWindow(self, RendererRegistry((FolderRenderer(),)))
        self.window.show_file(Gio.File.new_for_path(str(self.mixed)))
        GLib.timeout_add(50, self._poll_mixed)
        GLib.timeout_add_seconds(7, self._watchdog)

    def _poll_mixed(self) -> bool:
        self.polls += 1
        state = self.window.session.snapshot.state
        if state is PreviewState.OPENING and self.polls < 80:
            return GLib.SOURCE_CONTINUE
        GLib.timeout_add(350, self._check_mixed)
        return GLib.SOURCE_REMOVE

    def _check_mixed(self) -> bool:
        state = self.window.session.snapshot.state
        if state is not PreviewState.PREVIEW:
            self.failures.append(f"mixed folder expected preview, received {state.value}")

        view = self.window._stack.get_child_by_name("content")
        if not isinstance(view, FolderPreviewView):
            self.failures.append("folder did not return its native summary view")
        else:
            summary = view.summary
            if (summary.folder_count, summary.file_count, summary.hidden_count, summary.link_count) != (
                1,
                2,
                1,
                1,
            ):
                self.failures.append(f"unexpected folder counts: {summary}")
            if summary.direct_file_size != 8:
                self.failures.append("direct-file size did not exclude folders and links")
            labels = labels_below(view)
            text = "\n".join(label.get_text() for label in labels)
            if "Files directly in this folder: 8 bytes" not in text:
                self.failures.append("honestly labelled direct-file size is missing")
            if "<literal-item>" not in text:
                self.failures.append("literal child name is missing")
            if "⟦RLO⟧" not in text or "\u202e" in text:
                self.failures.append("deceptive child-name controls were not revealed")
            if self.mixed.name in text:
                self.failures.append("content redundantly repeated the header folder name")
            if any(label.get_use_markup() for label in labels):
                self.failures.append("folder summary interpreted a child name as markup")
            if view.get_focusable():
                self.failures.append("folder summary can steal navigation focus")
            if view.scroller.get_height() < 100:
                self.failures.append("folder item list did not retain a usable scroll area")
        self._capture("folder-mixed")

        self.window.show_file(Gio.File.new_for_path(str(self.empty)))
        self.polls = 0
        GLib.timeout_add(50, self._poll_empty)
        return GLib.SOURCE_REMOVE

    def _poll_empty(self) -> bool:
        self.polls += 1
        state = self.window.session.snapshot.state
        if state is PreviewState.OPENING and self.polls < 80:
            return GLib.SOURCE_CONTINUE
        GLib.timeout_add(350, self._check_empty)
        return GLib.SOURCE_REMOVE

    def _check_empty(self) -> bool:
        state = self.window.session.snapshot.state
        if state is not PreviewState.PREVIEW:
            self.failures.append(f"empty folder expected preview, received {state.value}")
        view = self.window._stack.get_child_by_name("content")
        if not isinstance(view, FolderPreviewView):
            self.failures.append("empty folder lost the native summary view")
        else:
            text = "\n".join(label.get_text() for label in labels_below(view))
            if view.summary.total_count != 0 or "No items in this folder." not in text:
                self.failures.append("empty folder did not receive a friendly empty state")
            if view.preview_geometry[0] != "folder":
                self.failures.append("folder view did not request folder-card geometry")
        width, height = self.window.get_width(), self.window.get_height()
        monitor = self.window._monitor_size()
        if width > monitor.width or height > monitor.height:
            self.failures.append("folder geometry escaped logical monitor bounds")
        self._capture("folder-empty")
        self._finish()
        return GLib.SOURCE_REMOVE

    def _capture(self, name: str) -> None:
        if self.screenshots:
            capture(self.window, self.screenshots / f"{name}.png")

    def _finish(self) -> None:
        self.window.close()
        self.quit()

    def _watchdog(self) -> bool:
        self.failures.append("folder renderer smoke test timed out")
        self._finish()
        return GLib.SOURCE_REMOVE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        mixed = root / "KUKNI_FOLDER_SMOKE_SELECTED"
        empty = root / "empty"
        mixed.mkdir()
        empty.mkdir()
        (mixed / "child-folder").mkdir()
        (mixed / "child-folder" / "not-immediate").write_bytes(b"x" * 1000)
        (mixed / "<literal-item>").write_bytes(b"12345")
        (mixed / ".hidden-\u202eitem").write_bytes(b"abc")
        (mixed / "link").symlink_to(mixed / "<literal-item>")

        application = FolderSmokeApplication(mixed, empty, args.screenshots)
        exit_code = application.run(["kukni-folder-smoke"])
        failures = application.failures

    if exit_code != 0:
        failures.append(f"application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"smoke failure: {failure}", file=sys.stderr)
        return 1
    print("Native folder summary smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
