# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Strict parent-side contract for disposable media preview workers.

This module deliberately has no GTK or GStreamer imports.  Worker output is
untrusted even though Kukni supplies the helper: a compromised native decoder
controls that process and everything it writes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any

from .worker import probe_bwrap_user_namespace, terminate_process_group


PROTOCOL_VERSION = 1
FRAME_FORMAT_RGBA8 = "rgba8"
FRAME_FORMAT_NONE = "none"
WORKER_POLL_SECONDS = 0.05
BWRAP_PROBE_TIMEOUT_SECONDS = 3.0
_TRUSTED_EXECUTABLE_DIRECTORIES = (Path("/usr/bin"), Path("/bin"))


CancellationCheck = Callable[[], bool]
ProcessFactory = Callable[..., Any]
RuntimeProbe = Callable[[str, str], bool]
Clock = Callable[[], float]


# @constraint Each decoder can consume the full per-process resource allowance.
# Keep admission bounded even though this supervisor is not automatic-route
# eligible until it also has aggregate process-tree limits or a no-fork policy.
_WORKER_SLOTS = threading.BoundedSemaphore(2)


class MediaWorkerError(RuntimeError):
    """A worker request or result violated the media preview contract."""


class MediaWorkerCancelled(Exception):
    """Media preparation stopped because its preview request was superseded."""


@dataclass(frozen=True, slots=True)
class MediaWorkerLimits:
    """Resource and protocol limits enforced by both sides of the boundary."""

    max_input_bytes: int = 16 * 1024 * 1024 * 1024
    max_edge_pixels: int = 1_800
    max_frame_bytes: int = 1_800 * 1_800 * 4
    max_result_bytes: int = 64 * 1024
    max_temp_bytes: int = 64 * 1024 * 1024
    max_duration_usec: int = 359_999_999 * 1_000_000
    max_address_space_bytes: int = 1024 * 1024 * 1024
    max_cpu_seconds: int = 10
    wall_timeout_seconds: float = 12.0
    max_open_files: int = 64

    def __post_init__(self) -> None:
        integer_fields = (
            self.max_input_bytes,
            self.max_edge_pixels,
            self.max_frame_bytes,
            self.max_result_bytes,
            self.max_temp_bytes,
            self.max_duration_usec,
            self.max_address_space_bytes,
            self.max_cpu_seconds,
            self.max_open_files,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integer_fields
        ):
            raise ValueError("media worker integer limits must be positive")
        if (
            isinstance(self.wall_timeout_seconds, bool)
            or not isinstance(self.wall_timeout_seconds, (int, float))
            or not math.isfinite(float(self.wall_timeout_seconds))
            or self.wall_timeout_seconds <= 0
        ):
            raise ValueError("media worker wall timeout must be positive and finite")
        if self.max_frame_bytes < self.max_edge_pixels * 4:
            raise ValueError("media worker frame limit cannot hold one maximum-width row")


DEFAULT_LIMITS = MediaWorkerLimits()


@dataclass(frozen=True, slots=True)
class MediaWorkerResult:
    """Validated metadata accompanying an optional raw RGBA preview frame."""

    kind: str
    has_video: bool
    has_audio: bool
    duration_usec: int
    width: int
    height: int
    frame_format: str
    frame_bytes: int

    @property
    def has_frame(self) -> bool:
        return self.frame_format == FRAME_FORMAT_RGBA8


@dataclass(frozen=True, slots=True)
class MediaWorkerOutput:
    """One fully validated worker result and its immutable raw frame bytes."""

    result: MediaWorkerResult
    frame: bytes


@dataclass(frozen=True, slots=True)
class MediaWorkerLaunch:
    """Immutable subprocess configuration with an explicit FD allowlist."""

    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]
    environment: tuple[tuple[str, str], ...] = (
        ("LANG", "C.UTF-8"),
        ("LC_ALL", "C.UTF-8"),
        ("PATH", "/usr/bin:/bin"),
    )


_RESULT_FIELDS = frozenset(
    {
        "version",
        "kind",
        "has_video",
        "has_audio",
        "duration_usec",
        "width",
        "height",
        "frame_format",
        "frame_bytes",
    }
)


