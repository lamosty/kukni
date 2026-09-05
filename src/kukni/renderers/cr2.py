# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Supervise disposable Canon CR2 extraction and JPEG decode workers.

No encoded image bytes are decoded in the UI process. The worker imports the
existing bounded pure-Python CR2 extractor, decodes and orients its selected
JPEG, and returns only tightly packed RGBA plus strictly validated metadata.

The worker has hard CPU, address-space, file-size, descriptor and task limits,
and enables ``PR_SET_NO_NEW_PRIVS`` before reading the CR2. It does not yet
have a mount or network namespace: a native decoder compromise would still
have the worker's ordinary user-level filesystem, network and IPC/signalling
access during its short lifetime. The fixed descriptor-only protocol limits
intended access, but is not a filesystem or network sandbox.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import functools
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from ..worker import terminate_process_group
from .base import ErrorCallback, ReadyCallback


PROTOCOL_VERSION = 1
PIXEL_FORMAT = "rgba8"
CR2_CONTENT_TYPES = frozenset(("image/x-canon-cr2", "image/x-cr2"))
CR2_SUFFIX = ".cr2"
GENERIC_CONTENT_TYPES = frozenset(
    ("", "application/octet-stream", "application/x-empty", "inode/x-empty")
)
WORKER_POLL_SECONDS = 0.05
MAX_HELPER_BYTES = 1024 * 1024
_TRUSTED_EXECUTABLE_DIRECTORIES = (Path("/usr/bin"), Path("/bin"))

CancellationCheck = Callable[[], bool]
ProcessFactory = Callable[..., Any]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Cr2Limits:
    """Hard process, protocol and retained-image limits for one preview."""

    max_input_bytes: int = 128 * 1024 * 1024
    max_jpeg_bytes: int = 64 * 1024 * 1024
    max_source_edge: int = 32_768
    max_source_pixels: int = 100_000_000
    max_render_edge: int = 4_096
    max_render_pixels: int = 4_096 * 4_096
    max_pixel_bytes: int = 4_096 * 4_096 * 4
    max_result_bytes: int = 1_024
    max_address_space_bytes: int = 768 * 1024 * 1024
    max_cpu_seconds: int = 6
    wall_timeout_seconds: float = 8.0
    max_open_files: int = 64

    def __post_init__(self) -> None:
        integers = (
            self.max_input_bytes,
            self.max_jpeg_bytes,
            self.max_source_edge,
            self.max_source_pixels,
            self.max_render_edge,
            self.max_render_pixels,
            self.max_pixel_bytes,
            self.max_result_bytes,
            self.max_address_space_bytes,
            self.max_cpu_seconds,
            self.max_open_files,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integers
        ):
            raise ValueError("CR2 worker integer limits must be positive")
        if (
            isinstance(self.wall_timeout_seconds, bool)
            or not isinstance(self.wall_timeout_seconds, (int, float))
            or not math.isfinite(float(self.wall_timeout_seconds))
            or self.wall_timeout_seconds <= 0
        ):
            raise ValueError("CR2 worker wall timeout must be positive and finite")
        if self.max_render_edge > self.max_source_edge:
            raise ValueError("CR2 render edge cannot exceed the source edge limit")
        if self.max_render_pixels > self.max_source_pixels:
            raise ValueError("CR2 render pixels cannot exceed the source pixel limit")
        if self.max_pixel_bytes < self.max_render_pixels * 4:
            raise ValueError("CR2 pixel limit cannot hold the maximum render")


DEFAULT_LIMITS = Cr2Limits()

# @decision Keep exactly one preparation admitted until its GTK delivery has
# consumed or discarded the raw payload. This bounds queued worker output even
# when navigation outruns the main context.
_WORKER_SLOT = threading.BoundedSemaphore(1)


class Cr2PreviewError(RuntimeError):
    """A CR2 worker or its result violated the bounded preview contract."""


class Cr2PreviewCancelled(Exception):
    """CR2 preparation stopped because its request was superseded."""


