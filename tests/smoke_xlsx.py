#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Verify a synthetic XLSX becomes a native, keyboard-safe GTK preview."""

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib

from kukni.renderers.registry import RendererRegistry
from kukni.renderers.spreadsheet import SpreadsheetRenderer, XlsxPreviewView
from kukni.session import PreviewState
from kukni.window import PreviewWindow


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPE_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def build_smoke_workbook() -> bytes:
    """Create a minimal workbook without depending on private user files."""

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
            '<sheet name="Kukni smoke" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{PACKAGE_REL_NS}">'
            f'<Relationship Id="rId1" Type="{DOC_REL_NS}/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{DOC_REL_NS}/externalLink" '
            'Target="https://invalid.example/book.xlsx" TargetMode="External"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{MAIN_NS}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Item</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>Result</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Safe preview</t></is></c>'
            '<c r="B2"><f>WEBSERVICE(&quot;https://invalid.example/&quot;)</f>'
            "<v>42</v></c></row>"
            "</sheetData></worksheet>"
        ),
        "xl/vbaProject.bin": b"synthetic macro bytes that must stay inert",
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return output.getvalue()


class XlsxSmokeApplication(Adw.Application):
    def __init__(self, sample: Path) -> None:
        super().__init__(application_id="io.github.lamosty.Kukni.XlsxSmoke")
        self.sample = sample
        self.failures: list[str] = []
        self.window: PreviewWindow | None = None
        self.checks = 0

    def do_activate(self) -> None:
        self.window = PreviewWindow(
            self,
            RendererRegistry((SpreadsheetRenderer(),)),
        )
        self.window.show_file(Gio.File.new_for_path(str(self.sample)))
        GLib.timeout_add(100, self._poll_result)
        GLib.timeout_add_seconds(8, self._watchdog)

    def _poll_result(self) -> bool:
        self.checks += 1
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.checks < 70:
            return GLib.SOURCE_CONTINUE

        if snapshot.state is not PreviewState.PREVIEW:
            self.failures.append(
                f"expected preview, received {snapshot.state.value}: {snapshot.detail}"
            )
            self._finish()
            return GLib.SOURCE_REMOVE

        widget = self.window._stack.get_child_by_name("content")
        if not isinstance(widget, XlsxPreviewView):
            self.failures.append("XLSX renderer did not return its native GTK view")
            self._finish()
            return GLib.SOURCE_REMOVE

        if widget.sheet_name_label.get_label() != "Kukni smoke":
            self.failures.append("worksheet name was not shown")
        if widget.column_view is None:
            self.failures.append("spreadsheet grid was not created")
        else:
            if widget.column_view.get_focusable() or widget.column_view.get_can_focus():
                self.failures.append("spreadsheet grid can steal keyboard navigation")
            if widget.column_view.get_columns().get_n_items() != 3:
                self.failures.append("spreadsheet grid has unexpected columns")

        notices = " ".join(label.get_label() for label in widget.notice_labels)
        for expected in (
            "never run",
            "cached values only",
            "link was ignored",
            "part was ignored",
        ):
            if expected not in notices:
                self.failures.append(f"missing spreadsheet safety notice: {expected}")
        if (
            "WEBSERVICE" in repr(widget.preview)
            or "invalid.example" in repr(widget.preview)
        ):
            self.failures.append(
                "formula source or external target leaked into the view model"
            )

        navigation: list[str] = []
        self.window.connect(
            "navigation-requested",
            lambda _window, direction: navigation.append(direction),
        )
        self.window.activate_action("win.navigate-right", None)
        if navigation != ["right"]:
            self.failures.append("preview navigation action was not preserved")
        if not self.window.get_visible():
            self.failures.append("XLSX preview closed the window")

        self._finish()
        return GLib.SOURCE_REMOVE

    def _finish(self) -> None:
        if self.window is not None:
            self.window.close()
        self.quit()

    def _watchdog(self) -> bool:
        self.failures.append("XLSX smoke test timed out")
        self._finish()
        return GLib.SOURCE_REMOVE


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        sample = Path(temporary, "synthetic.xlsx")
        sample.write_bytes(build_smoke_workbook())
        application = XlsxSmokeApplication(sample)
        exit_code = application.run(["kukni-xlsx-smoke"])
        failures = application.failures

    if exit_code != 0:
        failures.append(f"application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"smoke failure: {failure}", file=sys.stderr)
        return 1
    print("Native XLSX preview smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
