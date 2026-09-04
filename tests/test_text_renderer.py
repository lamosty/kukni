# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio

from kukni.renderers.text import (
    MAX_COMBINING_RUN,
    MAX_LINE_CHARACTERS,
    MAX_VISIBLE_LINES,
    MAX_TEXT_BYTES,
    TextPreviewCancelled,
    TextPreviewError,
    TextRenderer,
    decode_text_bytes,
    normalize_visible_text,
    read_text_sample,
    sanitize_display_label,
    supports_text,
)


class TextSupportTests(unittest.TestCase):
    def test_supports_required_text_and_source_formats(self):
        cases = (
            ("notes.txt", "text/plain"),
            ("script.sh", "application/x-shellscript"),
            ("script.py", "text/x-python"),
            ("launcher.desktop", "application/x-desktop"),
            ("data.json", "application/json"),
            ("document.xml", "application/xml"),
            ("README.md", "text/markdown"),
        )
        for filename, content_type in cases:
            with self.subTest(filename=filename, content_type=content_type):
                self.assertTrue(supports_text(filename, content_type))

    def test_suffixes_cover_generic_source_files(self):
        self.assertTrue(supports_text("SCRIPT.ZSH", "application/octet-stream"))
        self.assertTrue(supports_text("track.gpx", "application/octet-stream"))
        self.assertTrue(supports_text("Dockerfile", None))
        self.assertTrue(supports_text(".env.local", "application/octet-stream"))

    def test_html_can_fall_back_to_inert_source_after_the_rich_renderer(self):
        self.assertTrue(supports_text("page.html", "text/html"))

    def test_does_not_claim_images_or_native_executables(self):
        self.assertFalse(supports_text("image.txt", "image/png"))
        self.assertFalse(supports_text("binary.sh", "application/x-executable"))
        self.assertFalse(supports_text("binary", "application/x-pie-executable"))

    def test_renderer_requires_a_local_regular_file(self):
        renderer = TextRenderer()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.REGULAR)
        info.set_content_type("text/plain")

        self.assertTrue(renderer.supports(Gio.File.new_for_path("/tmp/file.txt"), info))
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_uri("https://invalid.example/file.txt"),
                info,
            )
        )
        info.set_file_type(Gio.FileType.DIRECTORY)
        self.assertFalse(
            renderer.supports(Gio.File.new_for_path("/tmp/directory.txt"), info)
        )


class TextNormalizationTests(unittest.TestCase):
    def test_rejects_invalid_utf8_instead_of_guessing_an_encoding(self):
        with self.assertRaisesRegex(TextPreviewError, "valid UTF-8"):
            normalize_visible_text(b"before\xffafter")

    def test_decodes_bom_marked_unicode(self):
        self.assertEqual(decode_text_bytes("ahoj".encode("utf-16")), "ahoj")
        self.assertEqual(decode_text_bytes("svet".encode("utf-32")), "svet")

    def test_rejects_nul_labelled_as_utf8_text(self):
        with self.assertRaisesRegex(TextPreviewError, "NUL"):
            decode_text_bytes(b"text\x00payload")

    def test_trims_only_an_incomplete_final_utf8_scalar_when_sample_is_cut(self):
        self.assertEqual(
            decode_text_bytes(b"safe \xe2\x82", allow_trailing_partial=True),
            "safe ",
        )
        with self.assertRaises(TextPreviewError):
            decode_text_bytes(b"bad\xfftail", allow_trailing_partial=True)

    def test_makes_controls_and_bidi_overrides_visible(self):
        source = "ok\x00\x1b\x7f\u202ehidden\u2066text\u2069"

        rendered = normalize_visible_text(source)

        self.assertEqual(rendered, "ok␀␛␡⟦RLO⟧hidden⟦LRI⟧text⟦PDI⟧")
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\u2066", rendered)

    def test_preserves_indentation_and_normalizes_line_endings(self):
        self.assertEqual(normalize_visible_text("a\r\n\tb\rc"), "a\n\tb␍c")

    def test_bounds_pathological_line_line_count_and_combining_runs(self):
        rendered = normalize_visible_text(
            ("a" * (MAX_LINE_CHARACTERS + 10))
            + "\n"
            + ("x\n" * MAX_VISIBLE_LINES)
            + "z"
            + ("\u0301" * (MAX_COMBINING_RUN + 5))
        )
        self.assertIn("⟦LINE CLIPPED⟧", rendered)
        self.assertIn("⟦PREVIEW CLIPPED TO SAFE DISPLAY LIMITS⟧", rendered)

        combining = normalize_visible_text(
            "z" + ("\u0301" * (MAX_COMBINING_RUN + 5))
        )
        self.assertIn("⟦COMBINING MARKS CLIPPED⟧", combining)

    def test_sanitizes_bidi_and_multiline_filenames(self):
        rendered = sanitize_display_label("invoice\u202egnp.exe\nnext")
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\n", rendered)
        self.assertIn("⟦RLO⟧", rendered)
        self.assertIn("␊", rendered)

    def test_rejects_unknown_input_types(self):
        with self.assertRaises(TypeError):
            normalize_visible_text(object())


