#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Extract and decode one CR2 preview into tightly packed RGBA bytes.

This helper is launched only through the parent-side ``prlimit`` contract.  It
imports the existing pure-Python extractor beside it so CR2/JPEG container
parsing continues to have one implementation.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import resource
import stat
import sys
from types import ModuleType

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib


PROTOCOL_VERSION = 1
PIXEL_FORMAT = "rgba8"
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
READ_CHUNK_BYTES = 64 * 1024

MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_JPEG_BYTES = 64 * 1024 * 1024
MAX_SOURCE_EDGE = 32_768
MAX_SOURCE_PIXELS = 100_000_000
MAX_RENDER_EDGE = 4_096
MAX_RENDER_PIXELS = 4_096 * 4_096
MAX_PIXEL_BYTES = MAX_RENDER_PIXELS * 4
MAX_RESULT_BYTES = 1_024
MAX_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
MAX_CPU_SECONDS = 6
MAX_OPEN_FILES = 64

_ARGUMENTS = (
    "--input-fd",
    "--pixels-fd",
    "--result-fd",
    "--max-input-bytes",
    "--max-jpeg-bytes",
    "--max-source-edge",
    "--max-source-pixels",
    "--max-render-edge",
    "--max-render-pixels",
    "--max-pixel-bytes",
    "--max-result-bytes",
    "--max-address-space-bytes",
    "--max-cpu-seconds",
    "--max-open-files",
)


class WorkerError(RuntimeError):
    """The worker contract or decoded preview was invalid."""


def parse_arguments(argv: list[str]) -> dict[str, int]:
    """Accept one exact ordered CLI so launch ambiguity fails closed."""

    if len(argv) != 1 + len(_ARGUMENTS) * 2:
        raise WorkerError("invalid CR2 worker arguments")
    values: dict[str, int] = {}
    cursor = 1
    for expected in _ARGUMENTS:
        if argv[cursor] != expected:
            raise WorkerError("invalid CR2 worker arguments")
        raw_value = argv[cursor + 1]
        if not raw_value.isascii() or not raw_value.isdecimal():
            raise WorkerError("invalid CR2 worker arguments")
        value = int(raw_value)
        if str(value) != raw_value:
            raise WorkerError("invalid CR2 worker arguments")
        values[expected.removeprefix("--").replace("-", "_")] = value
        cursor += 2

    descriptors = (values["input_fd"], values["pixels_fd"], values["result_fd"])
    if any(descriptor < 3 for descriptor in descriptors) or len(set(descriptors)) != 3:
        raise WorkerError("invalid CR2 worker descriptors")

    bounded = (
        ("max_input_bytes", MAX_INPUT_BYTES),
        ("max_jpeg_bytes", MAX_JPEG_BYTES),
        ("max_source_edge", MAX_SOURCE_EDGE),
        ("max_source_pixels", MAX_SOURCE_PIXELS),
        ("max_render_edge", MAX_RENDER_EDGE),
        ("max_render_pixels", MAX_RENDER_PIXELS),
        ("max_pixel_bytes", MAX_PIXEL_BYTES),
        ("max_result_bytes", MAX_RESULT_BYTES),
        ("max_address_space_bytes", MAX_ADDRESS_SPACE_BYTES),
        ("max_cpu_seconds", MAX_CPU_SECONDS),
        ("max_open_files", MAX_OPEN_FILES),
    )
    if any(values[name] <= 0 or values[name] > maximum for name, maximum in bounded):
        raise WorkerError("invalid CR2 worker limits")
    if values["max_render_edge"] > values["max_source_edge"]:
        raise WorkerError("invalid CR2 worker limits")
    if values["max_render_pixels"] > values["max_source_pixels"]:
        raise WorkerError("invalid CR2 worker limits")
    if values["max_pixel_bytes"] < values["max_render_edge"] * 4:
        raise WorkerError("invalid CR2 worker limits")
    if values["max_pixel_bytes"] < values["max_render_pixels"] * 4:
        raise WorkerError("invalid CR2 worker limits")
    return values


def enable_no_new_privileges() -> None:
    """Irreversibly disable privilege gain before reading untrusted bytes."""

    if not sys.platform.startswith("linux"):
        raise WorkerError("no-new-privileges is unavailable")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = (
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )
        prctl.restype = ctypes.c_int
        if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
        if prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
            raise OSError(errno.EPERM, "prctl verification failed")
    except (AttributeError, OSError) as error:
        raise WorkerError("no-new-privileges could not be enabled") from error


