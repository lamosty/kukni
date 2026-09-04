# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Read-only, never-executed previews for text, source, and script files."""

from __future__ import annotations

from collections.abc import Callable
import codecs
from dataclasses import dataclass
import os
import stat
import threading
import unicodedata

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from .base import ErrorCallback, ReadyCallback


MAX_TEXT_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
MAX_VISIBLE_CHARACTERS = 2 * 1024 * 1024
MAX_VISIBLE_LINES = 20_000
MAX_LINE_CHARACTERS = 32 * 1024
MAX_COMBINING_RUN = 64
CancellationCheck = Callable[[], bool]

TEXT_APPLICATION_TYPES = frozenset(
    (
        "application/ecmascript",
        "application/javascript",
        "application/json",
        "application/ld+json",
        "application/manifest+json",
        "application/sql",
        "application/toml",
        "application/x-ndjson",
        "application/x-desktop",
        "application/x-gnome-app-info",
        "application/x-httpd-php",
        "application/x-javascript",
        "application/x-perl",
        "application/x-ruby",
        "application/x-shellscript",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
    )
)

BINARY_EXECUTABLE_TYPES = frozenset(
    (
        "application/vnd.microsoft.portable-executable",
        "application/x-core",
        "application/x-executable",
        "application/x-mach-binary",
        "application/x-object",
        "application/x-pie-executable",
        "application/x-python-bytecode",
        "application/x-sharedlib",
    )
)

TEXT_SUFFIXES = (
    ".bash",
    ".c",
    ".cc",
    ".cfg",
    ".cjs",
    ".cmake",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".cxx",
    ".desktop",
    ".diff",
    ".fish",
    ".go",
    ".gpx",
    ".h",
    ".hh",
    ".hpp",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".ksh",
    ".kt",
    ".kts",
    ".log",
    ".lua",
    ".markdown",
    ".md",
    ".mjs",
    ".patch",
    ".php",
    ".pl",
    ".pm",
    ".properties",
    ".py",
    ".pyw",
    ".rb",
    ".rs",
    ".rst",
    ".service",
    ".sh",
    ".socket",
    ".sql",
    ".swift",
    ".target",
    ".text",
    ".timer",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".xsd",
    ".xsl",
    ".yaml",
    ".yml",
    ".zsh",
)

TEXT_BASENAMES = frozenset(
    (
        ".bashrc",
        ".dockerignore",
        ".editorconfig",
        ".env",
        ".gitattributes",
        ".gitignore",
        ".profile",
        ".zshrc",
        "changelog",
        "cmakelists.txt",
        "containerfile",
        "copying",
        "dockerfile",
        "license",
        "makefile",
        "meson.build",
        "readme",
    )
)

LAUNCHER_CONTENT_TYPES = frozenset(
    ("application/x-desktop", "application/x-gnome-app-info")
)

_BIDI_CONTROLS = {
    "\u061c": "ALM",
    "\u200e": "LRM",
    "\u200f": "RLM",
    "\u202a": "LRE",
    "\u202b": "RLE",
    "\u202c": "PDF",
    "\u202d": "LRO",
    "\u202e": "RLO",
    "\u2066": "LRI",
    "\u2067": "RLI",
    "\u2068": "FSI",
    "\u2069": "PDI",
}

_FORMAT_CONTROLS = {
    "\u00ad": "SHY",
    "\u200b": "ZWSP",
    "\u200c": "ZWNJ",
    "\u200d": "ZWJ",
    "\u2060": "WJ",
    "\ufeff": "BOM",
}

_BINARY_PREFIXES = (
    b"\x7fELF",
    b"\x00asm",
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    b"SQLite format 3\x00",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)

_WORKER_SLOTS = threading.BoundedSemaphore(4)
_DISPLAY_CLIPPED_MARKER = "⟦PREVIEW CLIPPED TO SAFE DISPLAY LIMITS⟧"
_LINE_CLIPPED_MARKER = "⟦LINE CLIPPED⟧"
_COMBINING_CLIPPED_MARKER = "⟦COMBINING MARKS CLIPPED⟧"


