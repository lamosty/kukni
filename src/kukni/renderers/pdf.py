# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded lazy PDF pages rendered by short-lived sandboxed Poppler children."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import functools
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from ..worker import probe_bwrap_user_namespace, terminate_process_group
from .base import ErrorCallback, ReadyCallback
from .image_view import ImagePreviewView


PDF_CONTENT_TYPES = frozenset(
    (
        "application/pdf",
    )
)
PDF_SUFFIX = ".pdf"


@dataclass(frozen=True, slots=True)
class PdfLimits:
    max_input_bytes: int = 256 * 1024 * 1024
    max_output_bytes: int = 32 * 1024 * 1024
    max_edge_pixels: int = 1_800
    max_address_space_bytes: int = 768 * 1024 * 1024
    max_cpu_seconds: int = 10
    wall_timeout_seconds: float = 12.0
    max_open_files: int = 64
    max_pages: int = 500
    max_metadata_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.max_input_bytes,
                self.max_output_bytes,
                self.max_edge_pixels,
                self.max_address_space_bytes,
                self.max_cpu_seconds,
                self.wall_timeout_seconds,
                self.max_open_files,
                self.max_pages,
                self.max_metadata_bytes,
            )
        ):
            raise ValueError("PDF limits must be positive")


DEFAULT_LIMITS = PdfLimits()
CancellationCheck = Callable[[], bool]
_WORKER_SLOTS = threading.BoundedSemaphore(2)


class PdfPreviewError(RuntimeError):
    """A PDF could not be rendered inside the preview safety limits."""


class PdfPreviewCancelled(Exception):
    """PDF rendering stopped because its preview request was superseded."""


@functools.lru_cache(maxsize=1)
def pdf_runtime_unavailable_reason() -> str | None:
    """Probe only on worker threads; capability routing must never run a child."""

    for name in ("pdftoppm", "pdfinfo", "prlimit", "bwrap", "true"):
        if shutil.which(name) is None:
            return f"PDF previews require the missing {name} tool"
    if not probe_bwrap_user_namespace(shutil.which("bwrap"), shutil.which("true")):
        return (
            "The required PDF sandbox cannot start; system namespace policy may "
            "block bubblewrap. PDF previews remain disabled until the sandbox "
            "installation is repaired"
        )
    return None


def pdf_runtime_available() -> bool:
    """Return sandbox capability; not suitable for GTK's supports() path."""

    return pdf_runtime_unavailable_reason() is None


@dataclass(frozen=True, slots=True)
class PdfPage:
    png: bytes
    page_number: int
    page_count: int
    total_pages: int


def supports_pdf(filename: str | None, content_type: str | None) -> bool:
    if content_type and content_type.casefold() in PDF_CONTENT_TYPES:
        return True
    generic_type = (
        not content_type
        or content_type in ("application/octet-stream", "application/x-empty")
        or Gio.content_type_is_unknown(content_type)
    )
    return bool(
        generic_type
        and filename
        and filename.casefold().endswith(PDF_SUFFIX)
    )


def render_pdf_first_page(
    path: str | os.PathLike[str],
    *,
    limits: PdfLimits = DEFAULT_LIMITS,
    cancelled: CancellationCheck | None = None,
    pdftoppm_path: str | None = None,
    prlimit_path: str | None = None,
    bwrap_path: str | None = None,
) -> bytes:
    """Compatibility entry point for a single bounded first-page raster."""

    return render_pdf_page(
        path, 1, limits=limits, cancelled=cancelled,
        pdftoppm_path=pdftoppm_path, prlimit_path=prlimit_path,
        bwrap_path=bwrap_path,
    ).png


