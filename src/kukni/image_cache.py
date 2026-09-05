# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Small process-local cache of already validated, immutable decoded pixels."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import math
import os
import stat
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .renderers.cr2 import Cr2WorkerOutput


@dataclass(frozen=True, slots=True)
class ImageCacheKey:
    renderer: str
    path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


class ImageCache:
    """Bounded LRU; only worker threads may call filesystem-touching methods.

    @decision Never cache encoded bytes, failures, directory listings, or disk
    artifacts. CR2 and ordinary images share one byte/entry budget but distinct
    renderer keys. TTL expiry is lazy on cache access, so idle retention stays
    bounded without adding a timer or another thread to the application.
    """

    def __init__(
        self,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        max_entries: int = 4,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(type(value) is not int or value <= 0 for value in (max_bytes, max_entries)):
            raise ValueError("image cache limits must be positive integers")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("image cache TTL must be positive and finite")
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[ImageCacheKey, tuple[float, Cr2WorkerOutput]] = OrderedDict()
        self._bytes = 0

    @staticmethod
    def key_for(renderer: str, path: str) -> ImageCacheKey | None:
        # Metadata failure disables caching, not previewing. A source on a
        # slow local mount can block stat, so this must never run on GTK.
        try:
            metadata = os.stat(path)
        except (OSError, ValueError, TypeError):
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            return None
        return ImageCacheKey(renderer, path, metadata.st_dev, metadata.st_ino,
                             metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)

    def get(self, key: ImageCacheKey | None) -> Cr2WorkerOutput | None:
        if key is None:
            return None
        current = self.key_for(key.renderer, key.path)
        with self._lock:
            self._prune(self._clock())
            if current != key:
                self._remove(key)
                return None
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def put(self, key: ImageCacheKey | None, output: Cr2WorkerOutput) -> bool:
        # This is called only after the supervisor validates its descriptor
        # output and cancellation. Recheck pathname identity too: replacing a
        # file during decode must not associate old pixels with the new file.
        if key is None or not self._valid_output(output):
            return False
        if len(output.pixels) > self._max_bytes:
            return False
        if self.key_for(key.renderer, key.path) != key:
            return False
        with self._lock:
            now = self._clock()
            self._prune(now)
            # Discard outdated versions of this file rather than retaining
            # their pixels until unrelated navigation eventually evicts them.
            for previous in tuple(self._entries):
                if previous.renderer == key.renderer and previous.path == key.path:
                    self._remove(previous)
            self._entries[key] = (now, output)
            self._bytes += len(output.pixels)
            while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
                self._remove(next(iter(self._entries)))
        return True

    @staticmethod
    def _valid_output(output: Cr2WorkerOutput) -> bool:
        # Import lazily to avoid the renderer/cache module cycle. The cache
        # accepts only the immutable raw protocol, never arbitrary widgets.
        from .renderers.cr2 import Cr2WorkerOutput, Cr2WorkerResult, DEFAULT_LIMITS

        if type(output) is not Cr2WorkerOutput or type(output.pixels) is not bytes:
            return False
        result = output.result
        if type(result) is not Cr2WorkerResult:
            return False
        dimensions = (result.width, result.height, result.source_width,
                      result.source_height, result.stride, result.pixel_bytes)
        return bool(
            all(type(value) is int and value > 0 for value in dimensions)
            and max(result.width, result.height) <= DEFAULT_LIMITS.max_render_edge
            and result.width * result.height <= DEFAULT_LIMITS.max_render_pixels
            and max(result.source_width, result.source_height) <= DEFAULT_LIMITS.max_source_edge
            and result.source_width * result.source_height <= DEFAULT_LIMITS.max_source_pixels
            and result.stride == result.width * 4
            and result.pixel_bytes == result.stride * result.height
            and result.pixel_bytes == len(output.pixels)
            and result.pixel_bytes <= DEFAULT_LIMITS.max_pixel_bytes
        )

    def _remove(self, key: ImageCacheKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= len(entry[1].pixels)

    def _prune(self, now: float) -> None:
        for key, (created, _output) in tuple(self._entries.items()):
            if now - created >= self._ttl_seconds:
                self._remove(key)