class TextPreviewError(RuntimeError):
    """A file could not be represented as bounded text safely."""


class TextPreviewCancelled(Exception):
    """Text loading stopped because a newer preview superseded it."""


@dataclass(frozen=True, slots=True)
class TextSample:
    data: bytes
    truncated: bool
    executable: bool
    has_shebang: bool


def supports_text(filename: str | None, content_type: str | None) -> bool:
    """Identify text conservatively without claiming native executables."""

    media_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if media_type in BINARY_EXECUTABLE_TYPES:
        return False
    if media_type.startswith("text/") or (
        media_type and Gio.content_type_is_a(media_type, "text/plain")
    ):
        return True
    if (
        media_type in TEXT_APPLICATION_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    ):
        return True

    generic_type = (
        not media_type
        or media_type
        in (
            "application/octet-stream",
            "application/x-empty",
            "application/x-zerosize",
            "inode/x-empty",
        )
        or Gio.content_type_is_unknown(media_type)
    )
    if not generic_type or not filename:
        return False

    basename = filename.rsplit("/", 1)[-1].casefold()
    return (
        basename in TEXT_BASENAMES
        or basename.startswith(".env.")
        or basename.endswith(TEXT_SUFFIXES)
    )


def decode_text_bytes(source: bytes, *, allow_trailing_partial: bool = False) -> str:
    """Decode declared Unicode strictly, trimming only a truncated final scalar."""

    if not isinstance(source, bytes):
        raise TypeError("text source must be bytes")

    encoding = "utf-8-sig"
    if source.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif source.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif b"\x00" in source:
        raise TextPreviewError("NUL bytes indicate binary rather than text content")

    try:
        return source.decode(encoding, errors="strict")
    except UnicodeDecodeError as error:
        trailing_partial = (
            allow_trailing_partial
            and error.end == len(source)
            and len(source) - error.start <= 4
        )
        if trailing_partial:
            try:
                return source[: error.start].decode(encoding, errors="strict")
            except UnicodeDecodeError:
                pass
        raise TextPreviewError(
            "The file is not valid UTF-8 or BOM-marked UTF-16/UTF-32 text"
        ) from error


def normalize_visible_text(source: bytes | str) -> str:
    """Expose deceptive controls while bounding GTK text-layout complexity."""

    if isinstance(source, bytes):
        text = decode_text_bytes(source)
    elif isinstance(source, str):
        text = source
    else:
        raise TypeError("text source must be bytes or text")

    text = text.replace("\r\n", "\n")
    visible: list[str] = []
    visible_characters = 0
    visible_lines = 1
    line_characters = 0
    combining_run = 0
    line_clipped = False
    combining_clipped = False

    def append(piece: str) -> bool:
        nonlocal visible_characters
        if visible_characters + len(piece) > MAX_VISIBLE_CHARACTERS:
            visible.append(f"\n{_DISPLAY_CLIPPED_MARKER}\n")
            return False
        visible.append(piece)
        visible_characters += len(piece)
        return True

    for character in text:
        if character == "\n":
            if visible_lines >= MAX_VISIBLE_LINES:
                append(f"\n{_DISPLAY_CLIPPED_MARKER}\n")
                break
            if not append(character):
                break
            visible_lines += 1
            line_characters = 0
            combining_run = 0
            line_clipped = False
            combining_clipped = False
            continue

        line_characters += 1
        if line_characters > MAX_LINE_CHARACTERS:
            if not line_clipped:
                if not append(_LINE_CLIPPED_MARKER):
                    break
                line_clipped = True
            continue

        if unicodedata.combining(character):
            combining_run += 1
            if combining_run > MAX_COMBINING_RUN:
                if not combining_clipped:
                    if not append(_COMBINING_CLIPPED_MARKER):
                        break
                    combining_clipped = True
                continue
        else:
            combining_run = 0
            combining_clipped = False

        if character in ("\n", "\t"):
            if not append(character):
                break
            continue
        if character in _BIDI_CONTROLS:
            if not append(f"⟦{_BIDI_CONTROLS[character]}⟧"):
                break
            continue

        codepoint = ord(character)
        if codepoint < 0x20:
            rendered = chr(0x2400 + codepoint)
        elif codepoint == 0x7F:
            rendered = "␡"
        elif character == "\u2028":
            rendered = "⟦LS⟧"
        elif character == "\u2029":
            rendered = "⟦PS⟧"
        elif unicodedata.category(character) == "Cc":
            rendered = f"⟦{codepoint:04X}⟧"
        elif unicodedata.category(character) == "Cf":
            label = _FORMAT_CONTROLS.get(character, f"{codepoint:04X}")
            rendered = f"⟦{label}⟧"
        else:
            rendered = character
        if not append(rendered):
            break
    return "".join(visible)


