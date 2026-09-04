# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded, static previews for Office Open XML spreadsheets.

This module deliberately implements only the small, declarative subset of XLSX
needed for a useful first-sheet preview.  It never launches an office suite,
extracts archive members, evaluates formulas, follows relationships outside the
package, or performs network access.

The parser is synchronous and toolkit-independent.  Callers should run
``render_xlsx`` away from the GTK main thread, then give the returned static HTML
to Kukni's locked-down HTML view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import html
import io
import os
from pathlib import Path
import posixpath
import re
import stat
import struct
from typing import BinaryIO, Callable, Iterable
from urllib.parse import urlsplit
import zipfile
import zlib


XLSX_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    }
)

_WORKBOOK_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    }
)
_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_EOCD = struct.Struct("<4s4H2LH")
_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_FILE_SIGNATURE = b"PK\x01\x02"
_CENTRAL_FILE_HEADER_BYTES = 46
_MAX_ZIP_COMMENT_BYTES = (1 << 16) - 1
_CELL_REFERENCE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)$")
_ACTIVE_PART_PREFIXES = (
    "customui/",
    "xl/activex/",
    "xl/embeddings/",
)
_ACTIVE_PART_NAMES = frozenset(
    {
        "xl/vbaproject.bin",
        "xl/vbasignature.bin",
    }
)


class XlsxPreviewError(ValueError):
    """The workbook cannot be previewed within Kukni's safety boundary."""


class XlsxPreviewCancelled(Exception):
    """Parsing stopped because the owning preview request was cancelled."""


CancellationCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class XlsxLimits:
    """Independent resource ceilings for untrusted spreadsheet packages."""

    max_input_bytes: int = 64 * 1024 * 1024
    max_members: int = 2_048
    max_member_name_characters: int = 512
    max_central_directory_bytes: int = 8 * 1024 * 1024
    max_member_compressed_bytes: int = 32 * 1024 * 1024
    max_member_uncompressed_bytes: int = 32 * 1024 * 1024
    max_total_uncompressed_bytes: int = 96 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_xml_bytes: int = 16 * 1024 * 1024
    max_xml_elements: int = 250_000
    max_relationships: int = 4_096
    max_sheets: int = 256
    max_shared_strings: int = 100_000
    max_shared_string_characters: int = 8 * 1024 * 1024
    max_rows: int = 200
    max_columns: int = 50
    max_cells: int = 5_000
    max_cell_characters: int = 4_096
    max_output_characters: int = 256 * 1024
    max_html_characters: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (
            self.max_input_bytes,
            self.max_members,
            self.max_member_name_characters,
            self.max_central_directory_bytes,
            self.max_member_compressed_bytes,
            self.max_member_uncompressed_bytes,
            self.max_total_uncompressed_bytes,
            self.max_compression_ratio,
            self.max_xml_bytes,
            self.max_xml_elements,
            self.max_relationships,
            self.max_sheets,
            self.max_shared_strings,
            self.max_shared_string_characters,
            self.max_rows,
            self.max_columns,
            self.max_cells,
            self.max_cell_characters,
            self.max_output_characters,
            self.max_html_characters,
        )
        if any(value <= 0 for value in values):
            raise ValueError("XLSX limits must be positive")


DEFAULT_LIMITS = XlsxLimits()


@dataclass(frozen=True, slots=True)
class PreviewCell:
    """A single visible cell; ``column`` is one-based."""

    column: int
    value: str


@dataclass(frozen=True, slots=True)
class PreviewRow:
    """A sparse preview row preserving its worksheet row number."""

    number: int
    cells: tuple[PreviewCell, ...]


@dataclass(frozen=True, slots=True)
class XlsxPreview:
    """A bounded, inert representation of the first visible worksheet."""

    sheet_name: str
    rows: tuple[PreviewRow, ...]
    column_count: int
    truncation_reasons: tuple[str, ...] = ()
    formula_cells: int = 0
    external_relationships_ignored: int = 0
    active_parts_ignored: int = 0

    @property
    def truncated(self) -> bool:
        return bool(self.truncation_reasons)


@dataclass(frozen=True, slots=True)
class XlsxRenderResult:
    """The model and its self-contained static HTML document."""

    preview: XlsxPreview
    html: str

    @property
    def title(self) -> str:
        return self.preview.sheet_name

    @property
    def subtitle(self) -> str:
        visible_rows = len(self.preview.rows)
        suffix = " · truncated" if self.preview.truncated else ""
        return f"Spreadsheet · {visible_rows} visible rows{suffix}"


@dataclass(frozen=True, slots=True)
class _Relationship:
    identifier: str
    kind: str
    target: str | None
    external: bool


