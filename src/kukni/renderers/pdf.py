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
from .pdf_layout import PdfDocumentLayout


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

    def cancel_pending(self) -> None:
        """Invalidate obsolete page work without cancelling the document."""

        with self._lock:
            self._generation += 1
            self._pending = None
            self._delivery = None

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


class _PdfPageSlot(Gtk.Overlay):
    """One cheap placeholder whose paintable may be dropped independently."""

    def __init__(self, page: int) -> None:
        super().__init__(halign=Gtk.Align.CENTER, overflow=Gtk.Overflow.HIDDEN)
        self.page = page
        self.add_css_class("pdf-canvas")
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"PDF page {page} preview"],
        )
        self.set_child(self.picture)
        self.status = Gtk.Label(label=f"Page {page}", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.status.add_css_class("caption")
        self.add_overlay(self.status)

    # @why Texture intrinsic sizes must not override document layout. This also
    # keeps an unloaded placeholder allocation identical to its loaded page.
    def do_measure(self, orientation, _for_size):
        requested = self.get_size_request()
        size = requested[0 if orientation == Gtk.Orientation.HORIZONTAL else 1]
        size = max(1, size)
        return size, size, -1, -1

    def show_texture(self, texture: Gdk.Texture) -> None:
        self.picture.set_paintable(texture)
        self.status.set_visible(False)
        self.status.set_tooltip_text(None)

    def show_loading(self) -> None:
        if self.picture.get_paintable() is None:
            self.status.set_label(f"Loading page {self.page}…")
            self.status.set_tooltip_text(None)
            self.status.set_visible(True)

    def show_error(self, message: str) -> None:
        self.picture.set_paintable(None)
        self.status.set_label(f"Page {self.page} preview unavailable")
        self.status.set_tooltip_text(message)
        self.status.set_visible(True)

    def unload(self) -> None:
        self.picture.set_paintable(None)
        self.status.set_label(f"Page {self.page}")
        self.status.set_tooltip_text(None)
        self.status.set_visible(True)


class PdfPreviewView(Gtk.Box):
    """Width-fit, continuously scrollable PDF with a bounded lazy pixel cache."""

    _MAX_RETAINED_TEXTURES = 5

    def __init__(
        self, texture: Gdk.Texture, page: PdfPage | None = None,
        loader: _PdfPageLoader | None = None,
    ) -> None:
        width, height = texture.get_width(), texture.get_height()
        if not (0 < width <= DEFAULT_LIMITS.max_edge_pixels
                and 0 < height <= DEFAULT_LIMITS.max_edge_pixels):
            raise PdfPreviewError("The rendered PDF page has unsafe dimensions")
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("pdf-preview")
        self._loader = loader
        self.page_number = page.page_number if page else 1
        self.requested_page = self.page_number
        self.page_count = page.page_count if page else 1
        self.total_pages = page.total_pages if page else 1
        if not (1 <= self.page_number <= self.page_count <= DEFAULT_LIMITS.max_pages):
            raise PdfPreviewError("The PDF page count exceeds the preview page limit")
        self.preview_geometry = ("pdf", width, height)
        self.texture = texture
        self.source_width, self.source_height = width, height
        self.zoom = 1.0
        self.fit_mode = True
        self._zoom_basis = "width"
        self._layout = PdfDocumentLayout(
            self.page_count, width, height, max_pages=DEFAULT_LIMITS.max_pages,
        )
        self._slots = [_PdfPageSlot(number) for number in range(1, self.page_count + 1)]
        self._textures: dict[int, Gdk.Texture] = {self.page_number: texture}
        self._errors: dict[int, str] = {}
        self._loading_page: int | None = None
        self._wanted_pages: tuple[int, ...] = ()
        self._rects = ()
        self._viewport_width = 0
        self._layout_dirty = True
        self._tick_id = 0
        self._relayout_frames = 0
        self._pending_anchor: tuple[int, float] | None = None
        self._pending_horizontal_fraction: float | None = None
        self._initial_top = True
        self._pan_origin = (0.0, 0.0)
        self._pan_active = False

        self.canvas = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=self._layout.gap)
        self.canvas.set_margin_top(self._layout.margin)
        self.canvas.set_margin_bottom(self._layout.margin)
        for slot in self._slots:
            self.canvas.append(slot)
        self._slots[self.page_number - 1].show_texture(texture)
        self.picture = self._slots[self.page_number - 1].picture
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, focusable=False)
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.canvas)
        self.append(self.scroller)
        self.scroller.get_vadjustment().connect("value-changed", self._on_scroll_position)
        for adjustment in (self.scroller.get_hadjustment(), self.scroller.get_vadjustment()):
            adjustment.connect("changed", lambda *_args: self._queue_layout())

        # Only Ctrl-wheel is intercepted. Returning False for ordinary smooth
        # and discrete input leaves GTK's native scroll/kinetic machinery intact.
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self.scroller.add_controller(scroll)
        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.scroller.add_controller(drag)
        self.drag_gesture = drag

        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.toolbar.add_css_class("preview-controls")
        self.toolbar.set_halign(Gtk.Align.CENTER)
        self.toolbar.append(self._button("zoom-out-symbolic", "Zoom out (−)", self.zoom_out))
        self.zoom_label = Gtk.Label(width_chars=5)
        self.zoom_label.add_css_class("caption")
        self.zoom_label.add_css_class("numeric")
        self.toolbar.append(self.zoom_label)
        self.toolbar.append(self._button("zoom-in-symbolic", "Zoom in (+)", self.zoom_in))
        self.fit_button = Gtk.Button(label="Fit width", focus_on_click=False)
        self.fit_button.add_css_class("flat")
        self.fit_button.set_tooltip_text("Fit pages to window width (0)")
        self.fit_button.connect("clicked", lambda *_args: self.fit())
        self.toolbar.append(self.fit_button)
        self.actual_button = Gtk.Button(label="1:1", focus_on_click=False)
        self.actual_button.add_css_class("flat")
        self.actual_button.set_tooltip_text("One retained preview pixel per logical display unit (1)")
        self.actual_button.connect("clicked", lambda *_args: self.actual_size())
        self.toolbar.append(self.actual_button)
        self.previous_button = self._button(
            "go-previous-symbolic", "Previous PDF page (Page Up)", lambda: self.change_page(-1),
        )
        self.next_button = self._button(
            "go-next-symbolic", "Next PDF page (Page Down)", lambda: self.change_page(1),
        )
        self.page_label = Gtk.Label()
        self.page_label.add_css_class("caption")
        self.toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.toolbar.append(self.previous_button)
        self.toolbar.append(self.page_label)
        self.toolbar.append(self.next_button)
        self.append(self.toolbar)
        self._update_page_controls()
        self._update_zoom_controls()
        self.connect("map", lambda *_args: self._queue_layout())

    @property
    def retained_texture_count(self) -> int:
        return len(self._textures)

    def fit(self) -> None:
        self.fit_mode = True
        self._zoom_basis = "width"
        self.zoom = 1.0
        self._begin_relayout()

    def actual_size(self) -> None:
        self.fit_mode = False
        self._zoom_basis = "pixels"
        self.zoom = 1.0
        self._begin_relayout()

    def zoom_in(self) -> None:
        self._set_zoom(self.zoom * 1.25)

    def zoom_out(self) -> None:
        self._set_zoom(self.zoom / 1.25)

    def _set_zoom(self, zoom: float) -> None:
        self.fit_mode = False
        self.zoom = max(0.05, min(8.0, zoom))
        self._begin_relayout()

    def _begin_relayout(self) -> None:
        if self._rects:
            adjustment = self.scroller.get_vadjustment()
            self._pending_anchor = self._layout.capture_anchor(
                self._rects, adjustment.get_value(),
            )
            horizontal = self.scroller.get_hadjustment()
            self._pending_horizontal_fraction = (
                horizontal.get_value() + horizontal.get_page_size() / 2
            ) / max(1, horizontal.get_upper())
        self._relayout_frames = 2
        self._layout_dirty = True
        self._update_zoom_controls()
        self._queue_layout()

    def change_page(self, offset: int) -> None:
        self.request_page(self.page_number + offset)

    def request_page(self, page_number: int) -> None:
        if type(page_number) is not int or not 1 <= page_number <= self.page_count:
            return
        if self._loader is not None and self._loader.cancellable.is_cancelled():
            return
        self.requested_page = page_number
        self._errors.pop(page_number, None)
        # An explicit page command wins over an older lazy-resize anchor.
        self._pending_anchor = None
        self._initial_top = False
        if self._rects:
            adjustment = self.scroller.get_vadjustment()
            adjustment.set_value(self._rects[page_number - 1].y - self._layout.margin)
        self.page_number = page_number
        self._set_current_page_properties()
        self._refresh_visible_pages(force_page=page_number)

    def _on_scroll_position(self, _adjustment) -> None:
        if not self._pending_anchor:
            self._refresh_visible_pages()
        self._queue_layout()

    def _on_scroll(self, controller, _dx, dy) -> bool:
        if not controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            return False
        if dy < 0:
            self.zoom_in()
        elif dy > 0:
            self.zoom_out()
        return True

    def pan_to(self, x: float, y: float) -> None:
        self._pending_anchor = None
        self.scroller.get_hadjustment().set_value(x)
        self.scroller.get_vadjustment().set_value(y)

    def _on_drag_begin(self, gesture, _x, _y) -> None:
        adjustments = (self.scroller.get_hadjustment(), self.scroller.get_vadjustment())
        self._pan_active = any(
            adjustment.get_upper() > adjustment.get_page_size()
            for adjustment in adjustments
        )
        if self._pan_active:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._pan_origin = tuple(adjustment.get_value() for adjustment in adjustments)
            self.scroller.set_cursor_from_name("grabbing")

    def _on_drag_update(self, _gesture, dx, dy) -> None:
        if self._pan_active:
            self.pan_to(self._pan_origin[0] - dx, self._pan_origin[1] - dy)

    def _on_drag_end(self, _gesture, _dx, _dy) -> None:
        self._pan_active = False
        self.scroller.set_cursor_from_name("default" if self.fit_mode else "grab")

    def _queue_layout(self) -> None:
        if not self._tick_id:
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _widget, _clock) -> bool:
        horizontal = self.scroller.get_hadjustment()
        viewport_width = round(horizontal.get_page_size())
        if viewport_width <= 1:
            viewport_width = self.scroller.get_width()
        viewport_width = max(1, viewport_width)
        needs_layout = self._layout_dirty or viewport_width != self._viewport_width or not self._rects
        if needs_layout:
            if self._rects and self._pending_anchor is None and not self._initial_top:
                self._pending_anchor = self._layout.capture_anchor(
                    self._rects, self.scroller.get_vadjustment().get_value(),
                )
            self._viewport_width = viewport_width
            self._layout_dirty = False
            self._rects = self._layout.rects(
                viewport_width, basis=self._zoom_basis, zoom=self.zoom,
            )
            for slot, rect in zip(self._slots, self._rects):
                slot.set_size_request(rect.width, rect.height)
            self._relayout_frames = max(self._relayout_frames, 2)
        if self._relayout_frames:
            self._relayout_frames -= 1
            return True
        vertical = self.scroller.get_vadjustment()
        if self._initial_top:
            vertical.set_value(0)
            self._initial_top = False
        elif self._pending_anchor is not None:
            vertical.set_value(self._layout.restore_anchor(self._rects, self._pending_anchor))
        self._pending_anchor = None
        if self._pending_horizontal_fraction is not None:
            horizontal = self.scroller.get_hadjustment()
            horizontal.set_value(
                self._pending_horizontal_fraction * horizontal.get_upper()
                - horizontal.get_page_size() / 2
            )
            self._pending_horizontal_fraction = None
        self._refresh_visible_pages()
        self._tick_id = 0
        return False

    def _refresh_visible_pages(self, *, force_page: int | None = None) -> None:
        if not self._rects:
            self._queue_layout()
            return
        vertical = self.scroller.get_vadjustment()
        wanted = list(self._layout.visible_pages(
            self._rects, vertical.get_value(), max(1, vertical.get_page_size()), adjacent=1,
        ))
        if force_page is not None:
            wanted = [force_page, *(item for item in wanted if item != force_page)]
        self._wanted_pages = tuple(wanted[:self._MAX_RETAINED_TEXTURES])
        if force_page is None:
            self.page_number = self._layout.page_at_viewport_center(
                self._rects, vertical.get_value(), max(1, vertical.get_page_size()),
            )
            self.requested_page = self.page_number
        self._set_current_page_properties()
        self._evict_distant_textures()
        self._request_next_page()

    def _request_next_page(self) -> None:
        if self._loader is None or self._loader.cancellable.is_cancelled():
            return
        candidates = [
            page for page in self._wanted_pages
            if page not in self._textures and page not in self._errors
        ]
        if self._loading_page is not None and self._loading_page not in self._wanted_pages:
            obsolete = self._loading_page
            self._loading_page = None
            if obsolete not in self._textures and obsolete not in self._errors:
                self._slots[obsolete - 1].unload()
            if not candidates:
                self._loader.cancel_pending()
                self._update_page_controls()
                return
        if not candidates:
            return
        if self._loading_page in self._wanted_pages:
            return
        target = candidates[0]
        self._loading_page = target
        self._slots[target - 1].show_loading()
        self._update_page_controls()
        self._loader.request(
            target,
            self._show_page,
            lambda message, requested=target: self._page_error(requested, message),
        )

    def _show_page(self, page: PdfPage) -> None:
        try:
            width, height = _validate_png_dimensions(page.png, DEFAULT_LIMITS.max_edge_pixels)
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(page.png))
        except (GLib.Error, PdfPreviewError):
            self._page_error(page.page_number, "The rendered PDF page could not be displayed")
            return
        if not 1 <= page.page_number <= self.page_count:
            return
        anchor = self._layout.capture_anchor(
            self._rects, self.scroller.get_vadjustment().get_value(),
        ) if self._rects else None
        self._layout.set_dimensions(page.page_number, width, height)
        self._textures[page.page_number] = texture
        self._slots[page.page_number - 1].show_texture(texture)
        self._errors.pop(page.page_number, None)
        self._loading_page = None
        self.total_pages = page.total_pages
        if anchor is not None:
            self._pending_anchor = anchor
        self._relayout_frames = 2
        self._rects = self._layout.rects(
            max(1, round(self.scroller.get_hadjustment().get_page_size())),
            basis=self._zoom_basis, zoom=self.zoom,
        )
        for slot, rect in zip(self._slots, self._rects):
            slot.set_size_request(rect.width, rect.height)
        self._evict_distant_textures()
        self._set_current_page_properties()
        self._update_page_controls()
        self._queue_layout()

    def _page_error(self, page: int, message: str) -> None:
        if not 1 <= page <= self.page_count:
            return
        if self._loading_page == page:
            self._loading_page = None
        self._errors[page] = message
        self._slots[page - 1].show_error(message)
        self._update_page_controls()
        self._request_next_page()

    def _evict_distant_textures(self) -> None:
        keep = set(self._wanted_pages)
        keep.add(self.page_number)
        while len(self._textures) > self._MAX_RETAINED_TEXTURES:
            victim = max(
                (page for page in self._textures if page not in keep),
                key=lambda page: abs(page - self.page_number),
                default=max(self._textures, key=lambda page: abs(page - self.page_number)),
            )
            del self._textures[victim]
            self._slots[victim - 1].unload()

    def _set_current_page_properties(self) -> None:
        slot = self._slots[self.page_number - 1]
        self.picture = slot.picture
        texture = self._textures.get(self.page_number)
        if texture is not None:
            self.texture = texture
            self.source_width, self.source_height = self._layout.dimensions(self.page_number)
        elif hasattr(self, "texture"):
            # The compatibility property must not pin an otherwise-evicted
            # off-screen texture outside the explicit cache bound.
            del self.texture
        self._update_page_controls()

    def _update_page_controls(self) -> None:
        text = f"Page {self.page_number} of {self.page_count}"
        detail = f"Page {self.page_number} of {self.total_pages}"
        if self.total_pages > self.page_count:
            text += " · limited"
            detail += f"; preview is limited to the first {self.page_count} pages"
        if self.page_number in self._errors:
            text += " · preview unavailable"
            detail = self._errors[self.page_number]
        elif self._loading_page == self.page_number:
            text = f"Loading page {self.page_number}…"
        self.page_label.set_label(text)
        self.page_label.set_tooltip_text(detail)
        available = self._loader is not None and not self._loader.cancellable.is_cancelled()
        self.previous_button.set_sensitive(available and self.page_number > 1)
        self.next_button.set_sensitive(available and self.page_number < self.page_count)

    def _update_zoom_controls(self) -> None:
        self.zoom_label.set_label(f"{round(self.zoom * 100)}%")
        basis = "page width" if self._zoom_basis == "width" else "retained preview pixels"
        self.zoom_label.set_tooltip_text(f"Scale relative to {basis}")
        self.fit_button.set_sensitive(not self.fit_mode)
        self.scroller.set_cursor_from_name("default" if self.fit_mode else "grab")

    @staticmethod
    def _button(icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon, focus_on_click=False)
        button.add_css_class("flat")
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        button.connect("clicked", lambda *_args: callback())
        return button


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