def verify_process_limits(arguments: dict[str, int]) -> None:
    """Require the hard launch limits instead of trusting parent intent."""

    # RLIMIT_NPROC is not enforced for UID 0 (or equivalent privileged
    # contexts). Kukni's decoder boundary depends on task creation remaining
    # disabled, so direct source launches as root must fail closed too.
    if os.geteuid() == 0:
        raise WorkerError("CR2 worker requires an unprivileged user")

    expected_maxima = (
        (resource.RLIMIT_AS, arguments["max_address_space_bytes"]),
        (resource.RLIMIT_CPU, arguments["max_cpu_seconds"]),
        (
            resource.RLIMIT_FSIZE,
            max(arguments["max_pixel_bytes"], arguments["max_result_bytes"]),
        ),
        (resource.RLIMIT_NOFILE, arguments["max_open_files"]),
        (resource.RLIMIT_CORE, 0),
    )
    for resource_id, maximum in expected_maxima:
        soft, hard = resource.getrlimit(resource_id)
        if (
            soft == resource.RLIM_INFINITY
            or hard == resource.RLIM_INFINITY
            or soft < 0
            or hard < 0
            or soft > maximum
            or hard > maximum
        ):
            raise WorkerError("CR2 worker resource limits are missing")
    soft_processes, hard_processes = resource.getrlimit(resource.RLIMIT_NPROC)
    if soft_processes != 0 or hard_processes != 0:
        raise WorkerError("CR2 worker task creation is not disabled")


def validate_descriptors(arguments: dict[str, int]) -> None:
    descriptors = (
        ("input", arguments["input_fd"], False),
        ("pixel output", arguments["pixels_fd"], True),
        ("result output", arguments["result_fd"], True),
    )
    identities: list[tuple[int, int]] = []
    for label, descriptor, writable in descriptors:
        try:
            metadata = os.fstat(descriptor)
            status_flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        except OSError as error:
            raise WorkerError(f"invalid {label} descriptor") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkerError(f"invalid {label} descriptor")
        access_mode = status_flags & os.O_ACCMODE
        if writable:
            if (
                access_mode not in (os.O_WRONLY, os.O_RDWR)
                or metadata.st_size != 0
                or os.lseek(descriptor, 0, os.SEEK_CUR) != 0
            ):
                raise WorkerError(f"invalid {label} descriptor")
        elif access_mode != os.O_RDONLY:
            raise WorkerError("invalid input descriptor")
        identities.append((metadata.st_dev, metadata.st_ino))
    if len(set(identities)) != len(identities):
        raise WorkerError("CR2 worker descriptors must be distinct")
    input_size = os.fstat(arguments["input_fd"]).st_size
    if input_size <= 0 or input_size > arguments["max_input_bytes"]:
        raise WorkerError("invalid CR2 input size")


