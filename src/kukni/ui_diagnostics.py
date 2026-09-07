# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Package-native, pixel-verifying HTML renderer self-test."""

from __future__ import annotations

from pathlib import Path
import tempfile

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GdkPixbuf, Gio, GLib, Gtk

from .renderers.html import HtmlRenderer, WebKit


APPLICATION_ID = "io.github.lamosty.Kukni.HtmlCheck"
DEADLINE_SECONDS = 8
BACKGROUND_RGB = (19, 42, 58)
MARKER_RGB = (37, 165, 106)
SUCCESS_LINE = "HTML rendering self-test: Passed"
_FAILURE_STAGES = frozenset(
    ("application", "deadline", "pixels", "render", "snapshot", "startup")
)
_FIXTURE = b"""<!doctype html>
<meta charset="utf-8">
<style>
html, body {
  width: 100%; height: 100%; margin: 0; overflow: hidden;
  background: rgb(19, 42, 58);
}
#kukni-html-self-test-marker {
  position: fixed; left: 25%; top: 25%; width: 50%; height: 50%;
  background: rgb(37, 165, 106);
}
</style>
<div id="kukni-html-self-test-marker"></div>
"""


def _near(pixel: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(actual - target) <= 3 for actual, target in zip(pixel, expected))


def failure_line(stage: str | None) -> str:
    """Return only an allow-listed stage, never a raw underlying diagnostic."""

    safe_stage = stage if stage in _FAILURE_STAGES else "application"
    return f"HTML rendering self-test: Failed ({safe_stage})"


def has_fixed_marker_pixels(
    pixels: bytes,
    *,
    width: int,
    height: int,
    rowstride: int,
    channels: int,
) -> bool:
    """Recognize both fixed fixture colors and their expected broad coverage."""

    if width < 32 or height < 32 or channels not in (3, 4):
        return False
    required_bytes = rowstride * (height - 1) + width * channels
    if rowstride < width * channels or len(pixels) < required_bytes:
        return False

    marker = 0
    background = 0
    opaque = 0
    total = width * height
    for y in range(height):
        row = y * rowstride
        for x in range(width):
            offset = row + x * channels
            rgb = tuple(pixels[offset : offset + 3])
            alpha = pixels[offset + 3] if channels == 4 else 255
            if alpha < 250:
                continue
            opaque += 1
            if _near(rgb, MARKER_RGB):
                marker += 1
            elif _near(rgb, BACKGROUND_RGB):
                background += 1

    # The marker covers one quarter of the page and the background the rest.
    # Wide bounds tolerate compositor scaling/rounding but reject blank,
    # transparent, wrong-color, and only-partially-painted snapshots.
    return (
        opaque >= total * 0.95
        and total * 0.15 <= marker <= total * 0.35
        and background >= total * 0.55
    )


def _texture_has_fixed_marker(texture) -> bool:
    # @decision WebKitGTK 6 returns a Gdk.Texture rather than a Cairo surface.
    # A PNG round-trip through the already-required GdkPixbuf API gives one
    # explicit RGB(A) channel order across all Gdk memory formats/backends.
    try:
        encoded = texture.save_to_png_bytes().get_data()
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(encoded)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            return False
        return has_fixed_marker_pixels(
            bytes(pixbuf.get_pixels()),
            width=pixbuf.get_width(),
            height=pixbuf.get_height(),
            rowstride=pixbuf.get_rowstride(),
            channels=pixbuf.get_n_channels(),
        )
    except Exception:
        return False


