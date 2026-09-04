# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Strict parent-side contract for disposable media preview workers.

This module deliberately has no GTK or GStreamer imports.  Worker output is
untrusted even though Kukni supplies the helper: a compromised native decoder
controls that process and everything it writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import stat
from typing import Any


PROTOCOL_VERSION = 1
FRAME_FORMAT_RGBA8 = "rgba8"
FRAME_FORMAT_NONE = "none"


class MediaWorkerError(RuntimeError):
    """A worker request or result violated the media preview contract."""


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

    # @constraint RLIMIT values are inherited but accounted per process.  Before
    # this launcher is eligible for automatic routing, its supervisor must also
    # enforce a process-tree memory/task budget (for example, a delegated cgroup)
    # or a no-fork policy.  The wall-clock supervisor must also terminate the
    # complete sandbox process tree on every timeout and cancellation.

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
