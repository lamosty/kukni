# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio, GLib

from kukni.renderers.media import (
    BACKEND_GSTREAMER_PAINTABLE,
    BACKEND_GTK_MEDIA,
    MediaPreviewError,
    MediaRenderer,
    _friendly_error,
    available_media_backends,
    format_media_time,
    supports_media,
    validate_local_regular_file,
)


def drain_main_context() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


class MediaCapabilityTests(unittest.TestCase):
    @staticmethod
    def file_info(content_type: str, file_type=Gio.FileType.REGULAR):
        info = Gio.FileInfo()
        info.set_file_type(file_type)
        info.set_content_type(content_type)
        return info

    def test_mime_type_wins_and_mkv_is_supported(self):
        self.assertTrue(supports_media("clip.bin", "video/x-matroska"))
        self.assertTrue(supports_media("song.bin", "audio/flac"))
        self.assertTrue(supports_media("clip.bin", "application/x-matroska"))
        self.assertFalse(supports_media("misleading.mkv", "image/png"))

    def test_suffix_is_only_a_fallback_for_generic_types(self):
        self.assertTrue(supports_media("CLIP.MKV", "application/octet-stream"))
        self.assertTrue(supports_media("voice.opus", None))
        self.assertFalse(supports_media("archive.zip", "application/octet-stream"))

    def test_remote_playlists_are_never_selected_as_media(self):
        for content_type in (
            "application/vnd.apple.mpegurl",
            "audio/x-mpegurl",
            "audio/x-scpls",
            "video/vnd.mpegurl",
            "video/x-ms-wvx",
        ):
            self.assertFalse(supports_media("stream.m3u", content_type))

    def test_renderer_requires_a_local_regular_file(self):
        renderer = MediaRenderer()
        info = self.file_info("video/x-matroska")

        self.assertTrue(
            renderer.supports(Gio.File.new_for_path("/tmp/clip.mkv"), info)
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_uri("https://example.invalid/x.mkv"),
                info,
            )
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_path("/tmp/folder.mkv"),
                self.file_info("video/x-matroska", Gio.FileType.DIRECTORY),
            )
        )

    def test_backend_order_prefers_direct_gstreamer_sink(self):
        with mock.patch(
            "kukni.renderers.media.gtk4_paintable_sink_available",
            return_value=True,
        ):
            self.assertEqual(
                available_media_backends(),
                (BACKEND_GSTREAMER_PAINTABLE, BACKEND_GTK_MEDIA),
            )


class RegularFileValidationTests(unittest.TestCase):
    def test_accepts_a_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "sample.mkv")
            path.write_bytes(b"synthetic test bytes")

            result = validate_local_regular_file(os.fspath(path))

            self.assertEqual(result.st_size, len(b"synthetic test bytes"))

    def test_rejects_directories_but_accepts_regular_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "target.mkv")
            target.write_bytes(b"media")
            link = Path(directory, "link.mkv")
            link.symlink_to(target)

            with self.assertRaisesRegex(MediaPreviewError, "regular local file"):
                validate_local_regular_file(directory)
            self.assertEqual(validate_local_regular_file(os.fspath(link)).st_size, 5)

    def test_rejects_empty_media_before_starting_a_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "empty.mkv")
            path.write_bytes(b"")

            with self.assertRaisesRegex(MediaPreviewError, "empty"):
                validate_local_regular_file(os.fspath(path))

    def test_rejects_renamed_remote_playlist_by_content(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, content in enumerate(
                (
                    b"#EXTM3U\nhttps://example.invalid/live.ts\n",
                    b"\xef\xbb\xbf  [playlist]\nFile1=http://example.invalid/x\n",
                    b"<?xml version='1.0'?><ASX><Entry /></ASX>",
                    b"RTSPtext\nrtsp://example.invalid/live\n",
                )
            ):
                with self.subTest(index=index):
                    path = Path(directory, f"renamed-{index}.mkv")
                    path.write_bytes(content)
                    with self.assertRaisesRegex(MediaPreviewError, "playlists"):
                        validate_local_regular_file(os.fspath(path))

    def test_does_not_reject_uri_text_inside_binary_media_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "metadata.mkv")
            path.write_bytes(b"\x1aE\xdf\xa3https://example.invalid/metadata")

            self.assertGreater(validate_local_regular_file(os.fspath(path)).st_size, 0)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support is unavailable")
    def test_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "not-media.mkv")
            os.mkfifo(path)

            with self.assertRaisesRegex(MediaPreviewError, "regular local file"):
                validate_local_regular_file(os.fspath(path))