def sanitize_display_label(value: str | None, fallback: str = "Untitled") -> str:
    """Make a one-line filename safe from control and bidi-based spoofing."""

    if not isinstance(value, str) or not value:
        return fallback
    return normalize_visible_text(value).replace("\n", "␊").replace("\t", "␉")


def read_text_sample(
    path: str | os.PathLike[str],
    *,
    limit: int = MAX_TEXT_BYTES,
    cancelled: CancellationCheck | None = None,
) -> TextSample:
    """Read at most ``limit`` bytes from one verified regular-file descriptor."""

    if limit <= 0:
        raise ValueError("text sample limit must be positive")
    _check_cancelled(cancelled)

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOCTTY", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise TextPreviewError("The text file could not be opened safely") from error

    try:
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise TextPreviewError(
                "The text file could not be inspected safely"
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            raise TextPreviewError("Text preview requires a regular local file")

        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            _check_cancelled(cancelled)
            try:
                chunk = os.read(
                    descriptor,
                    min(READ_CHUNK_BYTES, limit + 1 - total),
                )
            except OSError as error:
                raise TextPreviewError(
                    "The text file could not be read safely"
                ) from error
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if _has_binary_magic(b"".join(chunks)):
                raise TextPreviewError("Binary files are not text previews")

        contents = b"".join(chunks)
        sample = contents[:limit]
        return TextSample(
            data=sample,
            truncated=metadata.st_size > limit or len(contents) > limit,
            executable=bool(metadata.st_mode & 0o111),
            has_shebang=sample.startswith(b"#!"),
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise TextPreviewCancelled("text preview cancelled")


def _has_binary_magic(contents: bytes) -> bool:
    if contents.startswith(_BINARY_PREFIXES):
        return True
    if len(contents) < 0x40 or not contents.startswith(b"MZ"):
        return False
    pe_offset = int.from_bytes(contents[0x3C:0x40], "little")
    signature = contents[pe_offset : pe_offset + 4]
    return pe_offset <= len(contents) - 4 and signature == b"PE\0\0"


def _is_launcher(filename: str | None, content_type: str | None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().casefold()
    return media_type in LAUNCHER_CONTENT_TYPES or bool(
        filename and filename.casefold().endswith(".desktop")
    )


class TextPreviewView(Gtk.Box):
    """A static source view that deliberately cannot receive keyboard focus."""

    def __init__(
        self,
        text: str,
        *,
        truncated: bool,
        executable: bool,
        has_shebang: bool,
        launcher: bool,
    ) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=22,
            margin_end=22,
        )
        self.set_focusable(False)
        self.truncated = truncated

        status = "Read-only source preview · hidden controls shown explicitly"
        if truncated:
            status += " · first 1 MiB shown"
        status_label = Gtk.Label(label=status, xalign=0)
        status_label.add_css_class("dim-label")
        self.append(status_label)

        self.safety_banner: Gtk.Widget | None = None
        if executable or has_shebang or launcher:
            self.safety_banner = self._build_safety_banner(launcher)
            self.append(self.safety_banner)

        scroller = Gtk.ScrolledWindow(
            hexpand=True,
            vexpand=True,
            has_frame=True,
            focusable=False,
        )
        self.text_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            focusable=False,
            accepts_tab=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.NONE,
            top_margin=16,
            bottom_margin=16,
            left_margin=18,
            right_margin=18,
        )
        self.text_view.set_accessible_role(Gtk.AccessibleRole.DOCUMENT)
        self.text_view.set_direction(Gtk.TextDirection.LTR)
        self.text_view.get_buffer().set_text(text)
        scroller.set_child(self.text_view)
        self.append(scroller)

    @staticmethod
    def _build_safety_banner(launcher: bool) -> Gtk.Widget:
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner.add_css_class("card")
        banner.set_margin_top(2)
        banner.set_margin_bottom(2)
        banner.set_margin_start(2)
        banner.set_margin_end(2)
        icon = Gtk.Image(icon_name="security-high-symbolic", pixel_size=24)
        banner.append(icon)
        message = (
            "Launcher definition shown as text. Kukni never follows its Exec command."
            if launcher
            else "Executable script shown as text. Kukni never runs its commands."
        )
        label = Gtk.Label(label=message, xalign=0, wrap=True)
        label.set_hexpand(True)
        banner.append(label)
        return banner


class TextRenderer:
    """Render a bounded snapshot without invoking the selected file."""

    id = "text"

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        return (
            file.is_native()
            and info.get_file_type() == Gio.FileType.REGULAR
            and supports_text(file.get_basename(), info.get_content_type())
        )

    def render(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        """Read off-thread and invoke a single callback on GTK's main context."""

        path = file.get_path() if file.is_native() else None
        if path is None:
            GLib.idle_add(
                self._deliver_error,
                cancellable,
                on_error,
                "Text preview supports local files only",
            )
            return
        if cancellable.is_cancelled():
            return
        if not _WORKER_SLOTS.acquire(blocking=False):
            GLib.idle_add(
                self._deliver_error,
                cancellable,
                on_error,
                "Text preview is busy; showing the universal preview",
            )
            return

        filename = file.get_basename()
        content_type = info.get_content_type()

        def worker() -> None:
            try:
                sample = read_text_sample(
                    path,
                    cancelled=cancellable.is_cancelled,
                )
                decoded = decode_text_bytes(
                    sample.data,
                    allow_trailing_partial=sample.truncated,
                )
                text = normalize_visible_text(decoded)
            except TextPreviewCancelled:
                return
            except TextPreviewError as error:
                GLib.idle_add(
                    self._deliver_error,
                    cancellable,
                    on_error,
                    str(error),
                )
                return
            except Exception:
                GLib.idle_add(
                    self._deliver_error,
                    cancellable,
                    on_error,
                    "The text preview could not be prepared safely",
                )
                return
            finally:
                _WORKER_SLOTS.release()

            GLib.idle_add(
                self._create_view,
                sample,
                text,
                filename,
                content_type,
                cancellable,
                on_ready,
                on_error,
            )

        thread = threading.Thread(
            target=worker,
            name="kukni-text-reader",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            _WORKER_SLOTS.release()
            GLib.idle_add(
                self._deliver_error,
                cancellable,
                on_error,
                "The text preview worker could not be started",
            )

    @staticmethod
    def _deliver_error(
        cancellable: Gio.Cancellable,
        on_error: ErrorCallback,
        message: str,
    ) -> bool:
        if not cancellable.is_cancelled():
            on_error(message)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _create_view(
        sample: TextSample,
        text: str,
        filename: str | None,
        content_type: str | None,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> bool:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        launcher = _is_launcher(filename, content_type)
        try:
            view = TextPreviewView(
                text,
                truncated=sample.truncated,
                executable=sample.executable,
                has_shebang=sample.has_shebang,
                launcher=launcher,
            )
        except Exception:
            on_error("The text preview could not be displayed")
            return GLib.SOURCE_REMOVE

        if launcher:
            subtitle = "Launcher source · read-only, never executed"
        elif sample.executable or sample.has_shebang:
            subtitle = "Script source · read-only, never executed"
        else:
            subtitle = "Text document · read-only"
        on_ready(view, subtitle)
        return GLib.SOURCE_REMOVE
