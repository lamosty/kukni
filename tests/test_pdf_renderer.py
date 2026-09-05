# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from dataclasses import replace
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio

from kukni.renderers.pdf import (
    DEFAULT_LIMITS,
    PdfPage,
    PdfPreviewCancelled,
    PdfPreviewError,
    PdfRenderer,
    _PdfPageLoader,
    _build_sandbox_command,
    _parse_page_count,
    _read_output_png,
    _run_pdf_command,
    _validate_png_dimensions,
    pdf_runtime_available,
    pdf_runtime_unavailable_reason,
    render_pdf_page,
    render_pdf_first_page,
    supports_pdf,
)


def build_minimal_pdf(pages: int = 1) -> bytes:
    """Build valid, visibly distinct pages without bundling binary fixtures."""

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (f"<< /Type /Pages /Kids [{' '.join(f'{4 + i * 2} 0 R' for i in range(pages))}] "
         f"/Count {pages} >>").encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i in range(pages):
        width, height = (595, 842) if i % 2 == 0 else (842, 595)
        stream = f"BT /F1 24 Tf 72 400 Td (Kukni PDF page {i + 1}) Tj ET\n".encode("ascii")
        objects.extend((
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + i * 2} 0 R >>").encode("ascii"),
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        ))
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

    def test_renderer_supports_is_pure_type_detection_without_a_runtime_probe(self):
        renderer = PdfRenderer()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.REGULAR)
        info.set_content_type("application/pdf")
        with mock.patch("kukni.renderers.pdf.pdf_runtime_unavailable_reason") as probe:
            self.assertTrue(renderer.supports(Gio.File.new_for_path("/tmp/document.pdf"), info))
            probe.assert_not_called()
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

    def test_lazily_renders_the_requested_second_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "pages.pdf")
            source.write_bytes(build_minimal_pdf(3))
            page = render_pdf_page(source, 2)
        self.assertEqual((page.page_number, page.page_count, page.total_pages), (2, 3, 3))
        width, height = _validate_png_dimensions(page.png, DEFAULT_LIMITS.max_edge_pixels)
        self.assertGreater(width, height)

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



class PdfRuntimeTests(unittest.TestCase):
    def tearDown(self):
        pdf_runtime_unavailable_reason.cache_clear()

    def test_missing_dependency_has_an_actionable_reason(self):
        pdf_runtime_unavailable_reason.cache_clear()
        with mock.patch("kukni.renderers.pdf.shutil.which", side_effect=lambda name: None if name == "pdfinfo" else "/usr/bin/" + name):
            self.assertIn("pdfinfo", pdf_runtime_unavailable_reason())

    def test_sandbox_failure_is_cached_and_fails_closed_before_opening_pdf(self):
        pdf_runtime_unavailable_reason.cache_clear()
        with mock.patch("kukni.renderers.pdf.shutil.which", side_effect=lambda name: "/usr/bin/" + name), mock.patch("kukni.renderers.pdf.probe_bwrap_user_namespace", return_value=False) as probe, mock.patch("kukni.renderers.pdf.os.open") as opened:
            self.assertFalse(pdf_runtime_available())
            with self.assertRaisesRegex(PdfPreviewError, "required PDF sandbox"):
                render_pdf_page("never-opened.pdf", 1)
            self.assertEqual(probe.call_count, 1)
            opened.assert_not_called()