class HtmlSelfTestApplication(Gtk.Application):
    """Render one owned fixture without registering production integrations."""

    def __init__(self, fixture: Path) -> None:
        super().__init__(
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.fixture = fixture
        self.failure_stage: str | None = "startup"
        self._settled = False
        self._deadline_id = 0
        self._quit_id = 0
        self._cancellable = Gio.Cancellable()
        self._window: Gtk.ApplicationWindow | None = None
        self._snapshot_document = None
        self._snapshot_frames = 0

    def do_activate(self) -> None:
        try:
            self._window = Gtk.ApplicationWindow(
                application=self,
                title="Kukni HTML rendering self-test",
                default_width=320,
                default_height=240,
            )
            self._window.set_child(Gtk.Box())
            self._window.present()
            self._deadline_id = GLib.timeout_add_seconds(
                DEADLINE_SECONDS, self._on_deadline
            )
            file = Gio.File.new_for_path(str(self.fixture))
            info = file.query_info(
                ",".join(
                    (
                        Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                        Gio.FILE_ATTRIBUTE_STANDARD_CONTENT_TYPE,
                        Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
                    )
                ),
                Gio.FileQueryInfoFlags.NONE,
                None,
            )
            HtmlRenderer().render(
                file,
                info,
                self._cancellable,
                self._on_ready,
                lambda _message: self._finish("render"),
            )
        except Exception:
            self._finish("startup")

    def _on_ready(self, wrapper: Gtk.Widget, _detail: str) -> None:
        if self._settled or self._window is None:
            return
        try:
            document = (
                wrapper.get_child_by_name("document")
                if hasattr(wrapper, "get_child_by_name")
                else None
            )
            valid_document = (
                document is not None
                and WebKit is not None
                and isinstance(document, WebKit.WebView)
            )
        except Exception:
            valid_document = False
            document = None
        if not valid_document:
            self._finish("render")
            return
        try:
            self._window.set_child(wrapper)
        except Exception:
            self._finish("render")
            return

        self._snapshot_document = document
        self._snapshot_frames = 0
        wrapper.add_tick_callback(self._after_snapshot_frame)

    def _after_snapshot_frame(self, _widget, _frame_clock) -> bool:
        if self._settled:
            return GLib.SOURCE_REMOVE
        self._snapshot_frames += 1
        if self._snapshot_frames < 2:
            return GLib.SOURCE_CONTINUE
        try:
            self._snapshot_document.get_snapshot(
                WebKit.SnapshotRegion.VISIBLE,
                WebKit.SnapshotOptions.NONE,
                self._cancellable,
                self._on_snapshot,
                None,
            )
        except Exception:
            self._finish("snapshot")
        return GLib.SOURCE_REMOVE

    def _on_snapshot(self, view, result, _data=None) -> None:
        if self._settled:
            return
        try:
            texture = view.get_snapshot_finish(result)
        except Exception:
            self._finish("snapshot")
            return
        self._finish(None if _texture_has_fixed_marker(texture) else "pixels")

    def _on_deadline(self) -> bool:
        self._deadline_id = 0
        self._finish("deadline")
        return GLib.SOURCE_REMOVE

    def _finish(self, failure_stage: str | None) -> None:
        if self._settled:
            return
        self._settled = True
        self.failure_stage = failure_stage
        if self._deadline_id:
            GLib.source_remove(self._deadline_id)
            self._deadline_id = 0
        self._cancellable.cancel()
        # HtmlRenderer queues WebKit process teardown from its cancellation
        # callback. Quit at low idle priority so that cleanup runs first rather
        # than being abandoned with the main loop.
        try:
            self._quit_id = GLib.idle_add(
                self._close_and_quit,
                priority=GLib.PRIORITY_LOW,
            )
        except Exception:
            self._close_and_quit()

    def _close_and_quit(self) -> bool:
        self._quit_id = 0
        if self._window is not None:
            self._window.close()
        self.quit()
        return GLib.SOURCE_REMOVE


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="kukni-html-check-") as temporary:
            fixture = Path(temporary) / "fixed-self-test.html"
            fixture.write_bytes(_FIXTURE)
            application = HtmlSelfTestApplication(fixture)
            exit_code = application.run(["kukni-html-check"])
            stage = application.failure_stage
    except Exception:
        print(failure_line("startup"))
        return 1

    if exit_code == 0 and stage is None:
        print(SUCCESS_LINE)
        return 0
    print(failure_line(stage))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