def parse_worker_result(
    payload: bytes,
    *,
    limits: MediaWorkerLimits = DEFAULT_LIMITS,
) -> MediaWorkerResult:
    """Parse one exact protocol-v1 result without trusting worker-controlled types."""

    if not isinstance(payload, bytes):
        raise TypeError("media worker result must be bytes")
    if not payload:
        raise MediaWorkerError("media worker returned no result")
    if len(payload) > limits.max_result_bytes:
        raise MediaWorkerError("media worker result exceeds the size limit")

    def reject_constant(value: str) -> None:
        raise MediaWorkerError(f"media worker result contains invalid value {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MediaWorkerError("media worker result repeats a field")
            result[key] = value
        return result

    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise MediaWorkerError("media worker result is not valid JSON") from error

    if not isinstance(value, dict):
        raise MediaWorkerError("media worker result must be a JSON object")
    fields = frozenset(value)
    if fields != _RESULT_FIELDS:
        # Worker-controlled field names must not become unbounded UI text.
        raise MediaWorkerError("media worker result fields are invalid")

    if type(value["version"]) is not int or value["version"] != PROTOCOL_VERSION:
        raise MediaWorkerError("media worker protocol version is unsupported")
    for field in ("has_video", "has_audio"):
        if not isinstance(value[field], bool):
            raise MediaWorkerError(f"media worker field {field} must be boolean")
    for field in ("duration_usec", "width", "height", "frame_bytes"):
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise MediaWorkerError(f"media worker field {field} must be a non-negative integer")

    kind = value["kind"]
    if kind not in ("audio", "video"):
        raise MediaWorkerError("media worker kind is unsupported")
    frame_format = value["frame_format"]
    if frame_format not in (FRAME_FORMAT_NONE, FRAME_FORMAT_RGBA8):
        raise MediaWorkerError("media worker frame format is unsupported")
    if not value["has_audio"] and not value["has_video"]:
        raise MediaWorkerError("media worker found no previewable stream")
    if kind == "video" and not value["has_video"]:
        raise MediaWorkerError("video result has no video stream")
    if kind == "audio" and (not value["has_audio"] or value["has_video"]):
        raise MediaWorkerError("audio result has inconsistent stream flags")
    if value["duration_usec"] > limits.max_duration_usec:
        raise MediaWorkerError("media duration exceeds the protocol limit")

    width = value["width"]
    height = value["height"]
    frame_bytes = value["frame_bytes"]
    if frame_format == FRAME_FORMAT_NONE:
        if width or height or frame_bytes:
            raise MediaWorkerError("frame-less media result contains frame dimensions")
    else:
        if kind != "video" or not width or not height:
            raise MediaWorkerError("RGBA frame requires a video result and positive dimensions")
        if width > limits.max_edge_pixels or height > limits.max_edge_pixels:
            raise MediaWorkerError("media frame dimensions exceed the edge limit")
        expected_bytes = width * height * 4
        if expected_bytes > limits.max_frame_bytes or frame_bytes != expected_bytes:
            raise MediaWorkerError("media frame byte count is invalid")

    return MediaWorkerResult(
        kind=kind,
        has_video=value["has_video"],
        has_audio=value["has_audio"],
        duration_usec=value["duration_usec"],
        width=width,
        height=height,
        frame_format=frame_format,
        frame_bytes=frame_bytes,
    )


def validate_frame_bytes(
    frame: bytes,
    result: MediaWorkerResult,
    *,
    limits: MediaWorkerLimits = DEFAULT_LIMITS,
) -> None:
    """Require the worker's raw frame to match validated metadata exactly."""

    if not isinstance(frame, bytes):
        raise TypeError("media frame must be bytes")
    if len(frame) > limits.max_frame_bytes:
        raise MediaWorkerError("media frame exceeds the size limit")
    if len(frame) != result.frame_bytes:
        raise MediaWorkerError("media frame does not match its declared size")
    if result.has_frame and len(frame) != result.width * result.height * 4:
        raise MediaWorkerError("media frame does not match its dimensions")
    if not result.has_frame and frame:
        raise MediaWorkerError("audio-only media returned unexpected frame bytes")


def validate_worker_descriptors(
    *,
    input_fd: int,
    frame_fd: int,
    result_fd: int,
    limits: MediaWorkerLimits = DEFAULT_LIMITS,
) -> None:
    """Validate the launcher's three already-open descriptor capabilities."""

    descriptors = (input_fd, frame_fd, result_fd)
    if any(
        isinstance(fd, bool) or not isinstance(fd, int) or fd < 3
        for fd in descriptors
    ):
        raise ValueError(
            "media worker descriptors must be open non-standard file descriptors"
        )
    if len(set(descriptors)) != len(descriptors):
        raise ValueError("media worker descriptors must be distinct")

    identities: list[tuple[int, int]] = []
    for label, descriptor, writable in (
        ("input", input_fd, False),
        ("frame output", frame_fd, True),
        ("result output", result_fd, True),
    ):
        try:
            metadata = os.fstat(descriptor)
            descriptor_flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
            status_flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        except OSError as error:
            raise ValueError(f"media worker {label} descriptor is not open") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"media worker {label} must be a regular file")
        if not descriptor_flags & fcntl.FD_CLOEXEC:
            raise ValueError(f"media worker {label} descriptor must be close-on-exec")
        access_mode = status_flags & os.O_ACCMODE
        if writable:
            if access_mode not in (os.O_WRONLY, os.O_RDWR):
                raise ValueError(f"media worker {label} descriptor must be writable")
            if metadata.st_size != 0 or os.lseek(descriptor, 0, os.SEEK_CUR) != 0:
                raise ValueError(f"media worker {label} descriptor must be empty and fresh")
        elif access_mode != os.O_RDONLY:
            raise ValueError("media worker input descriptor must be read-only")
        identities.append((metadata.st_dev, metadata.st_ino))

    if len(set(identities)) != len(identities):
        raise ValueError("media worker descriptors must refer to distinct files")
    input_size = os.fstat(input_fd).st_size
    if input_size <= 0:
        raise ValueError("media worker input must not be empty")
    if input_size > limits.max_input_bytes:
        raise ValueError("media worker input exceeds the size limit")