def load_extractor() -> ModuleType:
    path = Path(__file__).resolve().with_name("kukni-extract-preview.py")
    try:
        metadata = path.stat()
    except OSError as error:
        raise WorkerError("CR2 extractor is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise WorkerError("CR2 extractor is unavailable")
    spec = importlib.util.spec_from_file_location("kukni_cr2_extractor", path)
    if spec is None or spec.loader is None:
        raise WorkerError("CR2 extractor is unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise WorkerError("CR2 extractor could not be loaded") from error
    return module


def extract_and_decode(
    arguments: dict[str, int],
    extractor: ModuleType,
) -> tuple[GdkPixbuf.Pixbuf, int, int]:
    """Reuse the bounded parser, then decode and orient its selected JPEG."""

    try:
        data = extractor.read_cr2(
            f"/proc/self/fd/{arguments['input_fd']}",
            max_file_size=arguments["max_input_bytes"],
        )
        preview = extractor.find_best_preview(data)
    except (OSError, extractor.PreviewError, MemoryError) as error:
        raise WorkerError("CR2 extraction failed") from error
    if preview is None:
        raise WorkerError("CR2 contains no displayable JPEG")
    jpeg_size = preview.end - preview.start
    if jpeg_size <= 0 or jpeg_size > arguments["max_jpeg_bytes"]:
        raise WorkerError("embedded JPEG exceeds its size limit")

    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
    except GLib.Error as error:
        raise WorkerError("JPEG decoder is unavailable") from error
    dimensions = [0, 0]
    dimension_error = False

    def size_prepared(
        prepared_loader: GdkPixbuf.PixbufLoader,
        width: int,
        height: int,
    ) -> None:
        nonlocal dimension_error
        dimensions[:] = (width, height)
        if (
            width <= 0
            or height <= 0
            or width > arguments["max_source_edge"]
            or height > arguments["max_source_edge"]
            or width * height > arguments["max_source_pixels"]
        ):
            dimension_error = True
            prepared_loader.set_size(1, 1)
            return
        scale = min(
            1.0,
            arguments["max_render_edge"] / width,
            arguments["max_render_edge"] / height,
            math.sqrt(arguments["max_render_pixels"] / (width * height)),
        )
        if scale < 1.0:
            prepared_loader.set_size(
                max(1, math.floor(width * scale)),
                max(1, math.floor(height * scale)),
            )

    loader.connect("size-prepared", size_prepared)
    closed = False
    try:
        cursor = preview.start
        while cursor < preview.end:
            chunk_end = min(cursor + READ_CHUNK_BYTES, preview.end)
            if not loader.write(data[cursor:chunk_end]):
                raise WorkerError("embedded JPEG decode failed")
            if dimension_error:
                raise WorkerError("embedded JPEG dimensions exceed the limit")
            cursor = chunk_end
        if not loader.close():
            raise WorkerError("embedded JPEG decode failed")
        closed = True
        if dimension_error or not all(dimensions):
            raise WorkerError("embedded JPEG dimensions are invalid")
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            raise WorkerError("embedded JPEG decode failed")
        oriented = pixbuf.apply_embedded_orientation() or pixbuf
        rgba = (
            oriented
            if oriented.get_has_alpha()
            else oriented.add_alpha(False, 0, 0, 0)
        )
        if rgba is None:
            raise WorkerError("embedded JPEG conversion failed")
        _validate_pixbuf(rgba, arguments)
        return rgba, dimensions[0], dimensions[1]
    except GLib.Error as error:
        raise WorkerError("embedded JPEG decode failed") from error
    finally:
        if not closed:
            try:
                loader.close()
            except GLib.Error:
                pass


def _validate_pixbuf(pixbuf: GdkPixbuf.Pixbuf, arguments: dict[str, int]) -> None:
    width = pixbuf.get_width()
    height = pixbuf.get_height()
    if (
        pixbuf.get_colorspace() != GdkPixbuf.Colorspace.RGB
        or pixbuf.get_bits_per_sample() != 8
        or pixbuf.get_n_channels() != 4
        or not pixbuf.get_has_alpha()
        or width <= 0
        or height <= 0
        or width > arguments["max_render_edge"]
        or height > arguments["max_render_edge"]
        or width * height > arguments["max_render_pixels"]
        or width * height * 4 > arguments["max_pixel_bytes"]
        or pixbuf.get_rowstride() < width * 4
    ):
        raise WorkerError("decoded CR2 pixels violate the output contract")


def write_outputs(
    arguments: dict[str, int],
    pixbuf: GdkPixbuf.Pixbuf,
    source_width: int,
    source_height: int,
) -> None:
    width = pixbuf.get_width()
    height = pixbuf.get_height()
    rowstride = pixbuf.get_rowstride()
    packed_stride = width * 4
    pixel_count = packed_stride * height
    pixels = pixbuf.get_pixels()
    required = rowstride * (height - 1) + packed_stride
    if len(pixels) < required or pixel_count > arguments["max_pixel_bytes"]:
        raise WorkerError("decoded CR2 pixels are truncated")

    pixel_descriptor = arguments["pixels_fd"]
    result_descriptor = arguments["result_fd"]
    os.lseek(pixel_descriptor, 0, os.SEEK_SET)
    for row in range(height):
        start = row * rowstride
        _write_all(pixel_descriptor, memoryview(pixels)[start : start + packed_stride])

    result = json.dumps(
        {
            "format": PIXEL_FORMAT,
            "height": height,
            "pixel_bytes": pixel_count,
            "source_height": source_height,
            "source_width": source_width,
            "stride": packed_stride,
            "version": PROTOCOL_VERSION,
            "width": width,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(result) > arguments["max_result_bytes"]:
        raise WorkerError("CR2 result exceeds its size limit")
    os.lseek(result_descriptor, 0, os.SEEK_SET)
    _write_all(result_descriptor, result)


def _write_all(descriptor: int, data: bytes | memoryview) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            amount = os.write(descriptor, view[written:])
        except InterruptedError:
            continue
        if amount <= 0:
            raise WorkerError("CR2 output could not be written")
        written += amount


def clear_outputs(arguments: dict[str, int]) -> None:
    for name in ("pixels_fd", "result_fd"):
        try:
            os.ftruncate(arguments[name], 0)
            os.lseek(arguments[name], 0, os.SEEK_SET)
        except OSError:
            pass


def run(argv: list[str]) -> int:
    arguments: dict[str, int] | None = None
    try:
        arguments = parse_arguments(argv)
        validate_descriptors(arguments)
        verify_process_limits(arguments)
        extractor = load_extractor()
        # @decision Set and verify NO_NEW_PRIVS before the first untrusted CR2
        # byte is read. RLIMIT_NPROC=0 is already hard-set by the launcher, so
        # native decoders cannot create child processes or threads.
        enable_no_new_privileges()
        pixbuf, source_width, source_height = extract_and_decode(
            arguments,
            extractor,
        )
        write_outputs(arguments, pixbuf, source_width, source_height)
        return 0
    except (MemoryError, WorkerError, OSError, ValueError):
        if arguments is not None:
            clear_outputs(arguments)
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