@dataclass(frozen=True, slots=True)
class _SharedStringTable:
    values: tuple[tuple[str, bool], ...] = ()


@dataclass(slots=True)
class _SheetAccumulator:
    limits: XlsxLimits
    rows: list[PreviewRow] = field(default_factory=list)
    current_cells: list[PreviewCell] = field(default_factory=list)
    truncation_reasons: set[str] = field(default_factory=set)
    formula_cells: int = 0
    cells_seen: int = 0
    output_characters: int = 0
    max_column: int = 0
    current_row_number: int = 0
    current_fallback_column: int = 1
    stop_collecting: bool = False


def supports_xlsx(filename: str | None, content_type: str | None) -> bool:
    """Return whether a renderer registry should offer the XLSX renderer."""

    if content_type and content_type.lower() in XLSX_CONTENT_TYPES:
        return True
    return bool(filename and Path(filename).suffix.lower() == ".xlsx")


def parse_xlsx(
    path: str | os.PathLike[str],
    limits: XlsxLimits = DEFAULT_LIMITS,
    *,
    cancelled: CancellationCheck | None = None,
) -> XlsxPreview:
    """Parse one local XLSX file into a bounded, non-executable preview model.

    The path is opened once, then validated and parsed through that same
    descriptor.  Symlinks to regular files work without reopening the target;
    special files are rejected.  Archive members are streamed into memory under
    per-member limits and are never written to disk.
    """

    descriptor = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )

    try:
        _check_cancelled(cancelled)
        descriptor = os.open(os.fspath(path), flags)
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise XlsxPreviewError("The spreadsheet is not a regular file")
        if file_status.st_size <= 0:
            raise XlsxPreviewError("The spreadsheet is empty")
        if file_status.st_size > limits.max_input_bytes:
            raise XlsxPreviewError("The spreadsheet exceeds the input size limit")
        _check_cancelled(cancelled)

        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        with stream:
            declared_members = _preflight_zip(
                stream,
                file_status.st_size,
                limits,
                cancelled,
            )
            stream.seek(0)
            try:
                with zipfile.ZipFile(stream, mode="r", allowZip64=False) as archive:
                    members = _validate_archive(
                        archive,
                        declared_members,
                        limits,
                        cancelled,
                    )
                    return _parse_package(archive, members, limits, cancelled)
            except (
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
                NotImplementedError,
                RuntimeError,
                UnicodeError,
            ) as error:
                raise XlsxPreviewError("The spreadsheet package is invalid") from error
    except XlsxPreviewError:
        raise
    except OSError as error:
        raise XlsxPreviewError("The spreadsheet could not be opened safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def render_xlsx(
    path: str | os.PathLike[str],
    limits: XlsxLimits = DEFAULT_LIMITS,
    *,
    cancelled: CancellationCheck | None = None,
) -> XlsxRenderResult:
    """Parse an XLSX file and produce a self-contained inert HTML preview."""

    preview = parse_xlsx(path, limits, cancelled=cancelled)
    _check_cancelled(cancelled)
    return XlsxRenderResult(
        preview=preview,
        html=build_xlsx_html(preview, limits, cancelled=cancelled),
    )