@dataclass(frozen=True, slots=True)
class Cr2WorkerResult:
    width: int
    height: int
    source_width: int
    source_height: int
    stride: int
    pixel_bytes: int


@dataclass(frozen=True, slots=True)
class Cr2WorkerOutput:
    result: Cr2WorkerResult
    pixels: bytes


@dataclass(frozen=True, slots=True)
class Cr2WorkerLaunch:
    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]
    environment: tuple[tuple[str, str], ...] = (
        ("LANG", "C.UTF-8"),
        ("LC_ALL", "C.UTF-8"),
        ("PATH", "/usr/bin:/bin"),
    )


@dataclass(frozen=True, slots=True)
class _InputSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


_RESULT_FIELDS = frozenset(
    (
        "version",
        "format",
        "width",
        "height",
        "source_width",
        "source_height",
        "stride",
        "pixel_bytes",
    )
)


def supports_cr2(filename: str | None, content_type: str | None) -> bool:
    """Return whether trusted metadata identifies a Canon CR2 container."""

    normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if normalized_type in CR2_CONTENT_TYPES:
        return True
    generic_type = normalized_type in GENERIC_CONTENT_TYPES
    if normalized_type and not generic_type:
        try:
            generic_type = Gio.content_type_is_unknown(normalized_type)
        except (TypeError, GLib.Error):
            generic_type = False
    return bool(
        generic_type
        and filename
        and filename.casefold().endswith(CR2_SUFFIX)
    )


@functools.lru_cache(maxsize=1)
def cr2_runtime_available() -> bool:
    """Return whether the fixed worker boundary is present on this system."""

    try:
        _resolve_cr2_runtime(
            prlimit_path=None,
            python_path=None,
            worker_path=None,
        )
    except Cr2PreviewError:
        return False
    return True


