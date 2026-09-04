#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Verify PDFs use a native fit-page view without resizing Kukni."""

from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from kukni.renderers.pdf import PdfPreviewView, PdfRenderer, pdf_runtime_available
from kukni.renderers.registry import RendererRegistry
from kukni.session import PreviewState
from kukni.window import PreviewWindow
from test_pdf_renderer import build_minimal_pdf


class PdfSmokeApplication(Adw.Application):
    def __init__(self, sample: Path) -> None:
        super().__init__(application_id="io.github.lamosty.Kukni.PdfSmoke")
        self.sample = sample
        self.failures: list[str] = []
        self.window = None
        self.checks = 0
        self.expected = (
            PreviewState.PREVIEW
            if pdf_runtime_available()
            else PreviewState.FALLBACK
        )

    def do_activate(self) -> None:
        self.window = PreviewWindow(self, RendererRegistry((PdfRenderer(),)))
        self.window.show_file(Gio.File.new_for_path(str(self.sample)))
        GLib.timeout_add(100, self._poll_result)

    def _poll_result(self) -> bool:
        self.checks += 1
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.checks < 100:
            return GLib.SOURCE_CONTINUE

        if snapshot.state is not self.expected:
            self.failures.append(
                f"expected {self.expected.value}, received {snapshot.state.value}: "
                f"{snapshot.detail}"
            )
        widget = self.window._stack.get_child_by_name("content")
        if self.expected is PreviewState.PREVIEW and not isinstance(
            widget,
            PdfPreviewView,
        ):
            self.failures.append("PDF renderer did not return its fit-page view")
        elif isinstance(widget, PdfPreviewView):
            if widget.picture.get_content_fit() != Gtk.ContentFit.CONTAIN:
                self.failures.append("PDF page does not default to fit-page mode")
            if (
                widget.texture.get_width() > 1_800
                or widget.texture.get_height() > 1_800
            ):
                self.failures.append("PDF page exceeds the render dimension cap")
        if self.window.get_default_size() != (1180, 760):
            self.failures.append("PDF content changed the stable window size")
        if not self.window.get_visible():
            self.failures.append("PDF preview closed the window")
        self.window.close()
        self.quit()
        return GLib.SOURCE_REMOVE


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        sample = Path(temporary, "a4.pdf")
        sample.write_bytes(build_minimal_pdf())
        application = PdfSmokeApplication(sample)
        exit_code = application.run(["kukni-pdf-smoke"])
        failures = application.failures

    if exit_code != 0:
        failures.append(f"application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"smoke failure: {failure}", file=sys.stderr)
        return 1
    print(f"Fit-page PDF preview smoke test passed ({application.expected.value})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
