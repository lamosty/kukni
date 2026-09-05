#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Separate real PDF runtime evidence from deterministic page/GTK lifecycle QA."""

import argparse
from pathlib import Path
import sys
import tempfile
import traceback
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from kukni.application import KukniApplication
from kukni.renderers.pdf import (
    PdfPage, PdfPreviewError, PdfPreviewView, PdfRenderer,
    pdf_runtime_available, pdf_runtime_unavailable_reason,
)
from kukni.renderers.registry import RendererRegistry
from kukni.session import PreviewState
from kukni.window import PreviewWindow
from image_fixtures import png
from smoke_images import capture
from test_pdf_renderer import build_minimal_pdf


class PdfSmokeApplication(Adw.Application):
    def __init__(self, sample: Path, *, simulated=False, screenshots=None) -> None:
        name = "PdfModelSmoke" if simulated else "PdfRuntimeSmoke"
        super().__init__(application_id=f"io.github.lamosty.Kukni.{name}")
        self.sample = sample
        self.simulated = simulated
        self.screenshots = screenshots
        self.failures: list[str] = []
        self.window = None
        self.checks = 0
        self.phase = "opening"
        self.expected = PreviewState.PREVIEW if simulated or pdf_runtime_available() else PreviewState.FALLBACK

    def do_activate(self) -> None:
        KukniApplication._load_styles()
        self.window = PreviewWindow(self, RendererRegistry((PdfRenderer(),)))
        self.window.show_file(Gio.File.new_for_path(str(self.sample)))
        GLib.timeout_add(50, self._poll_result)

    def _poll_result(self) -> bool:
        self.checks += 1
        try:
            if self.checks > 260:
                raise AssertionError(f"PDF smoke timed out during {self.phase}")
            snapshot = self.window.session.snapshot
            if snapshot.state is PreviewState.OPENING:
                return GLib.SOURCE_CONTINUE
            if snapshot.state is not self.expected:
                raise AssertionError(f"expected {self.expected.value}, got {snapshot.state.value}: {snapshot.detail}")
            if not self.window.get_visible():
                raise AssertionError("PDF preview closed the window")
            if self.expected is PreviewState.FALLBACK:
                if snapshot.detail != pdf_runtime_unavailable_reason():
                    raise AssertionError("Mandatory sandbox failure was not exposed in file details")
                self._finish()
                return GLib.SOURCE_REMOVE
            view = self.window._stack.get_child_by_name("content")
            if not isinstance(view, PdfPreviewView):
                raise AssertionError("Successful PDF route did not display a PDF canvas")
            if view.texture.get_width() > 1_800 or view.texture.get_height() > 1_800:
                raise AssertionError("PDF texture exceeded its fixed pixel bound")
            if self.phase == "opening":
                if view.page_number != 1 or view.page_count != (4 if self.simulated else 3):
                    raise AssertionError("PDF page count or initial page is incorrect")
                if view.previous_button.get_sensitive() or not view.next_button.get_sensitive():
                    raise AssertionError("Initial PDF page controls have wrong bounds")
                if view.picture.get_content_fit() != Gtk.ContentFit.CONTAIN or not view.fit_mode:
                    raise AssertionError("PDF does not start in fit-page mode")
                self.view = view
                self.phase = "settling"
                GLib.timeout_add(250, self._start_navigation)
                return GLib.SOURCE_REMOVE
            if view is not self.view:
                raise AssertionError("Page navigation replaced the document view")
            if self.window.get_default_size() != self.initial_size:
                raise AssertionError("Page navigation or zoom resized the existing window")
            if self.phase == "second" and view.page_number == 2:
                if view.texture.get_width() <= view.texture.get_height():
                    raise AssertionError("Second page did not display its landscape pixels")
                if not view.previous_button.get_sensitive():
                    raise AssertionError("Previous page remains unavailable on page two")
                view.actual_size()
                view.zoom_in()
                view.fit()
                self.phase = "second-settling"
                GLib.timeout_add(200, self._return_to_first)
            elif self.phase == "first-again" and view.page_number == view.requested_page == 1:
                if not view.fit_mode:
                    raise AssertionError("Changing pages failed to restore Fit")
                if self.simulated:
                    view.request_page(4)
                    self.phase = "page-error"
                else:
                    self._finish()
                    return GLib.SOURCE_REMOVE
            elif self.phase == "page-error" and "unavailable" in view.page_label.get_label():
                if view.page_number != 1 or view.requested_page != 1:
                    raise AssertionError("A failed later page discarded the previous successful page")
                if "synthetic page failure" not in view.page_label.get_tooltip_text():
                    raise AssertionError("A later-page failure has no diagnostic")
                view.next_button.emit("clicked")
                self.phase = "retry"
            elif self.phase == "retry" and view.page_number == 2:
                self._finish()
                return GLib.SOURCE_REMOVE
        except Exception as error:
            traceback.print_exc()
            self.failures.append(str(error))
            self._finish()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _start_navigation(self) -> bool:
        self.initial_size = self.window.get_default_size()
        self._capture("page-1")
        self.window.activate_action("win.page-next", None)
        self.phase = "second"
        GLib.timeout_add(50, self._poll_result)
        return GLib.SOURCE_REMOVE

    def _return_to_first(self) -> bool:
        self._capture("page-2")
        # Both intermediate rendering and already-queued deliveries must be
        # unable to overwrite the newest navigation request.
        self.view.request_page(3)
        self.window.activate_action("win.page-previous", None)
        self.window.activate_action("win.page-previous", None)
        self.phase = "first-again"
        return GLib.SOURCE_REMOVE

    def _capture(self, name: str) -> None:
        if self.screenshots:
            kind = "simulated" if self.simulated else "real"
            capture(self.window, self.screenshots / f"pdf-{kind}-{name}.png")

    def _finish(self) -> None:
        self.window.close()
        self.quit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    portrait, landscape = png(1200, 1700), png(1800, 1200)

    def simulated_page(_path, page, **_kwargs):
        if page == 4:
            raise PdfPreviewError("synthetic page failure")
        return PdfPage(landscape if page == 2 else portrait, page, 4, 4)

    failures = []
    with tempfile.TemporaryDirectory() as temporary:
        sample = Path(temporary, "pages.pdf")
        sample.write_bytes(build_minimal_pdf(3))
        real = PdfSmokeApplication(sample, screenshots=args.screenshots)
        if real.run(["kukni-pdf-runtime-smoke"]):
            failures.append("Real-runtime smoke application exited unsuccessfully")
        failures.extend(real.failures)
        with mock.patch("kukni.renderers.pdf.render_pdf_page", side_effect=simulated_page):
            model = PdfSmokeApplication(sample, simulated=True, screenshots=args.screenshots)
            if model.run(["kukni-pdf-model-smoke"]):
                failures.append("Deterministic lifecycle smoke application exited unsuccessfully")
            failures.extend(model.failures)
    for failure in failures:
        print(f"PDF smoke failure: {failure}", file=sys.stderr)
    if failures:
        return 1
    if real.expected is PreviewState.PREVIEW:
        print("Real sandboxed PDF rendering and multipage navigation passed")
    else:
        print("Real PDF rendering UNAVAILABLE: mandatory sandbox fallback verified (not a rendering pass)")
    print("Deterministic PDF GTK page navigation, fit/zoom and error recovery passed (synthetic pixels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