class PdfRequestTests(unittest.TestCase):
    """Deterministic supervisor tests, not evidence that the real sandbox works."""

    def test_invalid_pages_and_cancellation_do_not_probe_or_open(self):
        with mock.patch("kukni.renderers.pdf.pdf_runtime_unavailable_reason") as probe, mock.patch("kukni.renderers.pdf.os.open") as opened:
            for page in (0, -1, DEFAULT_LIMITS.max_pages + 1, 1.5, True):
                with self.subTest(page=page), self.assertRaisesRegex(PdfPreviewError, "page limit"):
                    render_pdf_page("unused.pdf", page)
            with self.assertRaises(PdfPreviewCancelled):
                render_pdf_page("unused.pdf", 1, cancelled=lambda: True)
            probe.assert_not_called()
            opened.assert_not_called()

    def test_metadata_count_is_bounded_and_strict(self):
        self.assertEqual(_parse_page_count(b"Title: hello\nPages:    123\n", 128), 123)
        for data in (b"Pages: 0\n", b"Pages: -2\n", b"Pages: 999999999999999999\n",
                     b"Pages: 2147483648\n", b"Pages: 1\nPages: 2\n", b"Pages: 1oops\n", b"no count"):
            with self.subTest(data=data), self.assertRaises(PdfPreviewError):
                _parse_page_count(data, 128)
        with self.assertRaisesRegex(PdfPreviewError, "metadata exceeds"):
            _parse_page_count(b"Pages: 10\n", 5)

    def test_single_page_commands_share_read_only_snapshot_and_wall_deadline(self):
        from image_fixtures import png
        import os
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "source.pdf")
            source.write_bytes(build_minimal_pdf(3))
            expected = source.read_bytes()

            def run(command, bwrap, limiter, directory, output, descriptor, limits, cancelled, deadline, **kwargs):
                calls.append((command, descriptor, deadline, kwargs))
                os.lseek(descriptor, 0, os.SEEK_SET)
                self.assertEqual(os.read(descriptor, len(expected) + 1), expected)
                with self.assertRaises(OSError):
                    os.write(descriptor, b"alter")
                if command[0].endswith("pdfinfo"):
                    source.write_bytes(b"changed original file")
                    kwargs["stdout"].write(b"Pages: 3\n")
                    kwargs["stdout"].flush()
                else:
                    Path(output, "page.png").write_bytes(png())

            with mock.patch("kukni.renderers.pdf.pdf_runtime_unavailable_reason", return_value=None), mock.patch("kukni.renderers.pdf._run_pdf_command", side_effect=run):
                result = render_pdf_page(source, 2, limits=replace(DEFAULT_LIMITS, max_pages=2))
        self.assertEqual((result.page_number, result.page_count, result.total_pages), (2, 2, 3))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1:3], calls[1][1:3])
        raster = calls[1][0]
        self.assertEqual(raster[raster.index("-f") + 1], "2")
        self.assertEqual(raster[raster.index("-l") + 1], "2")
        self.assertEqual(calls[0][3]["output_limit"], DEFAULT_LIMITS.max_metadata_bytes)
        with self.assertRaises(OSError):
            os.fstat(calls[0][1])

    def test_request_beyond_actual_count_never_starts_rasterizer(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "source.pdf")
            source.write_bytes(build_minimal_pdf())
            def run(*args, **kwargs):
                kwargs["stdout"].write(b"Pages: 1\n")
                kwargs["stdout"].flush()
            with mock.patch("kukni.renderers.pdf.pdf_runtime_unavailable_reason", return_value=None), mock.patch("kukni.renderers.pdf._run_pdf_command", side_effect=run) as run_mock:
                with self.assertRaisesRegex(PdfPreviewError, "not in this document"):
                    render_pdf_page(source, 2)
                self.assertEqual(run_mock.call_count, 1)

    def test_rejects_non_regular_empty_and_oversized_inputs_without_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            empty = directory / "empty.pdf"
            empty.write_bytes(b"")
            oversized = directory / "large.pdf"
            oversized.write_bytes(b"12345")
            with mock.patch("kukni.renderers.pdf.pdf_runtime_unavailable_reason", return_value=None), mock.patch("kukni.renderers.pdf._run_pdf_command") as run:
                for path, reason, limits in (
                    (directory, "regular", DEFAULT_LIMITS),
                    (empty, "empty", DEFAULT_LIMITS),
                    (oversized, "input size", replace(DEFAULT_LIMITS, max_input_bytes=4)),
                ):
                    with self.subTest(path=path), self.assertRaisesRegex(PdfPreviewError, reason):
                        render_pdf_page(path, 1, limits=limits)
                run.assert_not_called()

    @mock.patch("kukni.renderers.pdf.pdf_runtime_unavailable_reason", return_value=None)
    def test_detects_growth_beyond_the_initial_file_size(self, _runtime):
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

    def test_preview_fifo_and_symlink_cannot_block_or_redirect_output_read(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            fifo = Path(temporary, "fifo")
            os.mkfifo(fifo)
            with self.assertRaisesRegex(PdfPreviewError, "invalid preview"):
                _read_output_png(str(fifo), 1024)
            target = Path(temporary, "target")
            target.write_bytes(b"pixels")
            link = Path(temporary, "link")
            link.symlink_to(target)
            with self.assertRaises(PdfPreviewError):
                _read_output_png(str(link), 1024)

    def test_metadata_child_has_same_sandbox_and_resource_limits(self):
        process = mock.Mock(returncode=0)
        process.poll.return_value = 0
        with mock.patch("kukni.renderers.pdf.subprocess.Popen", return_value=process) as popen:
            _run_pdf_command(
                ["/usr/bin/pdfinfo", "/proc/self/fd/7"], "/usr/bin/bwrap", "/usr/bin/prlimit",
                "/tmp/work", "/tmp/work/output", 7, DEFAULT_LIMITS, None,
                time.monotonic() + 5, output_limit=4096,
            )
        command = popen.call_args.args[0]
        self.assertIn("--unshare-all", command)
        self.assertIn("--fsize=4096", command)
        self.assertIn(f"--as={DEFAULT_LIMITS.max_address_space_bytes}", command)
        self.assertIn(f"--cpu={DEFAULT_LIMITS.max_cpu_seconds}", command)
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (7,))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_expired_wall_budget_prevents_worker_launch(self):
        with mock.patch("kukni.renderers.pdf.subprocess.Popen") as popen:
            with self.assertRaisesRegex(PdfPreviewError, "timed out"):
                _run_pdf_command(["pdfinfo"], "bwrap", "prlimit", "/tmp", "/tmp/output", 7, DEFAULT_LIMITS, None, time.monotonic() - 1)
            popen.assert_not_called()

    def test_cancelled_running_child_is_terminated(self):
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch("kukni.renderers.pdf.subprocess.Popen", return_value=process), mock.patch("kukni.renderers.pdf._check_request", side_effect=[None, PdfPreviewCancelled()]), mock.patch("kukni.renderers.pdf.terminate_process_group") as terminate:
            with self.assertRaises(PdfPreviewCancelled):
                _run_pdf_command(["pdfinfo"], "bwrap", "prlimit", "/tmp", "/tmp/output", 7, DEFAULT_LIMITS, None, time.monotonic() + 5)
            terminate.assert_called_once_with(process)