def build_media_worker_launch(
    *,
    bwrap_path: str,
    prlimit_path: str,
    python_path: str,
    worker_path: str | os.PathLike[str],
    input_fd: int,
    frame_fd: int,
    result_fd: int,
    limits: MediaWorkerLimits = DEFAULT_LIMITS,
) -> MediaWorkerLaunch:
    """Build the fixed, network-denied process boundary for one media file."""

    # @constraint --unshare-all supplies the PID namespace and --die-with-parent
    # makes bubblewrap tear down sandbox children when its launcher dies.  That is
    # the intended process-tree quiescence property before outputs are accepted.
    # Automatic routing remains blocked until a real integration test proves
    # that property and either a delegated cgroup enforces aggregate memory/tasks
    # or the worker has a no-fork policy; RLIMIT values apply only per process.

    validate_worker_descriptors(
        input_fd=input_fd,
        frame_fd=frame_fd,
        result_fd=result_fd,
        limits=limits,
    )
    descriptors = (input_fd, frame_fd, result_fd)
    executables = (bwrap_path, prlimit_path, python_path)
    if any(not isinstance(path, str) or not path.startswith("/") for path in executables):
        raise ValueError("media worker executable paths must be absolute")
    worker = os.fspath(worker_path)
    if not worker.startswith("/"):
        raise ValueError("media worker helper path must be absolute")

    sandbox_command = [
        bwrap_path,
        "--unshare-all",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(limits.max_temp_bytes),
        "--tmpfs",
        "/tmp",
        "--dir",
        "/input",
        "--dir",
        "/output",
        "--dir",
        "/worker",
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
        "/etc/ld.so.cache",
        "/etc/localtime",
    ):
        if Path(source).exists():
            sandbox_command.extend(("--ro-bind", source, source))

    # @decision The decoder sees one read-only input inode and two bounded output
    # inodes.  It receives no home, session bus, display, audio socket, or host
    # network namespace; raw RGBA crosses back so the UI runs no image decoder on
    # worker-controlled output.
    sandbox_command.extend(
        (
            "--ro-bind",
            worker,
            "/worker/kukni-media-worker.py",
            "--ro-bind-fd",
            str(input_fd),
            "/input/media",
            "--bind-fd",
            str(frame_fd),
            "/output/frame.rgba",
            "--bind-fd",
            str(result_fd),
            "/output/result.json",
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
            "--setenv",
            "GST_REGISTRY",
            "/tmp/gstreamer-registry.bin",
            "--setenv",
            "GST_REGISTRY_FORK",
            "no",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--remount-ro",
            "/",
            "--",
            python_path,
            "/worker/kukni-media-worker.py",
            "--input",
            "/input/media",
            "--frame-output",
            "/output/frame.rgba",
            "--result-output",
            "/output/result.json",
            "--max-edge",
            str(limits.max_edge_pixels),
            "--max-frame-bytes",
            str(limits.max_frame_bytes),
            "--max-result-bytes",
            str(limits.max_result_bytes),
            "--max-input-bytes",
            str(limits.max_input_bytes),
        )
    )
    command = [
        prlimit_path,
        f"--as={limits.max_address_space_bytes}",
        f"--cpu={limits.max_cpu_seconds}",
        f"--fsize={max(limits.max_frame_bytes, limits.max_result_bytes)}",
        f"--nofile={limits.max_open_files}",
        "--core=0",
        "--",
        *sandbox_command,
    ]
    return MediaWorkerLaunch(argv=tuple(command), pass_fds=descriptors)


