# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from dataclasses import replace
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio

from kukni.renderers.pdf import (
    DEFAULT_LIMITS,
    PdfPreviewCancelled,
    PdfPreviewError,
    PdfRenderer,
    _build_sandbox_command,
    _validate_png_dimensions,
    pdf_runtime_available,
    render_pdf_first_page,
    supports_pdf,
)


def build_minimal_pdf() -> bytes:
    """Build one valid A4 page without bundling a binary fixture."""

    stream = b"BT /F1 24 Tf 72 760 Td (Kukni PDF preview) Tj ET\n"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


class PdfSupportTests(unittest.TestCase):
    def test_detects_pdf_without_claiming_unrelated_files(self):
        self.assertTrue(supports_pdf("document.bin", "application/pdf"))
        self.assertTrue(supports_pdf("DOCUMENT.PDF", "application/octet-stream"))
        self.assertFalse(supports_pdf("document.pdf", "text/plain"))
        self.assertFalse(supports_pdf("document.txt", "text/plain"))

    def test_renderer_accepts_only_regular_local_files_when_poppler_exists(self):
        renderer = PdfRenderer()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.REGULAR)
        info.set_content_type("application/pdf")
        expected = pdf_runtime_available()

        self.assertEqual(
            renderer.supports(Gio.File.new_for_path("/tmp/document.pdf"), info),
            expected,
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_uri("https://invalid.example/document.pdf"),
                info,
            )
        )


@unittest.skipUnless(pdf_runtime_available(), "sandboxed PDF runtime unavailable")
class PdfProcessTests(unittest.TestCase):
    def test_renders_a4_page_inside_the_fixed_pixel_box(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "page.pdf")
            source.write_bytes(build_minimal_pdf())

            png = render_pdf_first_page(source)

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLessEqual(len(png), DEFAULT_LIMITS.max_output_bytes)

    def test_accepts_a_symlink_to_a_regular_pdf(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "page.pdf")
            source.write_bytes(build_minimal_pdf())
            link = Path(temporary, "linked.pdf")
            link.symlink_to(source)

            self.assertTrue(render_pdf_first_page(link).startswith(b"\x89PNG"))

    def test_rejects_non_regular_empty_oversized_and_malformed_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(PdfPreviewError, "regular"):
                render_pdf_first_page(directory)

            empty = directory / "empty.pdf"
            empty.write_bytes(b"")
            with self.assertRaisesRegex(PdfPreviewError, "empty"):
                render_pdf_first_page(empty)

            oversized = directory / "large.pdf"
            oversized.write_bytes(b"12345")
            with self.assertRaisesRegex(PdfPreviewError, "input size"):
                render_pdf_first_page(
                    oversized,
                    limits=replace(DEFAULT_LIMITS, max_input_bytes=4),
                )

            malformed = directory / "broken.pdf"
            malformed.write_bytes(b"not a PDF")
            with self.assertRaisesRegex(PdfPreviewError, "could not be rendered"):
                render_pdf_first_page(malformed)

    def test_detects_growth_beyond_the_initial_file_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "growing.pdf")
            source.write_bytes(b"12345")
            real_fstat = __import__("os").fstat
            calls = 0

            def report_small_initial_size(descriptor):
                nonlocal calls
                calls += 1
                result = real_fstat(descriptor)
                if calls == 1:
                    values = list(result)
                    values[6] = 1
                    return __import__("os").stat_result(values)
                return result

            with mock.patch(
                "kukni.renderers.pdf.os.fstat",
                side_effect=report_small_initial_size,
            ):
                with self.assertRaisesRegex(PdfPreviewError, "input size"):
                    render_pdf_first_page(
                        source,
                        limits=replace(DEFAULT_LIMITS, max_input_bytes=4),
                    )

    def test_immediate_cancellation_does_not_open_the_path(self):
        with self.assertRaises(PdfPreviewCancelled):
            render_pdf_first_page("does-not-exist.pdf", cancelled=lambda: True)


class PdfLimitTests(unittest.TestCase):
    def test_rejects_non_positive_limits(self):
        with self.assertRaises(ValueError):
            replace(DEFAULT_LIMITS, max_edge_pixels=0)

    def test_rejects_png_dimensions_before_native_decode(self):
        png = (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + (50_000).to_bytes(4, "big")
            + (50_000).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
            + b"\x00\x00\x00\x00"
        )
        with self.assertRaisesRegex(PdfPreviewError, "unsafe dimensions"):
            _validate_png_dimensions(png, 1_800)

    def test_sandbox_exposes_only_runtime_files_and_output_directory(self):
        command = _build_sandbox_command(
            "/usr/bin/bwrap",
            "/tmp/kukni-output",
            ["/usr/bin/pdftoppm", "/proc/self/fd/7", "/output/page"],
        )

        self.assertIn("--unshare-all", command)
        self.assertIn("--die-with-parent", command)
        argument_triples = tuple(zip(command, command[1:], command[2:]))
        self.assertNotIn(("--ro-bind", "/", "/"), argument_triples)
        self.assertIn("/tmp/kukni-output", command)


if __name__ == "__main__":
    unittest.main()
