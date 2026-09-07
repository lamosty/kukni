# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Pure pixel-recognition tests for the HTML rendering self-test."""

from pathlib import Path
import sys
import unittest
from unittest import mock

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kukni.ui_diagnostics import (
    BACKGROUND_RGB,
    APPLICATION_ID,
    HtmlSelfTestApplication,
    MARKER_RGB,
    SUCCESS_LINE,
    _texture_has_fixed_marker,
    failure_line,
    has_fixed_marker_pixels,
)
from kukni import ui_diagnostics


def fixture_pixels(
    *,
    width=40,
    height=40,
    marker=MARKER_RGB,
    background=BACKGROUND_RGB,
    alpha=255,
):
    channels = 4
    rowstride = width * channels + 8
    pixels = bytearray(rowstride * height)
    for y in range(height):
        for x in range(width):
            in_marker = (
                width // 4 <= x < width * 3 // 4
                and height // 4 <= y < height * 3 // 4
            )
            color = marker if in_marker else background
            offset = y * rowstride + x * channels
            pixels[offset : offset + channels] = bytes((*color, alpha))
    return bytes(pixels), width, height, rowstride, channels


class HtmlPixelVerifierTests(unittest.TestCase):
    def verify(self, fixture):
        pixels, width, height, rowstride, channels = fixture
        return has_fixed_marker_pixels(
            pixels,
            width=width,
            height=height,
            rowstride=rowstride,
            channels=channels,
        )

    def test_accepts_fixed_marker_with_padded_rows(self):
        self.assertTrue(self.verify(fixture_pixels()))

    def test_ui_check_is_non_unique_and_separate_from_production(self):
        application = HtmlSelfTestApplication(Path("/fixed/internal-fixture.html"))
        self.assertEqual(application.get_application_id(), APPLICATION_ID)
        self.assertNotEqual(APPLICATION_ID, "io.github.lamosty.Kukni")
        self.assertTrue(application.get_flags() & Gio.ApplicationFlags.NON_UNIQUE)

    def test_settled_check_ignores_pending_snapshot_tick_and_callback(self):
        application = HtmlSelfTestApplication(Path("/fixed/internal-fixture.html"))
        application._snapshot_document = mock.Mock()
        application._settled = True
        self.assertEqual(
            application._after_snapshot_frame(None, None),
            GLib.SOURCE_REMOVE,
        )
        application._snapshot_document.get_snapshot.assert_not_called()
        view = mock.Mock()
        application._on_snapshot(view, mock.Mock())
        view.get_snapshot_finish.assert_not_called()

    def test_finish_defers_window_close_and_quit_for_renderer_cleanup(self):
        application = HtmlSelfTestApplication(Path("/fixed/internal-fixture.html"))
        application._window = mock.Mock()
        queued = []

        def queue(callback, **_kwargs):
            queued.append(callback)
            return 71

        with mock.patch.object(ui_diagnostics.GLib, "idle_add", side_effect=queue):
            with mock.patch.object(application, "quit") as quit_application:
                application._finish("render")
                application._window.close.assert_not_called()
                quit_application.assert_not_called()
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued.pop()(), GLib.SOURCE_REMOVE)
                application._window.close.assert_called_once_with()
                quit_application.assert_called_once_with()

    def test_gdk_texture_round_trip_preserves_marker_pixels(self):
        pixels, width, height, rowstride, _channels = fixture_pixels()
        texture = Gdk.MemoryTexture.new(
            width,
            height,
            Gdk.MemoryFormat.R8G8B8A8,
            GLib.Bytes.new(pixels),
            rowstride,
        )
        self.assertTrue(_texture_has_fixed_marker(texture))

    def test_rejects_wrong_marker_color(self):
        self.assertFalse(self.verify(fixture_pixels(marker=(180, 40, 90))))

    def test_rejects_blank_or_transparent_pixels(self):
        self.assertFalse(
            self.verify(fixture_pixels(marker=BACKGROUND_RGB))
        )
        self.assertFalse(self.verify(fixture_pixels(alpha=0)))

    def test_rejects_truncated_or_implausibly_small_buffers(self):
        pixels, width, height, rowstride, channels = fixture_pixels()
        required = rowstride * (height - 1) + width * channels
        self.assertFalse(
            has_fixed_marker_pixels(
                pixels[: required - 1],
                width=width,
                height=height,
                rowstride=rowstride,
                channels=channels,
            )
        )
        self.assertFalse(
            has_fixed_marker_pixels(
                b"\0" * 16,
                width=2,
                height=2,
                rowstride=8,
                channels=4,
            )
        )

    def test_result_lines_are_fixed_and_raw_details_are_never_interpolated(self):
        self.assertEqual(SUCCESS_LINE, "HTML rendering self-test: Passed")
        self.assertEqual(
            failure_line("pixels"),
            "HTML rendering self-test: Failed (pixels)",
        )
        self.assertEqual(
            failure_line("secret /tmp/document.html"),
            "HTML rendering self-test: Failed (application)",
        )


if __name__ == "__main__":
    unittest.main()