def build_xlsx_html(
    preview: XlsxPreview,
    limits: XlsxLimits = DEFAULT_LIMITS,
    *,
    cancelled: CancellationCheck | None = None,
) -> str:
    """Render a preview model to escaped HTML with a deny-by-default CSP."""

    _check_cancelled(cancelled)
    sheet_name = html.escape(preview.sheet_name, quote=False)
    notices: list[str] = []
    if preview.formula_cells:
        notices.append(
            f"{preview.formula_cells} formula cell(s) shown only from cached values; "
            "formulas were not evaluated."
        )
    if preview.external_relationships_ignored:
        notices.append("External workbook links were ignored.")
    if preview.active_parts_ignored:
        notices.append("Embedded active content was ignored.")
    if preview.truncation_reasons:
        notices.append(
            "Preview limited by "
            + ", ".join(html.escape(reason, quote=False) for reason in preview.truncation_reasons)
            + "."
        )

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
        "script-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; img-src data:; "
        "font-src data:; media-src &#39;none&#39;; frame-src &#39;none&#39;; object-src "
        "&#39;none&#39;; connect-src &#39;none&#39;; base-uri &#39;none&#39;; "
        "form-action &#39;none&#39;\">",
        f"<title>{sheet_name}</title>",
        "<style>",
        """
        :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
        * { box-sizing: border-box; }
        body { margin: 0; color: CanvasText; background: Canvas; }
        header { position: sticky; top: 0; z-index: 3; padding: 14px 18px;
                 border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
                 background: color-mix(in srgb, Canvas 94%, transparent);
                 backdrop-filter: blur(12px); }
        h1 { margin: 0; overflow: hidden; font-size: 16px; font-weight: 650;
             text-overflow: ellipsis; white-space: nowrap; }
        .notice { margin: 6px 0 0; color: color-mix(in srgb, CanvasText 70%, transparent);
                  font-size: 12px; }
        .grid { overflow: auto; padding: 0 0 24px; }
        table { border-spacing: 0; border-collapse: separate; min-width: 100%;
                table-layout: fixed; font-size: 13px; }
        th, td { min-width: 112px; max-width: 320px; height: 30px; padding: 5px 9px;
                 overflow: hidden;
                 border-right: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
                 border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
                 text-align: left; text-overflow: ellipsis; vertical-align: top;
                 white-space: pre-wrap; overflow-wrap: anywhere; }
        thead th { position: sticky; top: 0; z-index: 2; height: 28px; text-align: center;
                   color: color-mix(in srgb, CanvasText 70%, transparent);
                   background: color-mix(in srgb, Canvas 92%, CanvasText 8%); }
        .row-number { position: sticky; left: 0; z-index: 1; min-width: 54px; width: 54px;
                      color: color-mix(in srgb, CanvasText 65%, transparent); text-align: right;
                      background: color-mix(in srgb, Canvas 92%, CanvasText 8%); }
        thead .row-number { z-index: 4; }
        .empty { padding: 52px 20px; color: color-mix(in srgb, CanvasText 65%, transparent);
                 text-align: center; }
        """,
        "</style></head><body>",
        f"<header><h1>{sheet_name}</h1>",
    ]
    if notices:
        parts.append(f'<p class="notice">{html.escape(" ".join(notices), quote=False)}</p>')
    parts.append("</header>")

    if not preview.rows:
        parts.append('<p class="empty">This worksheet contains no visible cell values.</p>')
    else:
        column_count = max(1, min(preview.column_count, limits.max_columns))
        parts.extend(['<div class="grid"><table><thead><tr><th class="row-number"></th>'])
        for column in range(1, column_count + 1):
            parts.append(f"<th>{_column_label(column)}</th>")
        parts.append("</tr></thead><tbody>")
        for row in preview.rows[: limits.max_rows]:
            _check_cancelled(cancelled)
            by_column = {
                cell.column: cell.value
                for cell in row.cells
                if 1 <= cell.column <= column_count
            }
            parts.append(f'<tr><th class="row-number">{row.number}</th>')
            for column in range(1, column_count + 1):
                value = html.escape(by_column.get(column, ""), quote=False)
                parts.append(f"<td>{value}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table></div>")

    parts.append("</body></html>")
    _check_cancelled(cancelled)
    rendered = "".join(parts)
    if len(rendered) > limits.max_html_characters:
        raise XlsxPreviewError("The rendered spreadsheet preview exceeds its output limit")
    return rendered