def run_media_worker(
    path: str | os.PathLike[str],
    *,
    limits: MediaWorkerLimits = DEFAULT_LIMITS,
    cancelled: CancellationCheck | None = None,
    bwrap_path: str | os.PathLike[str] | None = None,
    prlimit_path: str | os.PathLike[str] | None = None,
    python_path: str | os.PathLike[str] | None = None,
    worker_path: str | os.PathLike[str] | None = None,
    true_path: str | os.PathLike[str] | None = None,
    process_factory: ProcessFactory = subprocess.Popen,
    runtime_probe: RuntimeProbe | None = None,
    clock: Clock = time.monotonic,
) -> MediaWorkerOutput:
    """Run one decoder behind the committed sandbox and validate all output.

    The selected path is opened exactly once by the parent.  The worker receives
    only that read-only descriptor and two new private output descriptors; it
    cannot ask the parent to reopen a worker-controlled path or error message.

    @constraint O_NONBLOCK prevents FIFO/device opens from waiting, but Linux
    cannot cancel an individual pathname-resolution/open syscall.  A stalled
    filesystem syscall can therefore outlive this userspace monotonic deadline.
    """

    _check_cancelled(cancelled)
    deadline = clock() + limits.wall_timeout_seconds
    slot_acquired = False
    input_fd = -1
    frame_fd = -1
    result_fd = -1
    output_directory: str | None = None
    process: Any | None = None
    try:
        _acquire_worker_slot(cancelled, deadline=deadline, clock=clock)
        slot_acquired = True
        _check_cancelled(cancelled)

        runtime = _resolve_media_worker_runtime(
            bwrap_path=bwrap_path,
            prlimit_path=prlimit_path,
            python_path=python_path,
            worker_path=worker_path,
            true_path=true_path,
        )
        probe = probe_bwrap_user_namespace if runtime_probe is None else runtime_probe
        if runtime_probe is None or probe is probe_bwrap_user_namespace:
            _require_default_probe_budget(deadline, clock)
        try:
            sandbox_ready = probe(runtime[0], runtime[4])
        except Exception as error:
            raise MediaWorkerError("the media worker sandbox is unavailable") from error
        if sandbox_ready is not True:
            raise MediaWorkerError("the media worker sandbox is unavailable")
        _check_deadline(deadline, clock)
        _check_cancelled(cancelled)

        input_fd, input_snapshot = _open_media_input(path, limits)
        _check_cancelled(cancelled)
        output_directory, frame_fd, result_fd = _create_private_outputs()

        try:
            launch = build_media_worker_launch(
                bwrap_path=runtime[0],
                prlimit_path=runtime[1],
                python_path=runtime[2],
                worker_path=runtime[3],
                input_fd=input_fd,
                frame_fd=frame_fd,
                result_fd=result_fd,
                limits=limits,
            )
        except (OSError, ValueError) as error:
            raise MediaWorkerError("the media worker could not be prepared") from error

        _check_cancelled(cancelled)
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
                env=dict(launch.environment),
            )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise MediaWorkerError("the media worker could not start") from error

        returncode = _wait_for_media_worker(
            process,
            cancelled=cancelled,
            deadline=deadline,
            clock=clock,
        )
        if returncode != 0:
            raise MediaWorkerError("the media worker failed")
        _check_cancelled(cancelled)

        result_payload = _read_bounded_output(
            result_fd,
            limits.max_result_bytes,
        )
        frame = _read_bounded_output(frame_fd, limits.max_frame_bytes)
        _check_cancelled(cancelled)
        try:
            result = parse_worker_result(result_payload, limits=limits)
            validate_frame_bytes(frame, result, limits=limits)
        except MediaWorkerError as error:
            # @constraint Protocol diagnostics are fixed parent strings.  Never
            # surface stderr, JSON fields, or any other decoder-controlled text.
            raise MediaWorkerError("the media worker returned invalid output") from error

        _ensure_input_unchanged(input_fd, input_snapshot)
        _check_cancelled(cancelled)
        _check_deadline(deadline, clock)
        return MediaWorkerOutput(result=result, frame=frame)
    except _MediaWorkerTimedOut as error:
        raise MediaWorkerError("the media worker timed out") from error
    finally:
        if process is not None:
            # Lifecycle cleanup is best-effort and must never replace the fixed
            # public error/cancellation already selected by the supervisor.
            try:
                terminate_process_group(process)
            except BaseException:
                pass
        for descriptor in (result_fd, frame_fd, input_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if output_directory is not None:
            shutil.rmtree(output_directory, ignore_errors=True)
        if slot_acquired:
            _WORKER_SLOTS.release()


class _MediaWorkerTimedOut(Exception):
    """Internal marker used to keep timeout text parent-controlled."""


@dataclass(frozen=True, slots=True)
class _InputSnapshot:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


def _resolve_media_worker_runtime(
    *,
    bwrap_path: str | os.PathLike[str] | None,
    prlimit_path: str | os.PathLike[str] | None,
    python_path: str | os.PathLike[str] | None,
    worker_path: str | os.PathLike[str] | None,
    true_path: str | os.PathLike[str] | None,
) -> tuple[str, str, str, str, str]:
    bwrap = _require_executable(bwrap_path, "bwrap")
    prlimit = _require_executable(prlimit_path, "prlimit")
    python = _require_executable(python_path, "python3")
    true = _require_executable(true_path, "true")

    try:
        candidate = (
            Path(os.fspath(worker_path))
            if worker_path is not None
            else Path(__file__).resolve().parents[2]
            / "helpers"
            / "kukni-media-worker.py"
        )
    except TypeError as error:
        raise MediaWorkerError("the media worker helper is unavailable") from error
    if not candidate.is_absolute():
        raise MediaWorkerError("the media worker helper is unavailable")
    # @constraint The app-owned helper is in the same-code trust domain, but
    # bubblewrap currently opens this path after validation.  Pinning its inode
    # through another passed FD requires coordinated launcher/packaging changes
    # and remains a follow-up; do not weaken these same-code runtime checks.
    try:
        candidate = candidate.resolve(strict=True)
        metadata = candidate.stat()
    except (OSError, RuntimeError) as error:
        raise MediaWorkerError("the media worker helper is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(candidate, os.R_OK):
        raise MediaWorkerError("the media worker helper is unavailable")
    return bwrap, prlimit, python, os.fspath(candidate), true


def _require_executable(
    configured_path: str | os.PathLike[str] | None,
    command_name: str,
) -> str:
    if configured_path is None:
        candidates = tuple(
            directory / command_name
            for directory in _TRUSTED_EXECUTABLE_DIRECTORIES
        )
    else:
        try:
            candidate = os.fspath(configured_path)
        except TypeError as error:
            raise MediaWorkerError("a media worker runtime tool is unavailable") from error
        if (
            not isinstance(candidate, str)
            or not candidate
            or not os.path.isabs(candidate)
        ):
            raise MediaWorkerError("a media worker runtime tool is unavailable")
        candidates = (Path(candidate),)

    for candidate_path in candidates:
        try:
            resolved = candidate_path.resolve(strict=True)
            metadata = resolved.stat()
        except (OSError, RuntimeError):
            continue
        if stat.S_ISREG(metadata.st_mode) and os.access(resolved, os.X_OK):
            return os.fspath(resolved)
    raise MediaWorkerError("a media worker runtime tool is unavailable")


def _acquire_worker_slot(
    cancelled: CancellationCheck | None,
    *,
    deadline: float,
    clock: Clock,
) -> None:
    while True:
        _check_cancelled(cancelled)
        remaining = deadline - clock()
        if remaining <= 0:
            raise _MediaWorkerTimedOut
        if _WORKER_SLOTS.acquire(timeout=min(WORKER_POLL_SECONDS, remaining)):
            return


def _check_deadline(deadline: float, clock: Clock) -> None:
    if clock() >= deadline:
        raise _MediaWorkerTimedOut


def _require_default_probe_budget(deadline: float, clock: Clock) -> None:
    # Keep this synchronized with worker.probe_bwrap_user_namespace's bounded
    # subprocess timeout.  Injected probes are test/config seams and declare
    # their own timing behavior; only the synchronous default needs this guard.
    if deadline - clock() < BWRAP_PROBE_TIMEOUT_SECONDS:
        raise _MediaWorkerTimedOut


def _open_media_input(
    path: str | os.PathLike[str],
    limits: MediaWorkerLimits,
) -> tuple[int, _InputSnapshot]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(os.fspath(path), flags)
    except (OSError, TypeError) as error:
        raise MediaWorkerError("the media input could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MediaWorkerError("media preview requires a regular local file")
        if metadata.st_size <= 0:
            raise MediaWorkerError("the media input is empty")
        if metadata.st_size > limits.max_input_bytes:
            raise MediaWorkerError("the media input exceeds the size limit")
        return descriptor, _snapshot_input(metadata)
    except OSError as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise MediaWorkerError("the media input could not be inspected safely") from error
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _snapshot_input(metadata: os.stat_result) -> _InputSnapshot:
    return _InputSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        links=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _create_private_outputs() -> tuple[str, int, int]:
    directory: str | None = None
    frame_fd = -1
    result_fd = -1
    try:
        directory = tempfile.mkdtemp(prefix="kukni-media-worker-")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
        )
        frame_fd = os.open(os.path.join(directory, "frame.rgba"), flags, 0o600)
        result_fd = os.open(os.path.join(directory, "result.json"), flags, 0o600)
        return directory, frame_fd, result_fd
    except OSError as error:
        for descriptor in (result_fd, frame_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise MediaWorkerError("private media worker output is unavailable") from error


def _wait_for_media_worker(
    process: Any,
    *,
    cancelled: CancellationCheck | None,
    deadline: float,
    clock: Clock,
) -> int:
    while True:
        _check_cancelled(cancelled)
        try:
            returncode = process.poll()
        except (OSError, subprocess.SubprocessError) as error:
            raise MediaWorkerError("the media worker failed") from error
        if returncode is not None:
            _check_cancelled(cancelled)
            _check_deadline(deadline, clock)
            return int(returncode)
        remaining = deadline - clock()
        if remaining <= 0:
            raise _MediaWorkerTimedOut
        try:
            returncode = process.wait(timeout=min(WORKER_POLL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            continue
        except (OSError, subprocess.SubprocessError) as error:
            raise MediaWorkerError("the media worker failed") from error
        _check_cancelled(cancelled)
        _check_deadline(deadline, clock)
        return int(returncode)


def _read_bounded_output(descriptor: int, limit: int) -> bytes:
    try:
        payload = os.pread(descriptor, limit + 1, 0)
        size_after_read = os.fstat(descriptor).st_size
    except OSError as error:
        raise MediaWorkerError("the media worker returned unreadable output") from error
    if len(payload) > limit or size_after_read > limit:
        raise MediaWorkerError("the media worker output exceeds the size limit")
    if len(payload) != size_after_read:
        raise MediaWorkerError("the media worker returned invalid output")
    return payload


def _ensure_input_unchanged(
    descriptor: int,
    expected: _InputSnapshot,
) -> None:
    try:
        current = _snapshot_input(os.fstat(descriptor))
    except OSError as error:
        raise MediaWorkerError("the media input changed during decoding") from error
    if current != expected:
        raise MediaWorkerError("the media input changed during decoding")


def _check_cancelled(cancelled: CancellationCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise MediaWorkerCancelled("media preview cancelled")