def parse_worker_result(
    payload: bytes,
    *,
    limits: Cr2Limits = DEFAULT_LIMITS,
) -> Cr2WorkerResult:
    """Parse an exact result without accepting worker-controlled text."""

    if not isinstance(payload, bytes):
        raise TypeError("CR2 worker result must be bytes")
    if not payload or len(payload) > limits.max_result_bytes:
        raise Cr2PreviewError("The CR2 worker result has an invalid size")

    def reject_constant(_value: str) -> None:
        raise Cr2PreviewError("The CR2 worker result contains an invalid value")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Cr2PreviewError("The CR2 worker result repeats a field")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("ascii", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise Cr2PreviewError("The CR2 worker result is not valid JSON") from error
    if not isinstance(value, dict) or frozenset(value) != _RESULT_FIELDS:
        raise Cr2PreviewError("The CR2 worker result fields are invalid")
    if type(value["version"]) is not int or value["version"] != PROTOCOL_VERSION:
        raise Cr2PreviewError("The CR2 worker protocol version is unsupported")
    if value["format"] != PIXEL_FORMAT:
        raise Cr2PreviewError("The CR2 worker pixel format is unsupported")

    integer_fields = (
        "width",
        "height",
        "source_width",
        "source_height",
        "stride",
        "pixel_bytes",
    )
    if any(
        isinstance(value[name], bool)
        or not isinstance(value[name], int)
        or value[name] <= 0
        for name in integer_fields
    ):
        raise Cr2PreviewError("The CR2 worker dimensions are invalid")
    width = value["width"]
    height = value["height"]
    source_width = value["source_width"]
    source_height = value["source_height"]
    stride = value["stride"]
    pixel_bytes = value["pixel_bytes"]
    if (
        width > limits.max_render_edge
        or height > limits.max_render_edge
        or width * height > limits.max_render_pixels
        or source_width > limits.max_source_edge
        or source_height > limits.max_source_edge
        or source_width * source_height > limits.max_source_pixels
        or stride != width * 4
        or pixel_bytes != stride * height
        or pixel_bytes > limits.max_pixel_bytes
    ):
        raise Cr2PreviewError("The CR2 worker dimensions exceed the limits")
    return Cr2WorkerResult(
        width=width,
        height=height,
        source_width=source_width,
        source_height=source_height,
        stride=stride,
        pixel_bytes=pixel_bytes,
    )


def build_cr2_worker_launch(
    *,
    prlimit_path: str,
    python_path: str,
    worker_path: str,
    input_fd: int,
    pixels_fd: int,
    result_fd: int,
    limits: Cr2Limits = DEFAULT_LIMITS,
) -> Cr2WorkerLaunch:
    """Build the fixed no-shell worker argv and hard resource limits."""

    _validate_launch_descriptors(
        input_fd=input_fd,
        pixels_fd=pixels_fd,
        result_fd=result_fd,
        limits=limits,
    )
    for path in (prlimit_path, python_path, worker_path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("CR2 worker runtime paths must be absolute")
    descriptors = (input_fd, pixels_fd, result_fd)
    argv = (
        prlimit_path,
        f"--as={limits.max_address_space_bytes}",
        f"--cpu={limits.max_cpu_seconds}",
        f"--fsize={max(limits.max_pixel_bytes, limits.max_result_bytes)}",
        f"--nofile={limits.max_open_files}",
        "--nproc=0:0",
        "--core=0",
        "--",
        python_path,
        "-I",
        "-B",
        worker_path,
        "--input-fd",
        str(input_fd),
        "--pixels-fd",
        str(pixels_fd),
        "--result-fd",
        str(result_fd),
        "--max-input-bytes",
        str(limits.max_input_bytes),
        "--max-jpeg-bytes",
        str(limits.max_jpeg_bytes),
        "--max-source-edge",
        str(limits.max_source_edge),
        "--max-source-pixels",
        str(limits.max_source_pixels),
        "--max-render-edge",
        str(limits.max_render_edge),
        "--max-render-pixels",
        str(limits.max_render_pixels),
        "--max-pixel-bytes",
        str(limits.max_pixel_bytes),
        "--max-result-bytes",
        str(limits.max_result_bytes),
        "--max-address-space-bytes",
        str(limits.max_address_space_bytes),
        "--max-cpu-seconds",
        str(limits.max_cpu_seconds),
        "--max-open-files",
        str(limits.max_open_files),
    )
    return Cr2WorkerLaunch(argv=argv, pass_fds=descriptors)


def run_cr2_worker(
    path: str | os.PathLike[str],
    *,
    limits: Cr2Limits = DEFAULT_LIMITS,
    cancelled: CancellationCheck | None = None,
    prlimit_path: str | os.PathLike[str] | None = None,
    python_path: str | os.PathLike[str] | None = None,
    worker_path: str | os.PathLike[str] | None = None,
    process_factory: ProcessFactory = subprocess.Popen,
    clock: Clock = time.monotonic,
) -> Cr2WorkerOutput:
    """Run one disposable decoder and accept only bounded raw RGBA output.

    @constraint ``O_NONBLOCK`` prevents FIFO/device opens from waiting, but an
    individual Linux pathname-resolution/open syscall cannot be cancelled by
    this userspace deadline.
    """

    _check_cancelled(cancelled)
    deadline = clock() + limits.wall_timeout_seconds
    input_fd = -1
    pixels_fd = -1
    result_fd = -1
    output_directory: str | None = None
    process: Any | None = None
    try:
        runtime = _resolve_cr2_runtime(
            prlimit_path=prlimit_path,
            python_path=python_path,
            worker_path=worker_path,
        )
        _check_deadline(deadline, clock)
        input_fd, before = _open_input(path, limits)
        _check_cancelled(cancelled)
        _check_deadline(deadline, clock)
        output_directory, pixels_fd, result_fd = _create_private_outputs()
        _check_deadline(deadline, clock)
        launch = build_cr2_worker_launch(
            prlimit_path=runtime[0],
            python_path=runtime[1],
            worker_path=runtime[2],
            input_fd=input_fd,
            pixels_fd=pixels_fd,
            result_fd=result_fd,
            limits=limits,
        )
        _check_deadline(deadline, clock)
        try:
            process = process_factory(
                launch.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=launch.pass_fds,
                start_new_session=True,
                shell=False,
                env=dict(launch.environment),
            )
        except (OSError, ValueError) as error:
            raise Cr2PreviewError("The CR2 worker could not start") from error

        _wait_for_worker(
            process,
            pixels_fd=pixels_fd,
            result_fd=result_fd,
            limits=limits,
            deadline=deadline,
            cancelled=cancelled,
            clock=clock,
        )
        if process.returncode != 0:
            raise Cr2PreviewError("The CR2 preview worker failed")
        _check_cancelled(cancelled)
        _check_deadline(deadline, clock)
        _check_unchanged(input_fd, before)
        result_payload = _read_private_output(
            result_fd,
            limits.max_result_bytes,
            deadline=deadline,
            cancelled=cancelled,
            clock=clock,
        )
        result = parse_worker_result(result_payload, limits=limits)
        pixels = _read_private_output(
            pixels_fd,
            limits.max_pixel_bytes,
            deadline=deadline,
            cancelled=cancelled,
            clock=clock,
        )
        if len(pixels) != result.pixel_bytes:
            raise Cr2PreviewError("The CR2 pixel output has an invalid size")
        _check_deadline(deadline, clock)
        _check_cancelled(cancelled)
        return Cr2WorkerOutput(result=result, pixels=pixels)
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    terminate_process_group(process)
                else:
                    process.wait()
            except Exception:
                # Cleanup must not replace the bounded error/cancellation which
                # caused this path; all output is discarded regardless.
                pass
        for descriptor in (input_fd, pixels_fd, result_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if output_directory is not None:
            shutil.rmtree(output_directory, ignore_errors=True)


def _wait_for_worker(
    process: Any,
    *,
    pixels_fd: int,
    result_fd: int,
    limits: Cr2Limits,
    deadline: float,
    cancelled: CancellationCheck | None,
    clock: Clock,
) -> None:
    while True:
        _check_cancelled(cancelled)
        now = clock()
        if now >= deadline:
            raise Cr2PreviewError("CR2 preview preparation timed out")
        returncode = process.poll()
        if returncode is not None:
            if clock() >= deadline:
                raise Cr2PreviewError("CR2 preview preparation timed out")
            return
        try:
            if os.fstat(pixels_fd).st_size > limits.max_pixel_bytes:
                raise Cr2PreviewError("The CR2 pixel output exceeds its limit")
            if os.fstat(result_fd).st_size > limits.max_result_bytes:
                raise Cr2PreviewError("The CR2 result output exceeds its limit")
        except OSError as error:
            raise Cr2PreviewError(
                "The CR2 worker output could not be verified"
            ) from error
        try:
            process.wait(timeout=min(WORKER_POLL_SECONDS, deadline - now))
        except subprocess.TimeoutExpired:
            continue
        if clock() >= deadline:
            raise Cr2PreviewError("CR2 preview preparation timed out")
        return


def _helper_paths_for_module(
    module_path: str | os.PathLike[str],
) -> tuple[Path, Path]:
    application_root = Path(module_path).resolve().parents[3]
    helper_root = application_root / "helpers"
    return (
        helper_root / "kukni-cr2-worker.py",
        helper_root / "kukni-extract-preview.py",
    )


def _default_helper_paths() -> tuple[Path, Path]:
    return _helper_paths_for_module(__file__)


def _resolve_cr2_runtime(
    *,
    prlimit_path: str | os.PathLike[str] | None,
    python_path: str | os.PathLike[str] | None,
    worker_path: str | os.PathLike[str] | None,
) -> tuple[str, str, str, str]:
    default_worker, _default_extractor = _default_helper_paths()
    resolved_prlimit = (
        _resolve_trusted_executable("prlimit")
        if prlimit_path is None
        else _require_executable(prlimit_path, "prlimit")
    )
    resolved_python = (
        _require_executable(sys.executable, "Python")
        if python_path is None
        else _require_executable(python_path, "Python")
    )
    resolved_worker = _require_helper(
        default_worker if worker_path is None else worker_path,
        "worker",
    )
    resolved_extractor = _require_helper(
        Path(resolved_worker).with_name("kukni-extract-preview.py"),
        "extractor",
    )
    return resolved_prlimit, resolved_python, resolved_worker, resolved_extractor


def _resolve_trusted_executable(name: str) -> str:
    for directory in _TRUSTED_EXECUTABLE_DIRECTORIES:
        candidate = directory / name
        try:
            return _require_executable(candidate, name)
        except Cr2PreviewError:
            continue
    raise Cr2PreviewError(f"The trusted CR2 {name} runtime is unavailable")


def _require_executable(path: str | os.PathLike[str], label: str) -> str:
    try:
        resolved = Path(path).resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, TypeError, ValueError) as error:
        raise Cr2PreviewError(f"The CR2 {label} runtime is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise Cr2PreviewError(f"The CR2 {label} runtime is unavailable")
    return os.fspath(resolved)


def _require_helper(path: str | os.PathLike[str], label: str) -> str:
    try:
        resolved = Path(path).resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, TypeError, ValueError) as error:
        raise Cr2PreviewError(f"The CR2 {label} helper is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_HELPER_BYTES
    ):
        raise Cr2PreviewError(f"The CR2 {label} helper is unavailable")
    return os.fspath(resolved)


def _open_input(
    path: str | os.PathLike[str],
    limits: Cr2Limits,
) -> tuple[int, _InputSnapshot]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOCTTY", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(os.fspath(path), flags)
    except (OSError, TypeError, ValueError) as error:
        raise Cr2PreviewError("The CR2 file could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise Cr2PreviewError("CR2 preview requires a regular local file")
        if metadata.st_size <= 0:
            raise Cr2PreviewError("The CR2 file is empty")
        if metadata.st_size > limits.max_input_bytes:
            raise Cr2PreviewError("The CR2 file exceeds the input size limit")
        return descriptor, _snapshot(metadata)
    except Exception:
        os.close(descriptor)
        raise


def _create_private_outputs() -> tuple[str, int, int]:
    directory = tempfile.mkdtemp(prefix="kukni-cr2-worker-")
    try:
        os.chmod(directory, 0o700)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
        )
        pixels_fd = os.open(os.path.join(directory, "pixels.rgba"), flags, 0o600)
        try:
            result_fd = os.open(os.path.join(directory, "result.json"), flags, 0o600)
        except Exception:
            os.close(pixels_fd)
            raise
        return directory, pixels_fd, result_fd
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _validate_launch_descriptors(
    *,
    input_fd: int,
    pixels_fd: int,
    result_fd: int,
    limits: Cr2Limits,
) -> None:
    descriptors = (input_fd, pixels_fd, result_fd)
    if any(
        isinstance(descriptor, bool)
        or not isinstance(descriptor, int)
        or descriptor < 3
        for descriptor in descriptors
    ) or len(set(descriptors)) != 3:
        raise ValueError("CR2 worker descriptors must be distinct open descriptors")
    identities: list[tuple[int, int]] = []
    for label, descriptor, writable in (
        ("input", input_fd, False),
        ("pixel output", pixels_fd, True),
        ("result output", result_fd, True),
    ):
        try:
            metadata = os.fstat(descriptor)
            descriptor_flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
            status_flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        except OSError as error:
            raise ValueError(f"CR2 worker {label} descriptor is invalid") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"CR2 worker {label} must be a regular file")
        if not descriptor_flags & fcntl.FD_CLOEXEC:
            raise ValueError(f"CR2 worker {label} must be close-on-exec")
        mode = status_flags & os.O_ACCMODE
        if writable:
            if mode != os.O_RDWR or metadata.st_size != 0:
                raise ValueError(f"CR2 worker {label} must be a fresh writable file")
        elif mode != os.O_RDONLY:
            raise ValueError("CR2 worker input must be read-only")
        identities.append((metadata.st_dev, metadata.st_ino))
    if len(set(identities)) != 3:
        raise ValueError("CR2 worker descriptors must refer to distinct files")
    input_size = os.fstat(input_fd).st_size
    if input_size <= 0 or input_size > limits.max_input_bytes:
        raise ValueError("CR2 worker input size is invalid")


def _read_private_output(
    descriptor: int,
    limit: int,
    *,
    deadline: float | None = None,
    cancelled: CancellationCheck | None = None,
    clock: Clock = time.monotonic,
) -> bytes:
    try:
        _check_cancelled(cancelled)
        if deadline is not None:
            _check_deadline(deadline, clock)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise Cr2PreviewError("The CR2 worker returned an empty output")
        if metadata.st_size > limit:
            raise Cr2PreviewError("The CR2 worker output exceeds its limit")
        size = metadata.st_size
        # @constraint Building the immutable payload can transiently retain the
        # chunk list plus one joined copy. The single admission slot remains
        # held through GTK delivery, so this bounded duplication cannot stack
        # across concurrent CR2 previews.
        chunks: list[bytes] = []
        offset = 0
        while offset < size:
            _check_cancelled(cancelled)
            if deadline is not None:
                _check_deadline(deadline, clock)
            chunk = os.pread(descriptor, min(64 * 1024, size - offset), offset)
            if not chunk:
                raise Cr2PreviewError("The CR2 worker output is truncated")
            chunks.append(chunk)
            offset += len(chunk)
        if deadline is not None:
            _check_deadline(deadline, clock)
        if os.fstat(descriptor).st_size != size:
            raise Cr2PreviewError("The CR2 worker output changed after completion")
        return b"".join(chunks)
    except OSError as error:
        raise Cr2PreviewError("The CR2 worker output could not be read") from error


def _snapshot(metadata: os.stat_result) -> _InputSnapshot:
    return _InputSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _check_unchanged(descriptor: int, before: _InputSnapshot) -> None:
    try:
        after = _snapshot(os.fstat(descriptor))
    except OSError as error:
        raise Cr2PreviewError("The CR2 file could not be verified") from error
    if after != before:
        raise Cr2PreviewError("The CR2 file changed during preview preparation")


def _check_deadline(deadline: float, clock: Clock) -> None:
    if clock() >= deadline:
        raise Cr2PreviewError("CR2 preview preparation timed out")


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise Cr2PreviewCancelled("CR2 preview cancelled")


class Cr2PreviewView(Gtk.Box):
    """A stable fit-to-window canvas for one validated raw worker frame."""

    def __init__(
        self,
        texture: Gdk.Texture,
        source_width: int,
        source_height: int,
    ) -> None:
        width = texture.get_width()
        height = texture.get_height()
        if (
            width <= 0
            or height <= 0
            or width > DEFAULT_LIMITS.max_render_edge
            or height > DEFAULT_LIMITS.max_render_edge
            or width * height > DEFAULT_LIMITS.max_render_pixels
        ):
            raise Cr2PreviewError("The CR2 texture dimensions are invalid")
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
            hexpand=True,
            vexpand=True,
        )
        self.texture = texture
        self.picture = Gtk.Picture(
            paintable=texture,
            content_fit=Gtk.ContentFit.CONTAIN,
            can_shrink=True,
            hexpand=True,
            vexpand=True,
        )
        self.picture.set_focusable(False)
        self.picture.set_can_focus(False)
        self.append(self.picture)
        status = Gtk.Label(
            label=f"Embedded JPEG · {source_width} × {source_height} · Fit",
        )
        status.set_margin_bottom(10)
        status.add_css_class("caption")
        status.add_css_class("dim-label")
        self.append(status)


