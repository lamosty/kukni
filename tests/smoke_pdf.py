#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Separate real PDF runtime evidence from deterministic page/GTK lifecycle QA."""

import argparse
import ctypes
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
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

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


class NativeWheel:
    """Inject real X11 wheel buttons into GTK's native event path."""

    def __init__(self) -> None:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11  # noqa: F401 - registers get_xid()

        self.x11 = ctypes.CDLL("libX11.so.6")
        self.xtst = ctypes.CDLL("libXtst.so.6")
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XRootWindow.restype = ctypes.c_ulong
        self.x11.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong,
        ]
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise AssertionError("Native wheel smoke requires the isolated X11 display")

    def scroll_down(self, window: Gtk.Window, count: int = 1) -> None:
        surface = window.get_surface()
        root = self.x11.XRootWindow(self.display, 0)
        root_x, root_y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        translated = self.x11.XTranslateCoordinates(
            self.display, surface.get_xid(), root, 0, 0,
            ctypes.byref(root_x), ctypes.byref(root_y), ctypes.byref(child),
        )
        if not translated:
            raise AssertionError("Could not place native wheel over the PDF scroller")
        self.xtst.XTestFakeMotionEvent(
            self.display, 0,
            root_x.value + window.get_width() // 2,
            root_y.value + window.get_height() // 2,
            10,
        )
        for _ in range(count):
            self.xtst.XTestFakeButtonEvent(self.display, 5, 1, 10)
            self.xtst.XTestFakeButtonEvent(self.display, 5, 0, 10)
        self.x11.XFlush(self.display)


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
                view = getattr(self, "view", None)
                detail = ""
                if view is not None:
                    detail = (
                        f" page={view.page_number} value="
                        f"{view.scroller.get_vadjustment().get_value():.1f} "
                        f"loading={view._loading_page} textures={tuple(view._textures)}"
                    )
                raise AssertionError(f"PDF smoke timed out during {self.phase}{detail}")
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
            texture = getattr(view, "texture", None)
            if texture is not None and (texture.get_width() > 1_800 or texture.get_height() > 1_800):
                raise AssertionError("PDF texture exceeded its fixed pixel bound")
            if self.phase == "opening":
                if view.page_number != 1 or view.page_count != (12 if self.simulated else 3):
                    raise AssertionError("PDF page count or initial page is incorrect")
                if view.previous_button.get_sensitive() or not view.next_button.get_sensitive():
                    raise AssertionError("Initial PDF page controls have wrong bounds")
                if view.picture.get_content_fit() != Gtk.ContentFit.CONTAIN or not view.fit_mode:
                    raise AssertionError("PDF does not start in fit-width mode")
                self.view = view
                self.phase = "settling"
                GLib.timeout_add(350, self._start_scrolling)
                return GLib.SOURCE_REMOVE
            if view is not self.view:
                raise AssertionError("Page navigation replaced the document view")
            if self.window.get_default_size() != self.initial_size:
                raise AssertionError("Page navigation or zoom resized the existing window")
            if self.phase == "wheel-offset":
                adjustment = view.scroller.get_vadjustment()
                if adjustment.get_value() > self.wheel_start:
                    if view.page_number != 1:
                        raise AssertionError("One wheel notch flipped pages instead of scrolling content")
                    self.native_wheel.scroll_down(self.window, 28)
                    self.phase = "wheel-traverse"
            elif self.phase == "wheel-traverse" and view.page_number > 1:
                # Let the rest of the queued XTest wheel events settle before
                # continuing deterministic assertions from page 2.
                self.phase = "wheel-settling"
                GLib.timeout_add(450, self._reset_to_second)
                return GLib.SOURCE_REMOVE
            elif self.phase == "second" and view.page_number == 2 and 2 in view._textures:
                if view.texture.get_width() <= view.texture.get_height():
                    raise AssertionError("Second page did not display its landscape pixels")
                if not view.previous_button.get_sensitive():
                    raise AssertionError("Previous page remains unavailable on page two")
                self._capture("page-2")
                drag = mock.Mock()
                before_drag = view.scroller.get_vadjustment().get_value()
                view._on_drag_begin(drag, 0, 0)
                view._on_drag_update(drag, 0, 40)
                if not view._pan_active or view.scroller.get_vadjustment().get_value() >= before_drag:
                    raise AssertionError("PDF drag-to-pan was lost on the overflowed document")
                view._on_drag_end(drag, 0, 40)
                self.anchor_before_zoom = view._layout.capture_anchor(
                    view._rects, view.scroller.get_vadjustment().get_value(),
                )
                control_scroll = mock.Mock()
                control_scroll.get_current_event_state.return_value = 0
                if view._on_scroll(control_scroll, 0, 1):
                    raise AssertionError("Ordinary wheel input was consumed instead of scrolling natively")
                control_scroll.get_current_event_state.return_value = Gdk.ModifierType.CONTROL_MASK
                if not view._on_scroll(control_scroll, 0, -1):
                    raise AssertionError("Ctrl-wheel did not invoke PDF zoom")
                self.phase = "zoomed"
                GLib.timeout_add(220, self._poll_result)
                return GLib.SOURCE_REMOVE
            elif self.phase == "zoomed" and not view._pending_anchor and view.zoom == 1.25:
                after = view._layout.capture_anchor(
                    view._rects, view.scroller.get_vadjustment().get_value(),
                )
                if after[0] != self.anchor_before_zoom[0] or abs(after[1] - self.anchor_before_zoom[1]) > .04:
                    raise AssertionError("Zoom did not preserve the continuous-document scroll anchor")
                view.actual_size()
                self.phase = "actual"
                GLib.timeout_add(220, self._poll_result)
                return GLib.SOURCE_REMOVE
            elif self.phase == "actual" and not view._pending_anchor:
                if view.fit_mode or view._zoom_basis != "pixels" or view.zoom != 1.0:
                    raise AssertionError("PDF 1:1 did not select retained-pixel sizing")
                view.fit()
                self.phase = "fitted"
                GLib.timeout_add(220, self._poll_result)
                return GLib.SOURCE_REMOVE
            elif self.phase == "fitted" and not view._pending_anchor:
                if not view.fit_mode or view._zoom_basis != "width":
                    raise AssertionError("PDF Fit did not restore width-fit sizing")
                viewport_width = view.scroller.get_hadjustment().get_page_size()
                expected_width = max(1, viewport_width - 2 * view._layout.margin)
                if abs(view._rects[view.page_number - 1].width - expected_width) > 2:
                    raise AssertionError(
                        "Fit width left the rendered page too small to read "
                        f"({view._rects[view.page_number - 1].width} != {expected_width})"
                    )
                if self.simulated:
                    self.tour_page = 3
                    self.window.activate_action("win.page-next", None)
                    self.phase = "tour"
                else:
                    self._finish()
                    return GLib.SOURCE_REMOVE
            elif self.phase == "tour" and self.tour_page in view._textures:
                if view.retained_texture_count > view._MAX_RETAINED_TEXTURES:
                    raise AssertionError("PDF retained texture cache exceeded its bound")
                if self.tour_page < 9:
                    self.tour_page += 1
                    view.request_page(self.tour_page)
                else:
                    if len(view._textures) > 5 or len(view._textures) >= view.page_count:
                        raise AssertionError("Scrolling eagerly retained the whole PDF")
                    view.request_page(11)
                    self.phase = "page-error"
            elif self.phase == "page-error" and 11 in view._errors:
                if view.page_number != 11 or "unavailable" not in view.page_label.get_label():
                    raise AssertionError("A failed lazy page was not represented locally")
                if "synthetic page failure" not in view.page_label.get_tooltip_text():
                    raise AssertionError("A later-page failure has no diagnostic")
                view.request_page(12)
                self.phase = "after-error"
            elif self.phase == "after-error" and 12 in view._textures:
                if view.page_number != 12:
                    raise AssertionError("A failed page prevented continued document navigation")
                self._finish()
                return GLib.SOURCE_REMOVE
        except Exception as error:
            traceback.print_exc()
            self.failures.append(str(error))
            self._finish()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _start_scrolling(self) -> bool:
        self.initial_size = self.window.get_default_size()
        self._capture("page-1")
        view = self.view
        monitor = self.window._monitor_size()
        if self.window.get_width() > monitor.width or self.window.get_height() > monitor.height:
            raise AssertionError("PDF window overflowed the logical monitor")
        if view.toolbar.get_width() > self.window.get_width():
            raise AssertionError("PDF page/zoom controls forced horizontal window overflow")
        adjustment = view.scroller.get_vadjustment()
        if adjustment.get_value() > view._rects[0].y:
            raise AssertionError(
                f"PDF did not open at the top of page one ({adjustment.get_value():.1f})"
            )
        if adjustment.get_upper() <= adjustment.get_page_size():
            raise AssertionError("PDF document has no native vertical scroll range")
        viewport_width = view.scroller.get_hadjustment().get_page_size()
        expected_width = max(1, viewport_width - 2 * view._layout.margin)
        if abs(view._rects[0].width - expected_width) > 2:
            raise AssertionError("Initial PDF page is not fit to the viewport width")
        self.native_wheel = NativeWheel()
        self.wheel_start = adjustment.get_value()
        self.native_wheel.scroll_down(self.window)
        self.phase = "wheel-offset"
        GLib.timeout_add(80, self._poll_result)
        return GLib.SOURCE_REMOVE

    def _capture(self, name: str) -> None:
        if self.screenshots:
            kind = "simulated" if self.simulated else "real"
            capture(self.window, self.screenshots / f"pdf-{kind}-{name}.png")

    def _reset_to_second(self) -> bool:
        self.view.request_page(2)
        self.phase = "second"
        GLib.timeout_add(50, self._poll_result)
        return GLib.SOURCE_REMOVE

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
        if page == 11:
            raise PdfPreviewError("synthetic page failure")
        return PdfPage(landscape if page % 2 == 0 else portrait, page, 12, 12)

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
    print("Deterministic PDF GTK continuous scrolling, lazy cache, fit/zoom and error recovery passed (synthetic pixels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
