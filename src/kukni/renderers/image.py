# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Ordinary raster images, decoded outside GTK using the shared pixel worker."""

from dataclasses import replace
from pathlib import Path

from gi.repository import Gio

from .cr2 import Cr2Renderer, DEFAULT_LIMITS, run_cr2_worker
from .image_view import ImagePreviewView


IMAGE_TYPES = frozenset((
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/tiff",
    "image/bmp", "image/x-bmp", "image/x-ms-bmp", "image/vnd.microsoft.icon",
    "image/x-icon",
))
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp", ".ico")
IMAGE_LIMITS = replace(DEFAULT_LIMITS, max_input_bytes=64 * 1024 * 1024)


def supports_image(filename, content_type):
    kind = (content_type or "").split(";", 1)[0].strip().casefold()
    if kind in IMAGE_TYPES:
        return True
    return bool(
        kind in ("", "application/octet-stream", "application/x-empty", "inode/x-empty")
        and filename and filename.casefold().endswith(IMAGE_SUFFIXES)
    )


def run_image_worker(path, *, cancelled=None, limits=IMAGE_LIMITS):
    # @decision Reuse the same supervisor, output validation and global
    # admission slot as CR2. Only the trusted decoder entry point differs.
    helper = Path(__file__).resolve().parents[3] / "helpers/kukni-image-worker.py"
    return run_cr2_worker(path, cancelled=cancelled, limits=limits, worker_path=helper)


class ImageRenderer(Cr2Renderer):
    id = "image"
    preview_subtitle = "Image"

    def supports(self, file, info):
        return (
            file.is_native()
            and info.get_file_type() == Gio.FileType.REGULAR
            and supports_image(file.get_basename(), info.get_content_type())
        )

    def _prepare(self, path, *, cancelled):
        return run_image_worker(path, cancelled=cancelled)

    def _create_view(self, texture, result):
        return ImagePreviewView(texture, result.source_width, result.source_height)

    @staticmethod
    def _queue_error(cancellable, on_error, message):
        Cr2Renderer._queue_error(cancellable, on_error, message.replace("CR2", "image"))
