# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded, fit-page PDF previews rendered by a short-lived Poppler child."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import functools
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from .base import ErrorCallback, ReadyCallback
from .html import probe_bwrap_user_namespace


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
def pdf_runtime_available() -> bool:
    """Return whether Poppler can run inside a resource-limited bwrap sandbox."""

    renderer = shutil.which("pdftoppm")
    limiter = shutil.which("prlimit")
    bwrap = shutil.which("bwrap")
    true = shutil.which("true")
    return bool(
        renderer
        and limiter
        and bwrap
        and true
        and probe_bwrap_user_namespace(bwrap, true)
    )


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
    """Render page one to bounded PNG bytes without reopening the input path."""

    _check_cancelled(cancelled)
    renderer = pdftoppm_path or shutil.which("pdftoppm")
    if renderer is None:
        raise PdfPreviewError("Poppler's pdftoppm tool is required for PDF previews")
    limiter = prlimit_path if prlimit_path is not None else shutil.which("prlimit")
    if limiter is None:
        raise PdfPreviewError("The PDF resource-limit helper is unavailable")
    bwrap = bwrap_path if bwrap_path is not None else shutil.which("bwrap")
    true = shutil.which("true")
    default_runtime = (
        pdftoppm_path is None
        and prlimit_path is None
        and bwrap_path is None
    )
    sandbox_ready = (
        pdf_runtime_available()
        if default_runtime
        else bool(
            bwrap
            and true
            and probe_bwrap_user_namespace(bwrap, true)
        )
    )
    if bwrap is None or not sandbox_ready:
        raise PdfPreviewError("The PDF sandbox is unavailable on this system")

    input_descriptor = -1
    snapshot = None
    snapshot_descriptor = -1
    process: subprocess.Popen | None = None
    temporary_directory = tempfile.mkdtemp(prefix="kukni-pdf-")
    output_directory = os.path.join(temporary_directory, "output")
    host_output_path = os.path.join(output_directory, "page.png")
    try:
        os.mkdir(output_directory, mode=0o700)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
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
        _check_cancelled(cancelled)

        snapshot = tempfile.TemporaryFile(
            mode="w+b",
            dir=temporary_directory,
        )
        total = 0
        while total <= limits.max_input_bytes:
            _check_cancelled(cancelled)
            try:
                chunk = os.read(
                    input_descriptor,
                    min(64 * 1024, limits.max_input_bytes + 1 - total),
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
        renderer_command = [
            renderer,
            "-q",
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            "-cropbox",
            "-scale-to",
            str(limits.max_edge_pixels),
            "-png",
            input_path,
            "/output/page",
        ]
        sandbox_command = _build_sandbox_command(
            bwrap,
            output_directory,
            renderer_command,
        )
        command = [
            limiter,
            f"--as={limits.max_address_space_bytes}",
            f"--cpu={limits.max_cpu_seconds}",
            f"--fsize={limits.max_output_bytes}",
            f"--nofile={limits.max_open_files}",
            "--core=0",
            "--",
            *sandbox_command,
        ]

        environment = {
            "HOME": temporary_directory,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": temporary_directory,
            "XDG_CACHE_HOME": os.path.join(temporary_directory, "cache"),
        }
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(snapshot_descriptor,),
                start_new_session=True,
                env=environment,
                cwd=temporary_directory,
            )
        except OSError as error:
            raise PdfPreviewError("The PDF rendering worker could not start") from error

        deadline = time.monotonic() + limits.wall_timeout_seconds
        while process.poll() is None:
            if cancelled is not None and cancelled():
                _terminate_process(process)
                raise PdfPreviewCancelled("PDF preview cancelled")
            if time.monotonic() >= deadline:
                _terminate_process(process)
                raise PdfPreviewError("PDF preview timed out")
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass

        if process.returncode != 0:
            raise PdfPreviewError("The PDF could not be rendered")
        _check_cancelled(cancelled)
        png = _read_output_png(host_output_path, limits.max_output_bytes)
        _validate_png_dimensions(png, limits.max_edge_pixels)
        return png
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        if input_descriptor >= 0:
            os.close(input_descriptor)
        if snapshot is not None:
            snapshot.close()
        if snapshot_descriptor >= 0:
            os.close(snapshot_descriptor)
        shutil.rmtree(temporary_directory, ignore_errors=True)


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
    flags |= getattr(os, "O_NOFOLLOW", 0)
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


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise PdfPreviewCancelled("PDF preview cancelled")