def render_pdf_page(
    path: str | os.PathLike[str],
    page_number: int,
    *,
    limits: PdfLimits = DEFAULT_LIMITS,
    cancelled: CancellationCheck | None = None,
    pdftoppm_path: str | None = None,
    pdfinfo_path: str | None = None,
    prlimit_path: str | None = None,
    bwrap_path: str | None = None,
) -> PdfPage:
    """Snapshot one input, inspect its count and raster exactly one bounded page.

    @decision Each navigation request re-snapshots the file: there are no idle
    child processes, retained document descriptors, or accumulating page cache.
    Count and pixels always come from the same immutable request snapshot, even
    if the original file changes while a page is being rendered. Both Poppler
    commands share one wall deadline and the same mandatory sandbox boundary.
    """

    _check_cancelled(cancelled)
    if type(page_number) is not int or not 1 <= page_number <= limits.max_pages:
        raise PdfPreviewError("The requested PDF page exceeds the preview page limit")
    deadline = time.monotonic() + limits.wall_timeout_seconds
    renderer = pdftoppm_path or shutil.which("pdftoppm")
    inspector = pdfinfo_path or shutil.which("pdfinfo")
    limiter = prlimit_path or shutil.which("prlimit")
    bwrap = bwrap_path or shutil.which("bwrap")
    for name, executable in (
        ("pdftoppm", renderer), ("pdfinfo", inspector),
        ("prlimit", limiter), ("bwrap", bwrap),
    ):
        if executable is None:
            raise PdfPreviewError(f"PDF previews require the missing {name} tool")
    if all(value is None for value in (
        pdftoppm_path, pdfinfo_path, prlimit_path, bwrap_path,
    )):
        reason = pdf_runtime_unavailable_reason()
    else:
        true = shutil.which("true")
        reason = None if true and probe_bwrap_user_namespace(bwrap, true) else (
            "The required PDF sandbox cannot start; PDF previews remain disabled"
        )
    if reason is not None:
        raise PdfPreviewError(reason)
    _check_request(cancelled, deadline)

    input_descriptor = -1
    snapshot = None
    snapshot_descriptor = -1
    temporary_directory = tempfile.mkdtemp(prefix="kukni-pdf-")
    output_directory = os.path.join(temporary_directory, "output")
    host_output_path = os.path.join(output_directory, "page.png")
    try:
        os.mkdir(output_directory, mode=0o700)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            input_descriptor = os.open(os.fspath(path), flags)
        except OSError as error:
            raise PdfPreviewError("The PDF could not be opened safely") from error
        metadata = os.fstat(input_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PdfPreviewError("PDF preview requires a regular local file")
        if metadata.st_size <= 0:
            raise PdfPreviewError("The PDF is empty")
        if metadata.st_size > limits.max_input_bytes:
            raise PdfPreviewError("The PDF exceeds the input size limit")

        snapshot = tempfile.TemporaryFile(mode="w+b", dir=temporary_directory)
        total = 0
        while total <= limits.max_input_bytes:
            _check_request(cancelled, deadline)
            try:
                chunk = os.read(
                    input_descriptor, min(64 * 1024, limits.max_input_bytes + 1 - total),
                )
            except OSError as error:
                raise PdfPreviewError("The PDF could not be read safely") from error
            if not chunk:
                break
            snapshot.write(chunk)
            total += len(chunk)
        if total > limits.max_input_bytes:
            raise PdfPreviewError("The PDF exceeds the input size limit")
        if total == 0:
            raise PdfPreviewError("The PDF is empty")
        snapshot.flush()
        snapshot.seek(0)
        snapshot_descriptor = os.open(
            f"/proc/self/fd/{snapshot.fileno()}",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        snapshot.close()
        snapshot = None
        os.close(input_descriptor)
        input_descriptor = -1
        input_path = f"/proc/self/fd/{snapshot_descriptor}"

        # @constraint pdfinfo parses untrusted PDF bytes too. Its stdout has a
        # separate small RLIMIT_FSIZE; never parse or expose arbitrary metadata.
        with tempfile.TemporaryFile(mode="w+b", dir=temporary_directory) as info_output:
            _run_pdf_command(
                [inspector, "-f", "1", "-l", "1", input_path],
                bwrap, limiter, temporary_directory, output_directory,
                snapshot_descriptor, limits, cancelled, deadline,
                stdout=info_output, output_limit=limits.max_metadata_bytes,
            )
            info_output.seek(0)
            total_pages = _parse_page_count(
                info_output.read(limits.max_metadata_bytes + 1), limits.max_metadata_bytes,
            )
        page_count = min(total_pages, limits.max_pages)
        if page_number > page_count:
            raise PdfPreviewError("The requested PDF page is not in this document")
        _run_pdf_command(
            [renderer, "-q", "-f", str(page_number), "-l", str(page_number),
             "-singlefile", "-cropbox", "-scale-to", str(limits.max_edge_pixels),
             "-png", input_path, "/output/page"],
            bwrap, limiter, temporary_directory, output_directory,
            snapshot_descriptor, limits, cancelled, deadline,
        )
        _check_request(cancelled, deadline)
        png = _read_output_png(host_output_path, limits.max_output_bytes)
        _validate_png_dimensions(png, limits.max_edge_pixels)
        _check_request(cancelled, deadline)
        return PdfPage(png, page_number, page_count, total_pages)
    finally:
        if input_descriptor >= 0:
            os.close(input_descriptor)
        if snapshot is not None:
            snapshot.close()
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        shutil.rmtree(temporary_directory, ignore_errors=True)


def _parse_page_count(data: bytes, limit: int) -> int:
    if len(data) > limit:
        raise PdfPreviewError("The PDF metadata exceeds its output limit")
    # Poppler emits a fixed English key under LC_ALL=C.UTF-8. Reject duplicate
    # or malformed keys instead of trusting document-supplied metadata text.
    counts = re.findall(rb"^Pages:[ \t]*([0-9]{1,10})[ \t]*$", data, re.MULTILINE)
    if len(counts) != 1 or not 1 <= int(counts[0]) <= 2_147_483_647:
        raise PdfPreviewError("The PDF page count could not be read safely")
    return int(counts[0])


def _check_request(cancelled: CancellationCheck | None, deadline: float) -> None:
    _check_cancelled(cancelled)
    if time.monotonic() >= deadline:
        raise PdfPreviewError("PDF preview timed out")


def _run_pdf_command(
    renderer_command, bwrap, limiter, temporary_directory, output_directory,
    snapshot_descriptor, limits, cancelled, deadline, *,
    stdout=subprocess.DEVNULL, output_limit=None,
) -> None:
    _check_request(cancelled, deadline)
    command = [
        limiter, f"--as={limits.max_address_space_bytes}",
        f"--cpu={limits.max_cpu_seconds}",
        f"--fsize={output_limit or limits.max_output_bytes}",
        f"--nofile={limits.max_open_files}", "--core=0", "--",
        *_build_sandbox_command(bwrap, output_directory, renderer_command),
    ]
    environment = {
        "HOME": temporary_directory, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin", "TMPDIR": temporary_directory,
        "XDG_CACHE_HOME": os.path.join(temporary_directory, "cache"),
    }
    process = None
    try:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=stdout,
                stderr=subprocess.DEVNULL, close_fds=True,
                pass_fds=(snapshot_descriptor,), start_new_session=True,
                env=environment, cwd=temporary_directory,
            )
        except OSError as error:
            raise PdfPreviewError("The PDF rendering worker could not start") from error
        while process.poll() is None:
            _check_request(cancelled, deadline)
            try:
                process.wait(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        _check_request(cancelled, deadline)
        if process.returncode != 0:
            raise PdfPreviewError("The PDF could not be rendered inside its safety limits")
    finally:
        if process is not None and process.poll() is None:
            terminate_process_group(process)


def _build_sandbox_command(
    bwrap_path: str,
    output_directory: str,
    renderer_command: list[str],
) -> list[str]:
    command = [
        bwrap_path,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/etc",
        "--dir",
        "/var",
        "--dir",
        "/var/cache",
        "--dir",
        "/run",
        "--dir",
        "/home",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for source in (
        "/lib",
        "/lib64",
        "/etc/fonts",
        "/etc/fontconfig",
        "/etc/ld.so.cache",
        "/etc/localtime",
        "/etc/passwd",
        "/etc/group",
        "/etc/nsswitch.conf",
        "/var/cache/fontconfig",
    ):
        if os.path.exists(source):
            command.extend(("--ro-bind", source, source))
    command.extend(
        (
            "--bind",
            output_directory,
            "/output",
            "--chdir",
            "/tmp",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "XDG_CACHE_HOME",
            "/tmp/cache",
            "--",
            *renderer_command,
        )
    )
    return command


def _read_output_png(path: str, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PdfPreviewError("The PDF renderer did not produce a preview") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise PdfPreviewError("The PDF renderer produced an invalid preview")
        if metadata.st_size > limit:
            raise PdfPreviewError("The rendered PDF page exceeds its output limit")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            raise PdfPreviewError("The rendered PDF page exceeds its output limit")
        return b"".join(chunks)
    except OSError as error:
        raise PdfPreviewError("The rendered PDF page could not be read") from error
    finally:
        os.close(descriptor)


def _validate_png_dimensions(data: bytes, max_edge: int) -> tuple[int, int]:
    if (
        len(data) < 33
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[12:16] != b"IHDR"
        or int.from_bytes(data[8:12], "big") != 13
    ):
        raise PdfPreviewError("The PDF renderer produced an invalid PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if (
        not width
        or not height
        or width > max_edge
        or height > max_edge
        or width * height > max_edge * max_edge
    ):
        raise PdfPreviewError("The rendered PDF page has unsafe dimensions")
    return width, height


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise PdfPreviewCancelled("PDF preview cancelled")


class _PdfPageLoader:
    """Coalesce rapid page requests without creating an unbounded thread queue."""

    def __init__(self, path: str, cancellable: Gio.Cancellable) -> None:
        self.path = path
        self.cancellable = cancellable
        self._lock = threading.Lock()
        self._generation = 0
        self._pending = None
        self._running = False
        self._delivery = None
        self._delivery_queued = False

    def request(self, page: int, on_page: Callable, on_error: ErrorCallback) -> None:
        if self.cancellable.is_cancelled():
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._delivery = None
            deadline = time.monotonic() + DEFAULT_LIMITS.wall_timeout_seconds
            self._pending = (generation, page, on_page, on_error, deadline)
            if self._running:
                return
            self._running = True
        try:
            threading.Thread(
                target=self._worker, name="kukni-pdf-renderer", daemon=True,
            ).start()
        except RuntimeError:
            with self._lock:
                self._running = False
                self._pending = None
            self._queue_delivery(generation, on_error, "The PDF preview worker could not start")

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self.cancellable.is_cancelled()

    def _worker(self) -> None:
        while True:
            with self._lock:
                request = self._pending
                self._pending = None
                if request is None or self.cancellable.is_cancelled():
                    self._running = False
                    return
            generation, page, on_page, on_error, deadline = request
            cancelled = lambda: not self._is_current(generation)
            admitted = False
            try:
                # @decision Superseded file requests may still be cleaning up
                # both children. Wait off GTK instead of making the newest PDF
                # permanently fall back merely because those slots are busy.
                # Admission, snapshot and Poppler share the request's deadline.
                while not admitted:
                    _check_request(cancelled, deadline)
                    admitted = _WORKER_SLOTS.acquire(
                        timeout=min(0.05, max(0.001, deadline - time.monotonic())),
                    )
                _check_request(cancelled, deadline)
                limits = replace(
                    DEFAULT_LIMITS, wall_timeout_seconds=max(0.001, deadline - time.monotonic()),
                )
                result = render_pdf_page(self.path, page, cancelled=cancelled, limits=limits)
            except PdfPreviewCancelled:
                continue
            except PdfPreviewError as error:
                self._queue_delivery(generation, on_error, str(error))
            except Exception:
                self._queue_delivery(generation, on_error, "The PDF preview could not be created safely")
            else:
                self._queue_delivery(generation, on_page, result)
            finally:
                if admitted:
                    _WORKER_SLOTS.release()

    def _queue_delivery(self, generation: int, callback: Callable, result) -> None:
        # @constraint A busy GTK loop must not accumulate one 32 MiB raster
        # per completed request. Keep one replaceable payload and one idle
        # notification; both requests and results invalidate obsolete pixels.
        with self._lock:
            if generation != self._generation or self.cancellable.is_cancelled():
                return
            self._delivery = (generation, callback, result)
            if self._delivery_queued:
                return
            self._delivery_queued = True
        GLib.idle_add(self._deliver_pending)

    def _deliver_pending(self) -> bool:
        with self._lock:
            delivery = self._delivery
            self._delivery = None
            self._delivery_queued = False
        if delivery is not None:
            generation, callback, result = delivery
            if self._is_current(generation):
                callback(result)
        return GLib.SOURCE_REMOVE


class PdfPreviewView(ImagePreviewView):
    """The shared zoom/pan canvas with one lazy, bounded PDF page at a time."""

    def __init__(
        self, texture: Gdk.Texture, page: PdfPage | None = None,
        loader: _PdfPageLoader | None = None,
    ) -> None:
        width, height = texture.get_width(), texture.get_height()
        if not (0 < width <= DEFAULT_LIMITS.max_edge_pixels
                and 0 < height <= DEFAULT_LIMITS.max_edge_pixels):
            raise PdfPreviewError("The rendered PDF page has unsafe dimensions")
        super().__init__(texture, width, height)
        self.add_css_class("pdf-preview")
        self.picture.add_css_class("pdf-page")
        self.picture.update_property([Gtk.AccessibleProperty.LABEL], ["PDF page preview"])
        self._loader = loader
        self.page_number = page.page_number if page else 1
        self.requested_page = self.page_number
        self.page_count = page.page_count if page else 1
        self.total_pages = page.total_pages if page else 1
        self.preview_geometry = ("pdf", width, height)
        self.previous_button = Gtk.Button(icon_name="go-previous-symbolic", focus_on_click=False)
        self.previous_button.add_css_class("flat")
        self.previous_button.update_property([Gtk.AccessibleProperty.LABEL], ["Previous PDF page"])
        self.previous_button.set_tooltip_text("Previous PDF page (Page Up)")
        self.previous_button.connect("clicked", lambda *_: self.change_page(-1))
        self.next_button = Gtk.Button(icon_name="go-next-symbolic", focus_on_click=False)
        self.next_button.add_css_class("flat")
        self.next_button.update_property([Gtk.AccessibleProperty.LABEL], ["Next PDF page"])
        self.next_button.set_tooltip_text("Next PDF page (Page Down)")
        self.next_button.connect("clicked", lambda *_: self.change_page(1))
        self.page_label = Gtk.Label()
        self.page_label.add_css_class("caption")
        self.toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.toolbar.append(self.previous_button)
        self.toolbar.append(self.page_label)
        self.toolbar.append(self.next_button)
        self._update_page_controls()

    def change_page(self, offset: int) -> None:
        self.request_page(self.requested_page + offset)

    def request_page(self, page_number: int) -> None:
        if (self._loader is None or self._loader.cancellable.is_cancelled()
                or not 1 <= page_number <= self.page_count
                or page_number == self.requested_page):
            return
        self.requested_page = page_number
        self._update_page_controls(loading=True)
        self._loader.request(page_number, self._show_page, self._page_error)

    def _show_page(self, page: PdfPage) -> None:
        try:
            _validate_png_dimensions(page.png, DEFAULT_LIMITS.max_edge_pixels)
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(page.png))
        except (GLib.Error, PdfPreviewError):
            self._page_error("The rendered PDF page could not be displayed")
            return
        self.set_texture(texture, texture.get_width(), texture.get_height())
        self.preview_geometry = ("pdf", texture.get_width(), texture.get_height())
        self.page_number = page.page_number
        self.requested_page = page.page_number
        self.page_count = page.page_count
        self.total_pages = page.total_pages
        self._update_page_controls()

    def _page_error(self, message: str) -> None:
        # A failed later page must not replace the successful document view or
        # invoke the once-only renderer callback. Retain the previous page.
        self.requested_page = self.page_number
        self._update_page_controls()
        self.page_label.set_label(f"Page {self.page_number} · preview unavailable")
        self.page_label.set_tooltip_text(message)

    def _update_page_controls(self, *, loading: bool = False) -> None:
        shown = self.requested_page if loading else self.page_number
        text = f"Page {shown} of {self.page_count}"
        detail = f"Page {shown} of {self.total_pages}"
        if self.total_pages > self.page_count:
            text += " · limited"
            detail += f"; preview is limited to the first {self.page_count} pages"
        if loading:
            text = f"Loading page {shown}…"
        self.page_label.set_label(text)
        self.page_label.set_tooltip_text(detail)
        self.previous_button.set_sensitive(self._loader is not None and shown > 1)
        self.next_button.set_sensitive(self._loader is not None and shown < self.page_count)


class PdfRenderer:
    """Resolve initial PDF capability off-thread; subsequent pages stay in-view."""

    id = "pdf"

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        # @constraint Registry selection runs on GTK's main context. Even a
        # cached sandbox probe can block for seconds on its first invocation.
        return (
            file.is_native()
            and info.get_file_type() == Gio.FileType.REGULAR
            and supports_pdf(file.get_basename(), info.get_content_type())
        )

    def render(
        self, file: Gio.File, _info: Gio.FileInfo, cancellable: Gio.Cancellable,
        on_ready: ReadyCallback, on_error: ErrorCallback,
    ) -> None:
        path = file.get_path() if file.is_native() else None
        if path is None:
            self._queue_error(cancellable, on_error, "PDF preview supports local files only")
            return
        loader = _PdfPageLoader(path, cancellable)

        def initial_page(page: PdfPage) -> None:
            try:
                _validate_png_dimensions(page.png, DEFAULT_LIMITS.max_edge_pixels)
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(page.png))
                view = PdfPreviewView(texture, page, loader)
            except (GLib.Error, PdfPreviewError):
                on_error("The rendered PDF page could not be displayed")
                return
            on_ready(view, "PDF document")

        loader.request(1, initial_page, on_error)

    @staticmethod
    def _queue_error(cancellable, on_error, message) -> None:
        def deliver() -> bool:
            if not cancellable.is_cancelled():
                on_error(message)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)