def _preflight_zip(
    stream: BinaryIO,
    size: int,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> int:
    """Validate the classic ZIP directory before ``ZipFile`` allocates its index."""

    _check_cancelled(cancelled)
    tail_size = min(size, _EOCD.size + _MAX_ZIP_COMMENT_BYTES)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    position = tail.rfind(_EOCD_SIGNATURE)
    while position >= 0:
        if position + _EOCD.size <= len(tail):
            fields = _EOCD.unpack_from(tail, position)
            comment_size = fields[-1]
            if position + _EOCD.size + comment_size == len(tail):
                break
        position = tail.rfind(_EOCD_SIGNATURE, 0, position)
    if position < 0:
        raise XlsxPreviewError("The spreadsheet is not a complete ZIP package")

    (
        _signature,
        disk_number,
        directory_disk,
        entries_on_disk,
        total_entries,
        directory_size,
        directory_offset,
        _comment_size,
    ) = _EOCD.unpack_from(tail, position)
    if disk_number or directory_disk or entries_on_disk != total_entries:
        raise XlsxPreviewError("Multi-disk spreadsheet packages are not supported")
    if (
        total_entries == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise XlsxPreviewError("ZIP64 spreadsheet packages are outside preview limits")
    if not total_entries:
        raise XlsxPreviewError("The spreadsheet package has no members")
    if total_entries > limits.max_members:
        raise XlsxPreviewError("The spreadsheet package contains too many members")
    if directory_size > limits.max_central_directory_bytes:
        raise XlsxPreviewError("The spreadsheet package index is too large")

    absolute_eocd = size - tail_size + position
    if directory_offset + directory_size != absolute_eocd:
        raise XlsxPreviewError("The spreadsheet package layout is unsupported")
    _scan_central_directory(
        stream,
        directory_offset,
        directory_size,
        total_entries,
        limits,
        cancelled,
    )
    return total_entries


def _scan_central_directory(
    stream: BinaryIO,
    offset: int,
    size: int,
    expected_entries: int,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> None:
    """Count central records without allowing ``ZipFile`` to allocate first."""

    stream.seek(offset)
    consumed = 0
    entries = 0
    while consumed < size:
        _check_cancelled(cancelled)
        header = stream.read(_CENTRAL_FILE_HEADER_BYTES)
        if (
            len(header) != _CENTRAL_FILE_HEADER_BYTES
            or header[:4] != _CENTRAL_FILE_SIGNATURE
        ):
            raise XlsxPreviewError("The spreadsheet package index is malformed")
        name_size = struct.unpack_from("<H", header, 28)[0]
        extra_size = struct.unpack_from("<H", header, 30)[0]
        comment_size = struct.unpack_from("<H", header, 32)[0]
        disk_number = struct.unpack_from("<H", header, 34)[0]
        variable_size = name_size + extra_size + comment_size
        record_size = _CENTRAL_FILE_HEADER_BYTES + variable_size
        if disk_number or record_size > size - consumed:
            raise XlsxPreviewError("The spreadsheet package index is malformed")
        if name_size > limits.max_member_name_characters * 4:
            raise XlsxPreviewError("The spreadsheet package has an invalid member name")
        raw_name = stream.read(name_size)
        if len(raw_name) != name_size:
            raise XlsxPreviewError("The spreadsheet package index is malformed")
        # ``ZipInfo`` historically truncates names at NUL.  Reject it in the raw
        # directory record so a crafted member cannot masquerade as a required
        # OOXML part after decoding.
        if b"\x00" in raw_name or b"\\" in raw_name or raw_name.startswith(b"/"):
            raise XlsxPreviewError("The spreadsheet package has an unsafe member path")
        stream.seek(extra_size + comment_size, os.SEEK_CUR)
        consumed += record_size
        entries += 1
        if entries > limits.max_members:
            raise XlsxPreviewError("The spreadsheet package contains too many members")
    if consumed != size or entries != expected_entries:
        raise XlsxPreviewError("The spreadsheet package index is inconsistent")


def _validate_archive(
    archive: zipfile.ZipFile,
    declared_members: int,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) != declared_members or len(infos) > limits.max_members:
        raise XlsxPreviewError("The spreadsheet package index is inconsistent")

    members: dict[str, zipfile.ZipInfo] = {}
    total_uncompressed = 0
    for info in infos:
        _check_cancelled(cancelled)
        normalized = _validate_member_name(info.filename, limits)
        if normalized in members:
            raise XlsxPreviewError("The spreadsheet package contains duplicate members")
        if info.flag_bits & 0x1:
            raise XlsxPreviewError("Encrypted spreadsheet members are not supported")
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise XlsxPreviewError("The spreadsheet uses unsupported compression")
        if info.compress_size > limits.max_member_compressed_bytes:
            raise XlsxPreviewError("A compressed spreadsheet member is too large")
        if info.file_size > limits.max_member_uncompressed_bytes:
            raise XlsxPreviewError("A spreadsheet member expands beyond the safety limit")
        if info.file_size and (
            not info.compress_size
            or info.file_size / info.compress_size > limits.max_compression_ratio
        ):
            raise XlsxPreviewError("A spreadsheet member has an unsafe compression ratio")
        total_uncompressed += info.file_size
        if total_uncompressed > limits.max_total_uncompressed_bytes:
            raise XlsxPreviewError("The spreadsheet package expands beyond the safety limit")
        members[normalized] = info
    return members


def _validate_member_name(name: str, limits: XlsxLimits) -> str:
    if not name or len(name) > limits.max_member_name_characters:
        raise XlsxPreviewError("The spreadsheet package has an invalid member name")
    if "\x00" in name or "\\" in name or name.startswith("/"):
        raise XlsxPreviewError("The spreadsheet package has an unsafe member path")
    normalized = name[:-1] if name.endswith("/") else name
    parts = normalized.split("/")
    if not normalized or any(part in {"", ".", ".."} for part in parts):
        raise XlsxPreviewError("The spreadsheet package has an unsafe member path")
    if ":" in parts[0] or posixpath.normpath(normalized) != normalized:
        raise XlsxPreviewError("The spreadsheet package has an unsafe member path")
    return normalized


def _parse_package(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> XlsxPreview:
    content_types = _read_xml_member(
        archive,
        members,
        "[Content_Types].xml",
        limits,
        "content types",
        cancelled,
    )
    root_relationships = _read_xml_member(
        archive,
        members,
        "_rels/.rels",
        limits,
        "package relationships",
        cancelled,
    )
    root_relations = _parse_relationships(
        root_relationships,
        "",
        limits,
        cancelled,
    )
    office_relation = _one_relationship(root_relations, "officeDocument")
    if office_relation.external or office_relation.target is None:
        raise XlsxPreviewError("The workbook relationship must stay inside the package")
    workbook_part = office_relation.target
    _validate_workbook_content_type(
        content_types,
        workbook_part,
        limits,
        cancelled,
    )

    workbook_xml = _read_xml_member(
        archive,
        members,
        workbook_part,
        limits,
        "workbook",
        cancelled,
    )
    workbook_relationship_part = _relationship_part_name(workbook_part)
    workbook_relationship_xml = _read_xml_member(
        archive,
        members,
        workbook_relationship_part,
        limits,
        "workbook relationships",
        cancelled,
    )
    workbook_relations = _parse_relationships(
        workbook_relationship_xml,
        workbook_part,
        limits,
        cancelled,
    )
    relation_by_id = {relation.identifier: relation for relation in workbook_relations}
    sheet_name, sheet_relation_id = _select_first_visible_sheet(
        workbook_xml,
        limits,
        cancelled,
    )
    try:
        sheet_relation = relation_by_id[sheet_relation_id]
    except KeyError as error:
        raise XlsxPreviewError("The worksheet relationship is missing") from error
    if not sheet_relation.kind.endswith("/worksheet"):
        raise XlsxPreviewError("The selected relationship is not a worksheet")
    if sheet_relation.external or sheet_relation.target is None:
        raise XlsxPreviewError("The worksheet relationship must stay inside the package")

    shared_strings = _SharedStringTable()
    shared_relation = _optional_relationship(workbook_relations, "sharedStrings")
    if shared_relation is not None:
        if shared_relation.external or shared_relation.target is None:
            raise XlsxPreviewError("Shared strings must stay inside the package")
        shared_xml = _read_xml_member(
            archive,
            members,
            shared_relation.target,
            limits,
            "shared strings",
            cancelled,
        )
        shared_strings = _parse_shared_strings(shared_xml, limits, cancelled)

    sheet_xml = _read_xml_member(
        archive,
        members,
        sheet_relation.target,
        limits,
        "worksheet",
        cancelled,
    )
    rows, column_count, reasons, formulas = _parse_worksheet(
        sheet_xml,
        shared_strings,
        limits,
        cancelled,
    )

    external_relationships = sum(
        relation.external for relation in (*root_relations, *workbook_relations)
    )
    active_parts = sum(
        _is_active_part(name) for name, info in members.items() if not info.is_dir()
    )
    return XlsxPreview(
        sheet_name=sheet_name,
        rows=rows,
        column_count=column_count,
        truncation_reasons=reasons,
        formula_cells=formulas,
        external_relationships_ignored=external_relationships,
        active_parts_ignored=active_parts,
    )


def _read_xml_member(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    name: str,
    limits: XlsxLimits,
    label: str,
    cancelled: CancellationCheck | None,
) -> bytes:
    try:
        info = members[name]
    except KeyError as error:
        raise XlsxPreviewError(f"The spreadsheet is missing its {label}") from error
    if info.is_dir():
        raise XlsxPreviewError(f"The spreadsheet {label} is not a file")
    if info.file_size > limits.max_xml_bytes:
        raise XlsxPreviewError(f"The spreadsheet {label} exceeds its XML size limit")

    try:
        with archive.open(info, mode="r") as member:
            chunks: list[bytes] = []
            remaining = limits.max_xml_bytes + 1
            while remaining:
                _check_cancelled(cancelled)
                chunk = member.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > limits.max_xml_bytes:
                raise XlsxPreviewError(
                    f"The spreadsheet {label} exceeds its XML size limit"
                )
    except (zipfile.BadZipFile, RuntimeError, OSError, zlib.error) as error:
        raise XlsxPreviewError(f"The spreadsheet {label} could not be read") from error
    _validate_xml_bytes(data, label)
    return data


def _validate_xml_bytes(data: bytes, label: str) -> None:
    # OOXML parts are normally UTF-8.  Refusing NUL-containing encodings keeps
    # the active-markup preflight simple and prevents UTF-16 keyword bypasses.
    upper = data.upper()
    if b"\x00" in data:
        raise XlsxPreviewError(f"The spreadsheet {label} uses an unsupported encoding")
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise XlsxPreviewError(f"The spreadsheet {label} contains active XML declarations")


def _parse_xml(
    data: bytes,
    label: str,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
):
    import xml.etree.ElementTree as element_tree

    try:
        iterator = element_tree.iterparse(io.BytesIO(data), events=("start",))
        element_count = 0
        for _event, _element in iterator:
            element_count += 1
            if not element_count % 256:
                _check_cancelled(cancelled)
            if element_count > limits.max_xml_elements:
                raise XlsxPreviewError(
                    f"The spreadsheet {label} contains too many XML elements"
                )
        return iterator.root
    except (element_tree.ParseError, LookupError, ValueError) as error:
        raise XlsxPreviewError(f"The spreadsheet {label} is malformed") from error


def _parse_relationships(
    data: bytes,
    source_part: str,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> tuple[_Relationship, ...]:
    root = _parse_xml(data, "relationships", limits, cancelled)
    relationships: list[_Relationship] = []
    identifiers: set[str] = set()
    for element in root.iter():
        _check_cancelled(cancelled)
        if _local_name(element.tag) != "Relationship":
            continue
        if len(relationships) >= limits.max_relationships:
            raise XlsxPreviewError("The spreadsheet contains too many relationships")
        identifier = element.attrib.get("Id", "")
        kind = element.attrib.get("Type", "")
        raw_target = element.attrib.get("Target", "")
        target_mode = element.attrib.get("TargetMode", "")
        if not identifier or not kind or not raw_target or identifier in identifiers:
            raise XlsxPreviewError("The spreadsheet contains an invalid relationship")
        if target_mode.lower() not in {"", "external"}:
            raise XlsxPreviewError("The spreadsheet contains an invalid relationship mode")
        external = target_mode.lower() == "external"
        target = None if external else _resolve_internal_target(source_part, raw_target)
        relationships.append(_Relationship(identifier, kind, target, external))
        identifiers.add(identifier)
    return tuple(relationships)


def _resolve_internal_target(source_part: str, target: str) -> str:
    if "\x00" in target or "\\" in target:
        raise XlsxPreviewError("A spreadsheet relationship has an unsafe target")
    try:
        parsed = urlsplit(target)
    except ValueError as error:
        raise XlsxPreviewError(
            "A spreadsheet relationship has an unsafe target"
        ) from error
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise XlsxPreviewError("A spreadsheet relationship leaves the package")
    if target.startswith("/"):
        candidate = target.lstrip("/")
    else:
        candidate = posixpath.join(posixpath.dirname(source_part), target)
    normalized = posixpath.normpath(candidate)
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or normalized.startswith("/")
    ):
        raise XlsxPreviewError("A spreadsheet relationship leaves the package")
    return normalized


def _one_relationship(
    relationships: Iterable[_Relationship], kind_suffix: str
) -> _Relationship:
    matches = [
        relation
        for relation in relationships
        if relation.kind.endswith(f"/{kind_suffix}")
    ]
    if len(matches) != 1:
        raise XlsxPreviewError(f"The spreadsheet has an invalid {kind_suffix} relationship")
    return matches[0]


def _optional_relationship(
    relationships: Iterable[_Relationship], kind_suffix: str
) -> _Relationship | None:
    matches = [
        relation
        for relation in relationships
        if relation.kind.endswith(f"/{kind_suffix}")
    ]
    if len(matches) > 1:
        raise XlsxPreviewError(f"The spreadsheet has duplicate {kind_suffix} relationships")
    return matches[0] if matches else None


def _validate_workbook_content_type(
    data: bytes,
    workbook_part: str,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> None:
    root = _parse_xml(data, "content types", limits, cancelled)
    expected_name = f"/{workbook_part}"
    extension = posixpath.basename(workbook_part).rsplit(".", 1)[-1].casefold()
    overrides: list[str] = []
    defaults: list[str] = []
    for element in root.iter():
        _check_cancelled(cancelled)
        local_name = _local_name(element.tag)
        if (
            local_name == "Override"
            and element.attrib.get("PartName") == expected_name
        ):
            overrides.append(element.attrib.get("ContentType", ""))
        elif (
            local_name == "Default"
            and element.attrib.get("Extension", "").casefold() == extension
        ):
            defaults.append(element.attrib.get("ContentType", ""))

    if len(overrides) > 1 or (not overrides and len(defaults) > 1):
        raise XlsxPreviewError(
            "The package contains duplicate workbook content-type declarations"
        )
    declared_type = overrides[0] if overrides else defaults[0] if defaults else ""
    if declared_type not in _WORKBOOK_CONTENT_TYPES:
        raise XlsxPreviewError("The package is not a standard XLSX workbook")


def _select_first_visible_sheet(
    data: bytes,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> tuple[str, str]:
    root = _parse_xml(data, "workbook", limits, cancelled)
    first_sheet: tuple[str, str] | None = None
    first_visible_sheet: tuple[str, str] | None = None
    sheet_count = 0
    for element in root.iter():
        _check_cancelled(cancelled)
        if _local_name(element.tag) != "sheet":
            continue
        sheet_count += 1
        if sheet_count > limits.max_sheets:
            raise XlsxPreviewError("The workbook contains too many worksheets")
        name = element.attrib.get("name", "").strip()
        relation_id = next(
            (
                value
                for key, value in element.attrib.items()
                if _local_name(key) == "id"
            ),
            "",
        )
        if not name or not relation_id:
            raise XlsxPreviewError("The workbook contains an invalid worksheet")
        candidate = (name[:255], relation_id)
        if first_sheet is None:
            first_sheet = candidate
        if (
            first_visible_sheet is None
            and element.attrib.get("state", "visible").lower() == "visible"
        ):
            first_visible_sheet = candidate
    if first_sheet is None:
        raise XlsxPreviewError("The workbook does not contain a worksheet")
    return first_visible_sheet or first_sheet


def _parse_shared_strings(
    data: bytes,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> _SharedStringTable:
    root = _parse_xml(data, "shared strings", limits, cancelled)
    values: list[tuple[str, bool]] = []
    total_characters = 0
    for item in root:
        _check_cancelled(cancelled)
        if _local_name(item.tag) != "si":
            continue
        if len(values) >= limits.max_shared_strings:
            raise XlsxPreviewError("The workbook contains too many shared strings")
        chunks: list[str] = []
        for child in item:
            if _local_name(child.tag) == "t":
                chunks.append(child.text or "")
            elif _local_name(child.tag) == "r":
                chunks.extend(
                    grandchild.text or ""
                    for grandchild in child
                    if _local_name(grandchild.tag) == "t"
                )
        value = "".join(chunks)
        total_characters += len(value)
        if total_characters > limits.max_shared_string_characters:
            raise XlsxPreviewError("The workbook shared-string table is too large")
        clipped, was_clipped = _clip_text(value, limits.max_cell_characters)
        values.append((clipped, was_clipped))
    return _SharedStringTable(tuple(values))


def _parse_worksheet(
    data: bytes,
    shared_strings: _SharedStringTable,
    limits: XlsxLimits,
    cancelled: CancellationCheck | None,
) -> tuple[tuple[PreviewRow, ...], int, tuple[str, ...], int]:
    import xml.etree.ElementTree as element_tree

    accumulator = _SheetAccumulator(limits)
    element_count = 0
    row_count = 0
    in_row = False

    try:
        iterator = element_tree.iterparse(io.BytesIO(data), events=("start", "end"))
        for event, element in iterator:
            if event == "start":
                element_count += 1
                if not element_count % 256:
                    _check_cancelled(cancelled)
                if element_count > limits.max_xml_elements:
                    raise XlsxPreviewError(
                        "The spreadsheet worksheet contains too many XML elements"
                    )
                if _local_name(element.tag) == "row":
                    row_count += 1
                    if row_count > limits.max_rows:
                        accumulator.truncation_reasons.add("row limit")
                        break
                    accumulator.current_row_number = _row_number(
                        element.attrib.get("r"), row_count
                    )
                    accumulator.current_fallback_column = 1
                    accumulator.current_cells = []
                    in_row = True
                continue

            local_name = _local_name(element.tag)
            if local_name == "c" and in_row:
                accumulator.cells_seen += 1
                if accumulator.cells_seen > limits.max_cells:
                    accumulator.truncation_reasons.add("cell limit")
                    accumulator.stop_collecting = True
                elif not accumulator.stop_collecting:
                    _collect_cell(element, accumulator, shared_strings)
                element.clear()
                if accumulator.stop_collecting:
                    break
            elif local_name == "row" and in_row:
                accumulator.rows.append(
                    PreviewRow(
                        number=accumulator.current_row_number,
                        cells=tuple(accumulator.current_cells),
                    )
                )
                in_row = False
                element.clear()
    except (element_tree.ParseError, LookupError, ValueError) as error:
        raise XlsxPreviewError("The spreadsheet worksheet is malformed") from error

    if in_row and accumulator.current_cells:
        accumulator.rows.append(
            PreviewRow(
                number=accumulator.current_row_number,
                cells=tuple(accumulator.current_cells),
            )
        )
    reasons = tuple(
        reason
        for reason in (
            "row limit",
            "column limit",
            "cell limit",
            "cell text limit",
            "text output limit",
        )
        if reason in accumulator.truncation_reasons
    )
    return (
        tuple(accumulator.rows),
        max(1, accumulator.max_column),
        reasons,
        accumulator.formula_cells,
    )


def _collect_cell(
    element,
    accumulator: _SheetAccumulator,
    shared_strings: _SharedStringTable,
) -> None:
    reference = element.attrib.get("r")
    if reference:
        match = _CELL_REFERENCE.fullmatch(reference)
        if match is None:
            raise XlsxPreviewError("The worksheet contains an invalid cell reference")
        column = _column_number(match.group(1))
        if int(match.group(2)) > 1_048_576 or column > 16_384:
            raise XlsxPreviewError("The worksheet contains an out-of-range cell reference")
    else:
        column = accumulator.current_fallback_column
    accumulator.current_fallback_column = column + 1

    if column > accumulator.limits.max_columns:
        accumulator.truncation_reasons.add("column limit")
        return

    has_formula = any(_local_name(child.tag) == "f" for child in element)
    if has_formula:
        accumulator.formula_cells += 1
    value, was_clipped = _cell_display_value(
        element,
        shared_strings,
        accumulator.limits.max_cell_characters,
    )
    if was_clipped:
        accumulator.truncation_reasons.add("cell text limit")

    remaining = accumulator.limits.max_output_characters - accumulator.output_characters
    if remaining <= 0:
        accumulator.truncation_reasons.add("text output limit")
        accumulator.stop_collecting = True
        return
    if len(value) > remaining:
        value, _was_clipped = _clip_text(value, remaining)
        accumulator.truncation_reasons.add("text output limit")
        accumulator.stop_collecting = True

    accumulator.output_characters += len(value)
    accumulator.max_column = max(accumulator.max_column, column)
    accumulator.current_cells.append(PreviewCell(column=column, value=value))


def _cell_display_value(
    element,
    shared_strings: _SharedStringTable,
    max_characters: int,
) -> tuple[str, bool]:
    cell_type = element.attrib.get("t", "n")
    value_element = next(
        (child for child in element if _local_name(child.tag) == "v"), None
    )
    raw_value = value_element.text if value_element is not None else ""
    raw_value = raw_value or ""

    if cell_type == "s":
        try:
            index = int(raw_value, 10)
            if index < 0:
                raise ValueError
            return shared_strings.values[index]
        except (ValueError, IndexError) as error:
            raise XlsxPreviewError("A worksheet shared-string reference is invalid") from error
    if cell_type == "inlineStr":
        inline = next(
            (child for child in element if _local_name(child.tag) == "is"), None
        )
        text = "" if inline is None else _rich_text(inline)
    elif cell_type == "b":
        text = "TRUE" if raw_value == "1" else "FALSE" if raw_value == "0" else raw_value
    else:
        # For formula cells this is the cached result only.  The ``f`` element is
        # intentionally never read, interpreted, or copied into output.
        text = raw_value
    return _clip_text(text, max_characters)


def _rich_text(container) -> str:
    chunks: list[str] = []
    for child in container:
        if _local_name(child.tag) == "t":
            chunks.append(child.text or "")
        elif _local_name(child.tag) == "r":
            chunks.extend(
                grandchild.text or ""
                for grandchild in child
                if _local_name(grandchild.tag) == "t"
            )
    return "".join(chunks)


def _clip_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    if limit == 1:
        return "…", True
    return f"{value[: limit - 1]}…", True


def _row_number(raw_number: str | None, fallback: int) -> int:
    if raw_number is None:
        return fallback
    try:
        number = int(raw_number, 10)
    except ValueError as error:
        raise XlsxPreviewError("The worksheet contains an invalid row number") from error
    if not 1 <= number <= 1_048_576:
        raise XlsxPreviewError("The worksheet contains an out-of-range row number")
    return number


def _column_number(label: str) -> int:
    number = 0
    for character in label.upper():
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _column_label(number: int) -> str:
    characters: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        characters.append(chr(ord("A") + remainder))
    return "".join(reversed(characters))


def _relationship_part_name(source_part: str) -> str:
    directory, basename = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", f"{basename}.rels")


def _is_active_part(name: str) -> bool:
    lowered = name.lower()
    return lowered in _ACTIVE_PART_NAMES or lowered.startswith(_ACTIVE_PART_PREFIXES)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise XlsxPreviewCancelled("Spreadsheet preview cancelled")