class PdfPreviewView(Gtk.Box):
    """A stable fit-page canvas; document dimensions never resize the window."""

    def __init__(self, texture: Gdk.Texture) -> None:
        if (
            texture.get_width() > DEFAULT_LIMITS.max_edge_pixels
            or texture.get_height() > DEFAULT_LIMITS.max_edge_pixels
        ):
            raise PdfPreviewError("The rendered PDF page has unsafe dimensions")
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
            hexpand=True,
            vexpand=True,
        )
        self.add_css_class("pdf-preview")
        self.texture = texture

        canvas = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        canvas.add_css_class("pdf-canvas")
        canvas.set_margin_top(20)
        canvas.set_margin_bottom(20)
        canvas.set_margin_start(20)
        canvas.set_margin_end(20)

        self.picture = Gtk.Picture(
            paintable=texture,
            content_fit=Gtk.ContentFit.CONTAIN,
            can_shrink=True,
            hexpand=True,
            vexpand=True,
        )
        self.picture.set_focusable(False)
        self.picture.set_can_focus(False)
        self.picture.add_css_class("pdf-page")
        canvas.append(self.picture)
        self.append(canvas)

        status = Gtk.Label(label="Page 1 · Fit Page")
        status.set_margin_bottom(10)
        status.add_css_class("caption")
        status.add_css_class("dim-label")
        self.append(status)


class PdfRenderer:
    """Render local PDFs off-thread and deliver one fitted page on GTK's context."""

    id = "pdf"

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        return (
            file.is_native()
            and info.get_file_type() == Gio.FileType.REGULAR
            and supports_pdf(file.get_basename(), info.get_content_type())
            and pdf_runtime_available()
        )

    def render(
        self,
        file: Gio.File,
        _info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        path = file.get_path() if file.is_native() else None
        if path is None:
            self._queue_error(cancellable, on_error, "PDF preview supports local files only")
            return
        if cancellable.is_cancelled():
            return

        def worker() -> None:
            if not _WORKER_SLOTS.acquire(blocking=False):
                self._queue_error(
                    cancellable,
                    on_error,
                    "PDF preview is busy; showing file details instead",
                )
                return
            try:
                try:
                    png = render_pdf_first_page(
                        path,
                        cancelled=cancellable.is_cancelled,
                    )
                except PdfPreviewCancelled:
                    return
                except PdfPreviewError as error:
                    self._queue_error(cancellable, on_error, str(error))
                    return
                except Exception:
                    self._queue_error(
                        cancellable,
                        on_error,
                        "The PDF preview could not be created safely",
                    )
                    return
            finally:
                _WORKER_SLOTS.release()
            GLib.idle_add(
                self._deliver_preview,
                cancellable,
                on_ready,
                on_error,
                png,
            )

        try:
            threading.Thread(
                target=worker,
                name="kukni-pdf-renderer",
                daemon=True,
            ).start()
        except RuntimeError:
            self._queue_error(
                cancellable,
                on_error,
                "The PDF preview worker could not be started",
            )

    @staticmethod
    def _queue_error(
        cancellable: Gio.Cancellable,
        on_error: ErrorCallback,
        message: str,
    ) -> None:
        GLib.idle_add(PdfRenderer._deliver_error, cancellable, on_error, message)

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
    def _deliver_preview(
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
        png: bytes,
    ) -> bool:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        try:
            _validate_png_dimensions(png, DEFAULT_LIMITS.max_edge_pixels)
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
            view = PdfPreviewView(texture)
        except (GLib.Error, PdfPreviewError):
            on_error("The rendered PDF page could not be displayed")
            return GLib.SOURCE_REMOVE
        on_ready(view, "PDF document · page 1 · fit page")
        return GLib.SOURCE_REMOVE
