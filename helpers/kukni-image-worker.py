#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Decode an allowlisted raster file using the established CR2 pixel contract.

This intentionally reuses the resource/descriptor/decoder validation rather
than maintaining a second subtly different worker boundary. Like the source
CR2 worker it is process-bounded, not a filesystem/network sandbox.
"""

import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location(
    "kukni_pixel_worker", Path(__file__).resolve().with_name("kukni-cr2-worker.py")
)
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def raster_type(header: bytes) -> str:
    # @constraint Do not auto-detect arbitrary installed loaders: SVG and other
    # active or externally referenced formats require their own sandbox policy.
    for magic, kind in (
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff", "jpeg"),
        (b"GIF87a", "gif"),
        (b"GIF89a", "gif"),
        (b"II*\0", "tiff"),
        (b"MM\0*", "tiff"),
        (b"BM", "bmp"),
        (b"\x00\x00\x01\x00", "ico"),
    ):
        if header.startswith(magic):
            return kind
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    raise common.WorkerError("Unsupported raster image")


def read_chunks(arguments):
    descriptor = arguments["input_fd"]
    total = 0
    while True:
        chunk = os.read(descriptor, min(common.READ_CHUNK_BYTES, arguments["max_input_bytes"] + 1 - total))
        if not chunk:
            return
        total += len(chunk)
        if total > arguments["max_input_bytes"]:
            raise common.WorkerError("Image exceeds the input limit")
        yield chunk


def run(argv):
    arguments = None
    try:
        arguments = common.parse_arguments(argv)
        common.validate_descriptors(arguments)
        common.verify_process_limits(arguments)
        common.enable_no_new_privileges()
        kind = raster_type(os.pread(arguments["input_fd"], 16, 0))
        pixbuf, width, height = common.decode_image(arguments, kind, read_chunks(arguments))
        common.write_outputs(arguments, pixbuf, width, height)
        return 0
    except (common.WorkerError, MemoryError, OSError, ValueError):
        if arguments is not None:
            common.clear_outputs(arguments)
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
