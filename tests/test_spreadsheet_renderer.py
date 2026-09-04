# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio, GLib

from kukni.renderers.spreadsheet import (
    SpreadsheetRenderer,
    XlsxPreviewView,
    column_label,
    preview_subtitle,
    safety_notices,
)
from kukni.renderers.xlsx import (
    PreviewCell,
    PreviewRow,
    XlsxPreview,
    XlsxPreviewCancelled,
    XlsxPreviewError,
)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPE_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def build_synthetic_workbook() -> bytes:
    """Return a tiny inert fixture with formula, link, and macro metadata."""

    members: dict[str, bytes | str] = {
        "[Content_Types].xml": (
            f'<Types xmlns="{CONTENT_TYPE_NS}">'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{PACKAGE_REL_NS}">'
            f'<Relationship Id="root" Type="{DOC_REL_NS}/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN_NS}" xmlns:r="{DOC_REL_NS}"><sheets>'
            '<sheet name="Overview" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{PACKAGE_REL_NS}">'
            f'<Relationship Id="rId1" Type="{DOC_REL_NS}/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{DOC_REL_NS}/externalLink" '
            'Target="https://invalid.example/workbook.xlsx" TargetMode="External"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{MAIN_NS}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>Value</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Kukni</t></is></c>'
            '<c r="B2"><f>WEBSERVICE(&quot;https://invalid.example/&quot;)</f>'
            "<v>42</v></c></row>"
            "</sheetData></worksheet>"
        ),
        "xl/vbaProject.bin": b"synthetic active content that is never interpreted",
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return output.getvalue()


def sample_preview(**changes) -> XlsxPreview:
    values = {
        "sheet_name": "Overview",
        "rows": (
            PreviewRow(
                number=1,
                cells=(PreviewCell(column=1, value="Kukni"),),
            ),
        ),
        "column_count": 1,
        "truncation_reasons": (),
        "formula_cells": 0,
        "external_relationships_ignored": 0,
        "active_parts_ignored": 0,
    }
    values.update(changes)
    return XlsxPreview(**values)


class QueuedIdleCalls:
    """Capture main-context deliveries without running a nested GLib loop."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []
        self.queued = threading.Event()

    def __call__(self, callback, *arguments):
        self.calls.append((callback, arguments))
        self.queued.set()
        return len(self.calls)

    def run_next(self):
        callback, arguments = self.calls.pop(0)
        if not self.calls:
            self.queued.clear()
        return callback(*arguments)


class PresentationTextTests(unittest.TestCase):
    def test_column_labels_cover_excel_style_boundaries(self):
        self.assertEqual(column_label(1), "A")
        self.assertEqual(column_label(26), "Z")
        self.assertEqual(column_label(27), "AA")
        self.assertEqual(column_label(50), "AX")
        with self.assertRaises(ValueError):
            column_label(0)

    def test_safety_notices_disclose_inert_and_limited_features(self):
        preview = sample_preview(
            formula_cells=2,
            external_relationships_ignored=1,
            active_parts_ignored=3,
            truncation_reasons=("row limit", "cell text limit"),
        )

        notices = safety_notices(preview)

        self.assertIn("never run", notices[0])
        self.assertIn("2 formula cells", notices[1])
        self.assertIn("cached values only", notices[1])
        self.assertIn("1 external workbook link was ignored", notices[2])
        self.assertIn("3 embedded active-content parts were ignored", notices[3])
        self.assertEqual(
            notices[4],
            "Preview limited by row limit, cell text limit.",
        )
        joined = " ".join(notices)
        self.assertNotIn("WEBSERVICE", joined)
        self.assertNotIn("https://", joined)

    def test_subtitle_is_stable_and_marks_truncation(self):
        self.assertEqual(
            preview_subtitle(sample_preview()),
            "Spreadsheet · 1 visible row",
        )
        limited = sample_preview(rows=(), truncation_reasons=("row limit",))
        self.assertEqual(
            preview_subtitle(limited),
            "Spreadsheet · 0 visible rows · limited",
        )


class RendererCapabilityTests(unittest.TestCase):
    @staticmethod
    def file_info(content_type: str, file_type=Gio.FileType.REGULAR):
        info = Gio.FileInfo()
        info.set_file_type(file_type)
        info.set_content_type(content_type)
        return info

    def test_accepts_only_regular_local_xlsx_candidates(self):
        renderer = SpreadsheetRenderer()
        xlsx_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        self.assertTrue(
            renderer.supports(
                Gio.File.new_for_path("/tmp/report.bin"),
                self.file_info(xlsx_type),
            )
        )
        self.assertTrue(
            renderer.supports(
                Gio.File.new_for_path("/tmp/REPORT.XLSX"),
                self.file_info("application/octet-stream"),
            )
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_path("/tmp/report.xls"),
                self.file_info("application/vnd.ms-excel"),
            )
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_path("/tmp/report.xlsx"),
                self.file_info(xlsx_type, Gio.FileType.DIRECTORY),
            )
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_uri("https://invalid.example/report.xlsx"),
                self.file_info(xlsx_type),
            )
        )


class RendererWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = SpreadsheetRenderer()
        self.info = Gio.FileInfo()
        self.info.set_file_type(Gio.FileType.REGULAR)
        self.info.set_content_type(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    def test_parses_a_synthetic_workbook_in_a_daemon_worker(self):
        idle = QueuedIdleCalls()
        ready = mock.Mock()
        failed = mock.Mock()
        worker_details: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "synthetic.xlsx")
            path.write_bytes(build_synthetic_workbook())
            real_parse = __import__(
                "kukni.renderers.spreadsheet",
                fromlist=["parse_xlsx"],
            ).parse_xlsx

            def record_parse(*arguments, **keywords):
                current = threading.current_thread()
                worker_details.update(name=current.name, daemon=current.daemon)
                return real_parse(*arguments, **keywords)

            captured: dict[str, XlsxPreview] = {}

            def make_view(preview):
                captured["preview"] = preview
                return object()

            with (
                mock.patch(
                    "kukni.renderers.spreadsheet.parse_xlsx",
                    side_effect=record_parse,
                ),
                mock.patch(
                    "kukni.renderers.spreadsheet.GLib.idle_add",
                    side_effect=idle,
                ),
                mock.patch(
                    "kukni.renderers.spreadsheet.XlsxPreviewView",
                    side_effect=make_view,
                ),
            ):
                self.renderer.render(
                    Gio.File.new_for_path(str(path)),
                    self.info,
                    Gio.Cancellable(),
                    ready,
                    failed,
                )
                self.assertTrue(idle.queued.wait(3), "worker did not settle")
                self.assertFalse(ready.called)
                self.assertFalse(failed.called)
                self.assertEqual(idle.run_next(), GLib.SOURCE_REMOVE)

        self.assertEqual(worker_details["name"], "kukni-xlsx-parser")
        self.assertTrue(worker_details["daemon"])
        preview = captured["preview"]
        self.assertEqual(preview.rows[1].cells[1].value, "42")
        self.assertEqual(preview.formula_cells, 1)
        self.assertEqual(preview.external_relationships_ignored, 1)
        self.assertEqual(preview.active_parts_ignored, 1)
        self.assertNotIn("WEBSERVICE", repr(preview))
        self.assertNotIn("invalid.example", repr(preview))
        ready.assert_called_once()
        self.assertEqual(ready.call_args.args[1], "Spreadsheet · 2 visible rows")
        failed.assert_not_called()

    def test_parser_errors_reach_error_callback_only_via_main_context(self):
        idle = QueuedIdleCalls()
        ready = mock.Mock()
        failed = mock.Mock()
        with (
            mock.patch(
                "kukni.renderers.spreadsheet.parse_xlsx",
                side_effect=XlsxPreviewError("Synthetic workbook rejected"),
            ),
            mock.patch(
                "kukni.renderers.spreadsheet.GLib.idle_add",
                side_effect=idle,
            ),
        ):
            self.renderer.render(
                Gio.File.new_for_path("/tmp/synthetic.xlsx"),
                self.info,
                Gio.Cancellable(),
                ready,
                failed,
            )
            self.assertTrue(idle.queued.wait(3), "worker did not report failure")
            failed.assert_not_called()
            self.assertEqual(idle.run_next(), GLib.SOURCE_REMOVE)

        ready.assert_not_called()
        failed.assert_called_once_with("Synthetic workbook rejected")

    def test_cancellation_during_parse_settles_silently(self):
        started = threading.Event()
        completed = threading.Event()
        release = threading.Event()
        idle = QueuedIdleCalls()
        cancellable = Gio.Cancellable()

        def cancelled_parse(_path, *, cancelled):
            started.set()
            release.wait(3)
            try:
                self.assertTrue(cancelled())
                raise XlsxPreviewCancelled("cancelled")
            finally:
                completed.set()

        with (
            mock.patch(
                "kukni.renderers.spreadsheet.parse_xlsx",
                side_effect=cancelled_parse,
            ),
            mock.patch(
                "kukni.renderers.spreadsheet.GLib.idle_add",
                side_effect=idle,
            ),
        ):
            ready = mock.Mock()
            failed = mock.Mock()
            self.renderer.render(
                Gio.File.new_for_path("/tmp/synthetic.xlsx"),
                self.info,
                cancellable,
                ready,
                failed,
            )
            self.assertTrue(started.wait(3), "worker did not start")
            cancellable.cancel()
            release.set()
            self.assertTrue(completed.wait(3), "worker did not observe cancellation")

        self.assertEqual(idle.calls, [])
        ready.assert_not_called()
        failed.assert_not_called()

    def test_cancellation_discards_an_already_queued_result(self):
        idle = QueuedIdleCalls()
        cancellable = Gio.Cancellable()
        ready = mock.Mock()
        failed = mock.Mock()
        with (
            mock.patch(
                "kukni.renderers.spreadsheet.parse_xlsx",
                return_value=sample_preview(),
            ),
            mock.patch(
                "kukni.renderers.spreadsheet.GLib.idle_add",
                side_effect=idle,
            ),
            mock.patch("kukni.renderers.spreadsheet.XlsxPreviewView") as view,
        ):
            self.renderer.render(
                Gio.File.new_for_path("/tmp/synthetic.xlsx"),
                self.info,
                cancellable,
                ready,
                failed,
            )
            self.assertTrue(idle.queued.wait(3), "worker did not queue result")
            cancellable.cancel()
            self.assertEqual(idle.run_next(), GLib.SOURCE_REMOVE)

        view.assert_not_called()
        ready.assert_not_called()
        failed.assert_not_called()

    def test_direct_remote_render_queues_a_main_context_error(self):
        idle = QueuedIdleCalls()
        failed = mock.Mock()
        with mock.patch(
            "kukni.renderers.spreadsheet.GLib.idle_add",
            side_effect=idle,
        ):
            self.renderer.render(
                Gio.File.new_for_uri("https://invalid.example/report.xlsx"),
                self.info,
                Gio.Cancellable(),
                mock.Mock(),
                failed,
            )
            failed.assert_not_called()
            self.assertEqual(idle.run_next(), GLib.SOURCE_REMOVE)

        failed.assert_called_once_with("Spreadsheet preview supports local files only")


if __name__ == "__main__":
    unittest.main()