class TextReadTests(unittest.TestCase):
    def test_reads_a_regular_file_and_records_script_safety_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "script.sh")
            source = b"#!/bin/sh\nprintf safe\n"
            path.write_bytes(source)
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            sample = read_text_sample(path)

        self.assertEqual(sample.data, source)
        self.assertFalse(sample.truncated)
        self.assertTrue(sample.executable)
        self.assertTrue(sample.has_shebang)

    def test_never_executes_an_executable_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "must-not-exist"
            script = directory / "dangerous.sh"
            script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            sample = read_text_sample(script)

            self.assertIn(b"touch", sample.data)
            self.assertFalse(marker.exists())

    def test_bounds_large_files_and_marks_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "large.txt")
            path.write_bytes(b"a" * 17)

            sample = read_text_sample(path, limit=16)

        self.assertEqual(sample.data, b"a" * 16)
        self.assertTrue(sample.truncated)

    def test_detects_growth_beyond_initial_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "growing.txt")
            path.write_bytes(b"12345")
            real_fstat = os.fstat

            def report_small_initial_size(descriptor):
                result = real_fstat(descriptor)
                values = list(result)
                values[6] = 1
                return os.stat_result(values)

            with mock.patch(
                "kukni.renderers.text.os.fstat",
                side_effect=report_small_initial_size,
            ):
                sample = read_text_sample(path, limit=4)

        self.assertEqual(sample.data, b"1234")
        self.assertTrue(sample.truncated)

    def test_rejects_elf_even_with_a_text_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "misleading.sh")
            path.write_bytes(b"\x7fELF" + b"\x00" * 32)

            with self.assertRaisesRegex(TextPreviewError, "Binary"):
                read_text_sample(path)

    def test_rejects_other_strong_binary_magic_with_text_suffixes(self):
        fixtures = (
            ("archive.txt", b"PK\x03\x04archive"),
            ("document.md", b"%PDF-1.7\n"),
            ("image.py", b"\x89PNG\r\n\x1a\ncontent"),
            ("module.sh", b"\x00asm\x01\x00\x00\x00"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for filename, contents in fixtures:
                with self.subTest(filename=filename):
                    path = Path(temporary, filename)
                    path.write_bytes(contents)
                    with self.assertRaisesRegex(TextPreviewError, "Binary"):
                        read_text_sample(path)

    def test_rejects_non_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(TextPreviewError, "regular"):
                read_text_sample(temporary)

    def test_cancellation_prevents_path_access(self):
        with mock.patch("kukni.renderers.text.os.open") as open_file:
            with self.assertRaises(TextPreviewCancelled):
                read_text_sample("not-opened.txt", cancelled=lambda: True)
            open_file.assert_not_called()

    def test_cancellation_during_read_closes_the_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "cancelled.txt")
            path.write_bytes(b"a" * (128 * 1024))
            checks = 0

            def cancelled():
                nonlocal checks
                checks += 1
                return checks >= 3

            real_close = os.close
            closed: list[int] = []

            def record_close(descriptor):
                closed.append(descriptor)
                real_close(descriptor)

            with mock.patch("kukni.renderers.text.os.close", side_effect=record_close):
                with self.assertRaises(TextPreviewCancelled):
                    read_text_sample(path, cancelled=cancelled)

            self.assertEqual(len(closed), 1)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support is unavailable")
    def test_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            fifo = Path(temporary, "named-pipe.txt")
            os.mkfifo(fifo)

            with self.assertRaisesRegex(TextPreviewError, "regular"):
                read_text_sample(fifo)

    def test_accepts_a_symlink_to_regular_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "source.txt")
            source.write_text("linked source", encoding="utf-8")
            link = Path(temporary, "link.txt")
            link.symlink_to(source)

            self.assertEqual(read_text_sample(link).data, b"linked source")

    def test_rejects_non_positive_limits(self):
        with self.assertRaises(ValueError):
            read_text_sample("unused", limit=0)

    def test_default_limit_is_one_mib(self):
        self.assertEqual(MAX_TEXT_BYTES, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