class Cr2Renderer:
    """Prepare CR2 previews in one externally bounded decoder worker."""

    id = "cr2"

    def __init__(self) -> None:
        self._pending_id = 0

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        return (
            file.is_native()
            and info.get_file_type() == Gio.FileType.REGULAR
            and supports_cr2(file.get_basename(), info.get_content_type())
            and cr2_runtime_available()
        )

    def render(
        self,
        file: Gio.File,
        _info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        if self._pending_id:
            GLib.source_remove(self._pending_id)
            self._pending_id = 0
        path = file.get_path() if file.is_native() else None
        if path is None:
            self._queue_error(
                cancellable,
                on_error,
                "CR2 preview supports local files only",
            )
            return
        if cancellable.is_cancelled():
            return
        if not _WORKER_SLOT.acquire(blocking=False):
            # @decision One pending selection replaces the previous one. Keep
            # admission bounded through GTK delivery, but don't reject the new
            # photograph while a cancelled worker is still releasing its slot.
            # The session's preparation deadline also bounds time spent here.
            self._pending_id = GLib.timeout_add(
                25,
                self._retry_pending,
                file,
                _info,
                cancellable,
                on_ready,
                on_error,
            )
            return

        def worker() -> None:
            delivery_owns_slot = False
            error_message: str | None = None
            try:
                output = run_cr2_worker(path, cancelled=cancellable.is_cancelled)
                try:
                    source_id = GLib.idle_add(
                        self._deliver_preview,
                        cancellable,
                        on_ready,
                        on_error,
                        output,
                    )
                except Exception:
                    source_id = 0
                if isinstance(source_id, int) and source_id > 0:
                    delivery_owns_slot = True
                else:
                    error_message = "The CR2 preview could not be queued for display"
            except Cr2PreviewCancelled:
                pass
            except Cr2PreviewError as error:
                error_message = str(error)
            except Exception:
                error_message = "The CR2 preview could not be created safely"
            finally:
                if not delivery_owns_slot:
                    _WORKER_SLOT.release()
            if error_message is not None:
                self._queue_error(cancellable, on_error, error_message)

        try:
            thread = threading.Thread(
                target=worker,
                name="kukni-cr2-renderer",
                daemon=True,
            )
            thread.start()
        except (RuntimeError, OSError):
            _WORKER_SLOT.release()
            self._queue_error(
                cancellable,
                on_error,
                "The CR2 preview worker thread could not be started",
            )

    def _retry_pending(self, file, info, cancellable, on_ready, on_error) -> bool:
        self._pending_id = 0
        if not cancellable.is_cancelled():
            self.render(file, info, cancellable, on_ready, on_error)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _queue_error(
        cancellable: Gio.Cancellable,
        on_error: ErrorCallback,
        message: str,
    ) -> None:
        try:
            GLib.idle_add(Cr2Renderer._deliver_error, cancellable, on_error, message)
        except Exception:
            pass

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
        output: Cr2WorkerOutput,
    ) -> bool:
        try:
            if cancellable.is_cancelled():
                return GLib.SOURCE_REMOVE
            result = output.result
            if len(output.pixels) != result.pixel_bytes:
                raise Cr2PreviewError("The CR2 pixel payload has an invalid size")
            pixel_bytes = GLib.Bytes.new(output.pixels)
            texture = Gdk.MemoryTexture.new(
                result.width,
                result.height,
                Gdk.MemoryFormat.R8G8B8A8,
                pixel_bytes,
                result.stride,
            )
            view = Cr2PreviewView(
                texture,
                result.source_width,
                result.source_height,
            )
            on_ready(view, "Canon CR2 image · embedded JPEG · fit")
        except (GLib.Error, Cr2PreviewError, TypeError, ValueError):
            on_error("The decoded CR2 preview could not be displayed")
        finally:
            _WORKER_SLOT.release()
        return GLib.SOURCE_REMOVE