class TimeFormattingTests(unittest.TestCase):
    def test_formats_minutes_and_hours(self):
        self.assertEqual(format_media_time(0), "0:00")
        self.assertEqual(format_media_time(65.9), "1:05")
        self.assertEqual(format_media_time(3661), "1:01:01")

    def test_clamps_invalid_values(self):
        self.assertEqual(format_media_time(None), "0:00")
        self.assertEqual(format_media_time(-100), "0:00")
        self.assertEqual(format_media_time(float("inf")), "0:00")

    def test_missing_codec_error_is_actionable_and_distribution_neutral(self):
        message = _friendly_error("Your GStreamer installation is missing a plug-in")

        self.assertIn("codec", message)
        self.assertIn("distribution", message)
        self.assertNotIn("apt", message)


class RendererDispatchTests(unittest.TestCase):
    def test_missing_backend_reports_error_on_main_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "clip.mkv")
            path.write_bytes(b"not decoded by this unit test")
            errors: list[str] = []
            renderer = MediaRenderer()

            with mock.patch(
                "kukni.renderers.media.available_media_backends",
                return_value=(),
            ):
                renderer.render(
                    Gio.File.new_for_path(os.fspath(path)),
                    self._file_info(),
                    Gio.Cancellable(),
                    lambda *_args: self.fail("unexpected ready callback"),
                    errors.append,
                )
                self.assertEqual(errors, [])
                drain_main_context()

            self.assertEqual(len(errors), 1)
            self.assertIn("media backend", errors[0])

    def test_cancelled_request_never_constructs_a_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "clip.mkv")
            path.write_bytes(b"unused")
            cancellable = Gio.Cancellable()
            cancellable.cancel()
            callbacks: list[str] = []
            renderer = MediaRenderer()

            with mock.patch(
                "kukni.renderers.media._GtkMediaController"
            ) as controller:
                renderer.render(
                    Gio.File.new_for_path(os.fspath(path)),
                    self._file_info(),
                    cancellable,
                    lambda *_args: callbacks.append("ready"),
                    lambda *_args: callbacks.append("error"),
                )
                drain_main_context()

            controller.assert_not_called()
            self.assertEqual(callbacks, [])

    def test_dispatches_to_first_available_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "clip.mkv")
            path.write_bytes(b"unused")
            renderer = MediaRenderer()
            instances: list[object] = []

            class FakeController:
                def __init__(self, *_args):
                    instances.append(self)
                    self._on_finished = _args[-2]
                    self._owned_fd = _args[-1]

                def start(self):
                    os.close(self._owned_fd)
                    self._on_finished(self)

            with (
                mock.patch(
                    "kukni.renderers.media.available_media_backends",
                    return_value=(BACKEND_GSTREAMER_PAINTABLE, BACKEND_GTK_MEDIA),
                ),
                mock.patch(
                    "kukni.renderers.media._GstPlaybinController",
                    FakeController,
                ),
                mock.patch("kukni.renderers.media._GtkMediaController") as gtk_backend,
            ):
                renderer.render(
                    Gio.File.new_for_path(os.fspath(path)),
                    self._file_info(),
                    Gio.Cancellable(),
                    lambda *_args: None,
                    lambda *_args: None,
                )
                drain_main_context()

            self.assertEqual(len(instances), 1)
            gtk_backend.assert_not_called()

    @staticmethod
    def _file_info():
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.REGULAR)
        info.set_content_type("video/x-matroska")
        return info


if __name__ == "__main__":
    unittest.main()
