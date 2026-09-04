# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from dataclasses import replace
from io import BytesIO
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.renderers.xlsx import (
    DEFAULT_LIMITS,
    XlsxPreviewCancelled,
    XlsxPreviewError,
    build_xlsx_html,
    parse_xlsx,
    render_xlsx,
    supports_xlsx,
)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPE_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def _worksheet(rows: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{MAIN_NS}"><sheetData>{rows}</sheetData></worksheet>'
    )


def _workbook_bytes(
    *,
    sheet_xml: str | None = None,
    shared_strings: tuple[str, ...] = (),
    sheet_target: str = "worksheets/sheet1.xml",
    sheet_target_mode: str = "",
    sheets_xml: str | None = None,
    extra_relationships: str = "",
    extra_members: dict[str, bytes | str] | None = None,
    content_type_declaration: str | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    if sheet_xml is None:
        sheet_xml = _worksheet('<row r="1"><c r="A1"><v>7</v></c></row>')
    shared_relation = ""
    shared_member: str | None = None
    if shared_strings:
        shared_relation = (
            f'<Relationship Id="rId2" Type="{DOC_REL_NS}/sharedStrings" '
            'Target="sharedStrings.xml"/>'
        )
        items = "".join(f"<si><t>{value}</t></si>" for value in shared_strings)
        shared_member = f'<sst xmlns="{MAIN_NS}">{items}</sst>'
    target_mode = (
        f' TargetMode="{sheet_target_mode}"' if sheet_target_mode else ""
    )
    if sheets_xml is None:
        sheets_xml = '<sheet name="Overview &amp; notes" sheetId="1" r:id="rId1"/>'
    if content_type_declaration is None:
        content_type_declaration = (
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet.main+xml"/>'
        )

    members: dict[str, bytes | str] = {
        "[Content_Types].xml": (
            f'<Types xmlns="{CONTENT_TYPE_NS}">'
            f"{content_type_declaration}"
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
            f"{sheets_xml}</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{PACKAGE_REL_NS}">'
            f'<Relationship Id="rId1" Type="{DOC_REL_NS}/worksheet" '
            f'Target="{sheet_target}"{target_mode}/>'
            f"{shared_relation}{extra_relationships}</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    if shared_member is not None:
        members["xl/sharedStrings.xml"] = shared_member
    members.update(extra_members or {})

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return output.getvalue()


class XlsxRendererTests(unittest.TestCase):
    def _write_workbook(self, directory: Path, data: bytes) -> Path:
        path = directory / "synthetic.xlsx"
        path.write_bytes(data)
        return path

    def test_detects_xlsx_without_claiming_other_office_formats(self):
        self.assertTrue(supports_xlsx("report.XLSX", None))
        self.assertTrue(
            supports_xlsx(
                None,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )
        self.assertFalse(supports_xlsx("report.xls", "application/vnd.ms-excel"))
        self.assertFalse(supports_xlsx("report.xlsm", None))

    def test_renders_values_as_inert_escaped_html_and_ignores_formula_source(self):
        rows = (
            '<row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="inlineStr"><is><t>Line one</t><r><t> + two</t></r></is></c>'
            '<c r="D1" t="b"><v>1</v></c>'
            "</row>"
            '<row r="2"><c r="A2"><f>WEBSERVICE(&quot;https://attacker.invalid/&quot;)'
            "</f><v>42</v></c></row>"
        )
        external_relation = (
            f'<Relationship Id="rId3" Type="{DOC_REL_NS}/externalLink" '
            'Target="https://attacker.invalid/workbook.xlsx" TargetMode="External"/>'
        )
        data = _workbook_bytes(
            sheet_xml=_worksheet(rows),
            shared_strings=(
                "Hello &amp; goodbye",
                "&lt;script&gt;alert(1)&lt;/script&gt;",
            ),
            extra_relationships=external_relation,
            extra_members={"xl/vbaProject.bin": b"never interpreted"},
        )

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = self._write_workbook(directory, data)
            result = render_xlsx(path)

            self.assertEqual(result.preview.sheet_name, "Overview & notes")
            self.assertEqual(result.preview.rows[0].cells[0].value, "Hello & goodbye")
            self.assertEqual(result.preview.rows[0].cells[2].value, "Line one + two")
            self.assertEqual(result.preview.rows[0].cells[3].value, "TRUE")
            self.assertEqual(result.preview.rows[1].cells[0].value, "42")
            self.assertEqual(result.preview.formula_cells, 1)
            self.assertEqual(result.preview.external_relationships_ignored, 1)
            self.assertEqual(result.preview.active_parts_ignored, 1)
            self.assertIn("default-src &#39;none&#39;", result.html)
            self.assertIn("Hello &amp; goodbye", result.html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", result.html)
            self.assertNotIn("<script>alert", result.html)
            self.assertNotIn("WEBSERVICE", result.html)
            self.assertNotIn("attacker.invalid", result.html)
            self.assertIn("formulas were not evaluated", result.html)
            self.assertEqual([item.name for item in directory.iterdir()], [path.name])

    def test_prefers_the_first_visible_sheet(self):
        data = _workbook_bytes(
            sheets_xml=(
                '<sheet name="Hidden" sheetId="1" state="hidden" r:id="rId1"/>'
                '<sheet name="Visible" sheetId="2" r:id="rId2"/>'
            ),
            extra_relationships=(
                f'<Relationship Id="rId2" Type="{DOC_REL_NS}/worksheet" '
                'Target="worksheets/sheet2.xml"/>'
            ),
            extra_members={
                "xl/worksheets/sheet2.xml": _worksheet(
                    '<row r="1"><c r="A1"><v>99</v></c></row>'
                )
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            preview = parse_xlsx(path)

        self.assertEqual(preview.sheet_name, "Visible")
        self.assertEqual(preview.rows[0].cells[0].value, "99")

    def test_accepts_a_default_workbook_content_type_declaration(self):
        declaration = (
            '<Default Extension="XML" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        )
        data = _workbook_bytes(content_type_declaration=declaration)

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            self.assertEqual(parse_xlsx(path).rows[0].cells[0].value, "7")

    def test_override_takes_precedence_and_duplicate_declarations_fail(self):
        valid_default = (
            '<Default Extension="xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        )
        wrong_override = (
            '<Override PartName="/xl/workbook.xml" ContentType="text/plain"/>'
        )
        declarations = (
            valid_default + wrong_override,
            valid_default + valid_default,
            '<Default Extension="xml" ContentType="text/plain"/>',
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                data = _workbook_bytes(content_type_declaration=declaration)
                with tempfile.TemporaryDirectory() as temporary:
                    path = self._write_workbook(Path(temporary), data)
                    with self.assertRaises(XlsxPreviewError):
                        parse_xlsx(path)

    def test_preview_limits_rows_columns_cells_and_text(self):
        rows = (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>abcdefgh</t></is></c>'
            '<c r="B1"><v>2</v></c><c r="C1"><v>3</v></c></row>'
            '<row r="2"><c r="A2"><v>4</v></c></row>'
        )
        data = _workbook_bytes(sheet_xml=_worksheet(rows))
        limits = replace(
            DEFAULT_LIMITS,
            max_rows=1,
            max_columns=2,
            max_cells=10,
            max_cell_characters=6,
            max_output_characters=20,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            preview = parse_xlsx(path, limits)

        self.assertEqual(len(preview.rows), 1)
        self.assertEqual(preview.rows[0].cells[0].value, "abcde…")
        self.assertEqual(preview.column_count, 2)
        self.assertEqual(
            preview.truncation_reasons,
            ("row limit", "column limit", "cell text limit"),
        )

    def test_stops_at_global_cell_and_output_limits(self):
        rows = (
            '<row r="1"><c r="A1"><v>abcdef</v></c>'
            '<c r="B1"><v>second</v></c></row>'
        )
        data = _workbook_bytes(sheet_xml=_worksheet(rows))
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            output_limited = parse_xlsx(
                path, replace(DEFAULT_LIMITS, max_output_characters=5)
            )
            cell_limited = parse_xlsx(path, replace(DEFAULT_LIMITS, max_cells=1))

        self.assertEqual(output_limited.rows[0].cells[0].value, "abcd…")
        self.assertIn("text output limit", output_limited.truncation_reasons)
        self.assertEqual(len(cell_limited.rows[0].cells), 1)
        self.assertIn("cell limit", cell_limited.truncation_reasons)

    def test_rejects_external_or_escaping_worksheet_relationships(self):
        fixtures = (
            (
                _workbook_bytes(
                    sheet_target="https://attacker.invalid/sheet.xml",
                    sheet_target_mode="External",
                ),
                "package",
            ),
            (_workbook_bytes(sheet_target="../../outside.xml"), "package"),
            (_workbook_bytes(sheet_target="http://["), "unsafe target"),
        )
        for data, message in fixtures:
            with self.subTest(data_length=len(data), message=message):
                with tempfile.TemporaryDirectory() as temporary:
                    path = self._write_workbook(Path(temporary), data)
                    with self.assertRaisesRegex(XlsxPreviewError, message):
                        parse_xlsx(path)

    def test_rejects_unsafe_archive_member_paths(self):
        data = _workbook_bytes(extra_members={"../escape.xml": "not extracted"})
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            with self.assertRaisesRegex(XlsxPreviewError, "unsafe member path"):
                parse_xlsx(path)

    def test_rejects_a_raw_nul_before_zipfile_can_truncate_the_name(self):
        original_name = b"xl/unusedXxml"
        data = bytearray(_workbook_bytes(extra_members={original_name.decode(): "x"}))
        self.assertEqual(data.count(original_name), 2)
        for _occurrence in range(2):
            position = data.find(original_name)
            data[position + len(b"xl/unused")] = 0

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), bytes(data))
            with self.assertRaisesRegex(XlsxPreviewError, "unsafe member path"):
                parse_xlsx(path)

    def test_rejects_active_xml_declarations(self):
        sheet = (
            '<!DOCTYPE worksheet [<!ENTITY payload "expanded">]>'
            f'<worksheet xmlns="{MAIN_NS}"><sheetData><row r="1">'
            '<c r="A1" t="inlineStr"><is><t>&payload;</t></is></c>'
            "</row></sheetData></worksheet>"
        )
        data = _workbook_bytes(sheet_xml=sheet)
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            with self.assertRaisesRegex(XlsxPreviewError, "active XML"):
                parse_xlsx(path)

    def test_normalizes_unknown_xml_encodings(self):
        sheet = (
            '<?xml version="1.0" encoding="not-a-real-encoding"?>'
            f'<worksheet xmlns="{MAIN_NS}"><sheetData/></worksheet>'
        )
        data = _workbook_bytes(sheet_xml=sheet)
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            with self.assertRaisesRegex(XlsxPreviewError, "malformed"):
                parse_xlsx(path)

    def test_normalizes_corrupt_deflate_streams(self):
        data = bytearray(_workbook_bytes())
        with zipfile.ZipFile(BytesIO(data)) as archive:
            info = archive.getinfo("xl/worksheets/sheet1.xml")
        name_size, extra_size = struct.unpack_from("<HH", data, info.header_offset + 26)
        payload_offset = info.header_offset + 30 + name_size + extra_size
        data[payload_offset] ^= 0xFF

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), bytes(data))
            with self.assertRaises(XlsxPreviewError):
                parse_xlsx(path)

    def test_rejects_archive_and_member_resource_limit_violations(self):
        data = _workbook_bytes(extra_members={"xl/unused.xml": "A" * 20_000})
        cases = (
            (
                replace(DEFAULT_LIMITS, max_input_bytes=len(data) - 1),
                "input size limit",
            ),
            (replace(DEFAULT_LIMITS, max_members=5), "too many members"),
            (
                replace(DEFAULT_LIMITS, max_central_directory_bytes=1),
                "index is too large",
            ),
            (
                replace(DEFAULT_LIMITS, max_member_compressed_bytes=1),
                "compressed spreadsheet member is too large",
            ),
            (
                replace(DEFAULT_LIMITS, max_member_uncompressed_bytes=1_000),
                "expands beyond",
            ),
            (
                replace(DEFAULT_LIMITS, max_total_uncompressed_bytes=1_000),
                "package expands beyond",
            ),
            (
                replace(DEFAULT_LIMITS, max_compression_ratio=5.0),
                "unsafe compression ratio",
            ),
            (
                replace(
                    DEFAULT_LIMITS,
                    max_xml_bytes=100,
                    max_compression_ratio=10_000.0,
                ),
                "XML size limit",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            for limits, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(XlsxPreviewError, message):
                        parse_xlsx(path, limits)

    def test_rejects_shared_string_resource_limit_violations(self):
        data = _workbook_bytes(
            shared_strings=("first", "a deliberately longer second value")
        )
        cases = (
            (replace(DEFAULT_LIMITS, max_shared_strings=1), "too many shared strings"),
            (
                replace(DEFAULT_LIMITS, max_shared_string_characters=10),
                "shared-string table is too large",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            for limits, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(XlsxPreviewError, message):
                        parse_xlsx(path, limits)

    def test_preflights_the_actual_central_directory_member_count(self):
        data = bytearray(_workbook_bytes())
        eocd = data.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(eocd, 0)
        struct.pack_into("<H", data, eocd + 8, 1)
        struct.pack_into("<H", data, eocd + 10, 1)

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), bytes(data))
            with self.assertRaisesRegex(XlsxPreviewError, "index is inconsistent"):
                parse_xlsx(path)

    def test_rejects_non_regular_files_but_accepts_regular_symbolic_links(self):
        data = _workbook_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(XlsxPreviewError, "regular file"):
                parse_xlsx(directory)

            path = self._write_workbook(directory, data)
            link = directory / "linked.xlsx"
            link.symlink_to(path)
            self.assertEqual(parse_xlsx(link).rows[0].cells[0].value, "7")

    def test_cancellation_is_distinct_and_checked_during_work(self):
        rows = "".join(
            f'<row r="{row}"><c r="A{row}"><v>{row}</v></c></row>'
            for row in range(1, 151)
        )
        data = _workbook_bytes(sheet_xml=_worksheet(rows))
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 30

        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            with self.assertRaises(XlsxPreviewCancelled):
                render_xlsx(path, cancelled=cancelled)
        self.assertGreaterEqual(checks, 30)

    def test_immediate_cancellation_does_not_open_the_path(self):
        with self.assertRaises(XlsxPreviewCancelled):
            parse_xlsx("does-not-exist.xlsx", cancelled=lambda: True)

    def test_rejects_an_html_document_that_exceeds_the_final_output_limit(self):
        data = _workbook_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_workbook(Path(temporary), data)
            preview = parse_xlsx(path)
        with self.assertRaisesRegex(XlsxPreviewError, "output limit"):
            build_xlsx_html(
                preview,
                replace(DEFAULT_LIMITS, max_html_characters=100),
            )


if __name__ == "__main__":
    unittest.main()