class PdfPageLoaderTests(unittest.TestCase):
    def setUp(self):
        self.deliveries = []
        self.threads = []
        real_thread = threading.Thread
        def new_thread(**kwargs):
            thread = real_thread(**kwargs)
            self.threads.append(thread)
            return thread
        self.patches = [
            mock.patch("kukni.renderers.pdf.GLib.idle_add", side_effect=lambda callback: self.deliveries.append(callback)),
            mock.patch("kukni.renderers.pdf.threading.Thread", side_effect=new_thread),
        ]
        for patch in self.patches:
            patch.start()
        self.cancellable = Gio.Cancellable()
        self.loader = _PdfPageLoader("sample.pdf", self.cancellable)
        self.pages, self.errors = [], []

    def tearDown(self):
        self.cancellable.cancel()
        self.join_workers()
        for patch in reversed(self.patches):
            patch.stop()

    def join_workers(self):
        for thread in self.threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "PDF worker did not terminate")

    def drain(self):
        self.join_workers()
        for callback in self.deliveries:
            callback()
        self.deliveries.clear()

    def test_rapid_requests_coalesce_and_cancel_stale_page(self):
        entered, release = threading.Event(), threading.Event()
        rendered = []
        def render(path, page, *, cancelled, limits):
            rendered.append(page)
            if page == 1:
                entered.set()
                self.assertTrue(release.wait(2))
                self.assertTrue(cancelled())
                raise PdfPreviewCancelled()
            return PdfPage(b"pixels", page, 3, 3)
        with mock.patch("kukni.renderers.pdf.render_pdf_page", side_effect=render):
            self.loader.request(1, self.pages.append, self.errors.append)
            self.assertTrue(entered.wait(2))
            self.loader.request(2, self.pages.append, self.errors.append)
            self.loader.request(3, self.pages.append, self.errors.append)
            release.set()
            self.drain()
        self.assertEqual(rendered, [1, 3])
        self.assertEqual([page.page_number for page in self.pages], [3])
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(self.errors, [])

    def test_already_queued_stale_delivery_is_suppressed(self):
        with mock.patch("kukni.renderers.pdf.render_pdf_page", side_effect=lambda path, page, **kw: PdfPage(b"pixels", page, 3, 3)):
            self.loader.request(1, self.pages.append, self.errors.append)
            self.join_workers()
            self.loader.request(2, self.pages.append, self.errors.append)
            self.drain()
        self.assertEqual([page.page_number for page in self.pages], [2])

    def test_gtk_backlog_retains_only_one_latest_page_payload(self):
        with mock.patch("kukni.renderers.pdf.render_pdf_page", side_effect=lambda path, page, **kw: PdfPage(b"pixels", page, 20, 20)):
            for page in range(1, 21):
                self.loader.request(page, self.pages.append, self.errors.append)
                self.join_workers()
            self.assertEqual(len(self.deliveries), 1)
            self.assertEqual(self.loader._delivery[2].page_number, 20)
            self.drain()
        self.assertEqual([page.page_number for page in self.pages], [20])

    def test_session_cancel_suppresses_delivery(self):
        with mock.patch("kukni.renderers.pdf.render_pdf_page", return_value=PdfPage(b"pixels", 1, 3, 3)):
            self.loader.request(1, self.pages.append, self.errors.append)
            self.join_workers()
            self.cancellable.cancel()
            self.drain()
        self.assertEqual(self.pages, [])
        self.assertEqual(self.errors, [])

    def test_failure_delivers_one_error(self):
        with mock.patch("kukni.renderers.pdf.render_pdf_page", side_effect=PdfPreviewError("document rejected")):
            self.loader.request(1, self.pages.append, self.errors.append)
            self.drain()
        self.assertEqual(self.errors, ["document rejected"])

    def test_latest_file_waits_for_cancelled_workers_instead_of_falling_back(self):
        slots = threading.BoundedSemaphore(2)
        slots.acquire()
        slots.acquire()
        waiting = threading.Event()
        real_acquire = slots.acquire
        def acquire(**kwargs):
            waiting.set()
            return real_acquire(**kwargs)
        latest = _PdfPageLoader("latest.pdf", Gio.Cancellable())
        rendered = []
        def render(path, page, *, cancelled, limits):
            self.assertFalse(cancelled())
            self.assertGreater(limits.wall_timeout_seconds, 0)
            self.assertLessEqual(limits.wall_timeout_seconds, DEFAULT_LIMITS.wall_timeout_seconds)
            rendered.append(path)
            return PdfPage(b"pixels", page, 3, 3)
        with mock.patch("kukni.renderers.pdf._WORKER_SLOTS", slots), mock.patch.object(slots, "acquire", side_effect=acquire), mock.patch("kukni.renderers.pdf.render_pdf_page", side_effect=render):
            try:
                self.loader.request(1, self.pages.append, self.errors.append)
                self.assertTrue(waiting.wait(2))
                self.cancellable.cancel()
                waiting.clear()
                latest.request(2, self.pages.append, self.errors.append)
                self.assertTrue(waiting.wait(2))
                self.assertEqual(rendered, [])
            finally:
                slots.release()
                slots.release()
            self.drain()
        self.assertEqual(rendered, ["latest.pdf"])
        self.assertEqual([page.page_number for page in self.pages], [2])
        self.assertEqual(self.errors, [])
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))
        slots.release()
        slots.release()

    def test_cancelled_admission_exits_without_waiting_for_any_slot(self):
        slots = threading.BoundedSemaphore(2)
        slots.acquire()
        slots.acquire()
        waiting = threading.Event()
        real_acquire = slots.acquire
        def acquire(**kwargs):
            waiting.set()
            return real_acquire(**kwargs)
        with mock.patch("kukni.renderers.pdf._WORKER_SLOTS", slots), mock.patch.object(slots, "acquire", side_effect=acquire), mock.patch("kukni.renderers.pdf.render_pdf_page") as render:
            try:
                self.loader.request(1, self.pages.append, self.errors.append)
                self.assertTrue(waiting.wait(2))
                self.cancellable.cancel()
                self.drain()
                render.assert_not_called()
                self.assertEqual(self.pages, [])
                self.assertEqual(self.errors, [])
            finally:
                slots.release()
                slots.release()

    def test_admission_consumes_the_same_wall_budget_as_rendering(self):
        slots = threading.BoundedSemaphore(2)
        slots.acquire()
        slots.acquire()
        with mock.patch("kukni.renderers.pdf._WORKER_SLOTS", slots), mock.patch("kukni.renderers.pdf.DEFAULT_LIMITS", replace(DEFAULT_LIMITS, wall_timeout_seconds=0.05)), mock.patch("kukni.renderers.pdf.render_pdf_page") as render:
            try:
                self.loader.request(1, self.pages.append, self.errors.append)
                self.drain()
                render.assert_not_called()
            finally:
                slots.release()
                slots.release()
        self.assertEqual(self.errors, ["PDF preview timed out"])


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
