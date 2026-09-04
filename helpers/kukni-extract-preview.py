#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Write a bounded, displayable JPEG embedded in a Canon CR2 to stdout."""

from __future__ import annotations

import os
import stat
import sys
from typing import NamedTuple


MAX_FILE_SIZE = 128 * 1024 * 1024
MAX_JPEG_SIZE = 64 * 1024 * 1024
MAX_CANDIDATES = 64
MAX_MARKERS = 16_384
MAX_DIMENSION = 32_768
MAX_PIXELS = 100_000_000
SCAN_MULTIPLIER = 2

SOI = b"\xff\xd8"

# DCT JPEG frame types that ordinary image viewers can display. Lossless JPEG
# frames (used for the actual RAW sensor data) are deliberately excluded.
DISPLAY_SOF_MARKERS = {
    0xC0,  # baseline DCT
    0xC1,  # extended sequential DCT
    0xC2,  # progressive DCT
    0xC5,
    0xC6,
    0xC9,
    0xCA,
    0xCD,
    0xCE,
}
ALL_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}
STANDALONE_MARKERS = {0x01, *range(0xD0, 0xD9)}


class PreviewError(Exception):
    """A safe, user-facing preview failure."""


class ScanLimitExceeded(PreviewError):
    """The input exceeded a deterministic parser-work limit."""


class ScanBudget:
    def __init__(self, byte_limit: int, marker_limit: int = MAX_MARKERS):
        self.bytes_remaining = byte_limit
        self.markers_remaining = marker_limit

    def consume_bytes(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("scan accounting cannot move backwards")
        if amount > self.bytes_remaining:
            raise ScanLimitExceeded("CR2 scan byte limit exceeded")
        self.bytes_remaining -= amount

    def consume_marker(self) -> None:
        if self.markers_remaining == 0:
            raise ScanLimitExceeded("CR2 marker limit exceeded")
        self.markers_remaining -= 1


class ParsedJpeg(NamedTuple):
    end: int
    width: int
    height: int


class Preview(NamedTuple):
    start: int
    end: int
    width: int
    height: int


def _budgeted_find(
    data: bytes,
    needle: bytes,
    start: int,
    end: int,
    budget: ScanBudget,
) -> int:
    bounded_end = min(end, start + budget.bytes_remaining)
    position = data.find(needle, start, bounded_end)
    scanned_end = bounded_end if position < 0 else position + len(needle)
    budget.consume_bytes(scanned_end - start)
    if position < 0 and bounded_end < end:
        raise ScanLimitExceeded("CR2 scan byte limit exceeded")
    return position


def parse_jpeg(
    data: bytes,
    start: int,
    budget: ScanBudget | None = None,
) -> ParsedJpeg | None:
    """Return metadata for one bounded display JPEG beginning at ``start``."""
    if start < 0 or start + len(SOI) > len(data) or data[start : start + 2] != SOI:
        return None

    if budget is None:
        budget = ScanBudget(max(len(data) * SCAN_MULTIPLIER, len(SOI)))

    natural_end = min(len(data), start + MAX_JPEG_SIZE)
    parse_end = min(natural_end, start + budget.bytes_remaining)
    limited_by_budget = parse_end < natural_end
    if parse_end <= start + len(SOI):
        raise ScanLimitExceeded("CR2 scan byte limit exceeded")

    pos = start + len(SOI)
    furthest = pos
    width = 0
    height = 0
    display_frame = False
    saw_scan = False

    try:
        while pos < parse_end:
            marker_start = pos
            if data[pos] != 0xFF:
                return None

            while pos < parse_end and data[pos] == 0xFF:
                pos += 1
            furthest = max(furthest, pos)
            if pos >= parse_end:
                if limited_by_budget:
                    raise ScanLimitExceeded("CR2 scan byte limit exceeded")
                return None

            marker = data[pos]
            pos += 1
            furthest = max(furthest, pos)
            budget.consume_marker()

            if marker == 0x00:
                return None
            if marker == 0xD9:  # EOI
                if display_frame and saw_scan and width and height:
                    return ParsedJpeg(pos, width, height)
                return None
            if marker == 0xD8 or marker in STANDALONE_MARKERS:
                continue

            if pos + 2 > parse_end:
                if limited_by_budget:
                    raise ScanLimitExceeded("CR2 scan byte limit exceeded")
                return None
            segment_length = int.from_bytes(data[pos : pos + 2], "big")
            if segment_length < 2:
                return None

            payload = pos + 2
            segment_end = pos + segment_length
            if segment_end > parse_end:
                if limited_by_budget:
                    raise ScanLimitExceeded("CR2 scan byte limit exceeded")
                return None

            if marker in ALL_SOF_MARKERS:
                if marker not in DISPLAY_SOF_MARKERS or payload + 5 > segment_end:
                    return None
                candidate_height = int.from_bytes(
                    data[payload + 1 : payload + 3], "big"
                )
                candidate_width = int.from_bytes(
                    data[payload + 3 : payload + 5], "big"
                )
                candidate_pixels = candidate_width * candidate_height
                if (
                    candidate_width == 0
                    or candidate_height == 0
                    or candidate_width > MAX_DIMENSION
                    or candidate_height > MAX_DIMENSION
                    or candidate_pixels > MAX_PIXELS
                ):
                    return None
                if display_frame and (candidate_width, candidate_height) != (
                    width,
                    height,
                ):
                    return None
                width = candidate_width
                height = candidate_height
                display_frame = True

            pos = segment_end
            furthest = max(furthest, pos)
            if marker != 0xDA:  # SOS
                continue

            saw_scan = True

            # Entropy-coded data uses FF 00 for a literal FF and FF D0..D7 for
            # restart markers. Any other unescaped marker returns to headers.
            while pos < parse_end:
                search_start = pos
                marker_start = data.find(b"\xff", search_start, parse_end)
                if marker_start < 0:
                    furthest = parse_end
                    if limited_by_budget:
                        raise ScanLimitExceeded("CR2 scan byte limit exceeded")
                    return None
                furthest = max(furthest, marker_start + 1)
                pos = marker_start + 1
                while pos < parse_end and data[pos] == 0xFF:
                    pos += 1
                furthest = max(furthest, pos)
                if pos >= parse_end:
                    if limited_by_budget:
                        raise ScanLimitExceeded("CR2 scan byte limit exceeded")
                    return None
                entropy_marker = data[pos]
                if entropy_marker == 0x00 or 0xD0 <= entropy_marker <= 0xD7:
                    budget.consume_marker()
                    pos += 1
                    furthest = max(furthest, pos)
                    continue
                pos = marker_start
                break

        if limited_by_budget:
            raise ScanLimitExceeded("CR2 scan byte limit exceeded")
        return None
    finally:
        budget.consume_bytes(furthest - start)


def find_best_preview(data: bytes) -> Preview | None:
    budget = ScanBudget(max(len(data) * SCAN_MULTIPLIER, len(SOI)))
    best: Preview | None = None
    cursor = 0

    for _candidate in range(MAX_CANDIDATES):
        start = _budgeted_find(data, SOI, cursor, len(data), budget)
        if start < 0:
            return best
        cursor = start + len(SOI)

        parsed = parse_jpeg(data, start, budget)
        if parsed is None:
            continue
        candidate = Preview(start, parsed.end, parsed.width, parsed.height)
        score = candidate.width * candidate.height
        if best is None or (score, candidate.end - candidate.start) > (
            best.width * best.height,
            best.end - best.start,
        ):
            best = candidate

    raise ScanLimitExceeded("too many embedded JPEG candidates")


def read_cr2(filename: str, max_file_size: int = MAX_FILE_SIZE) -> bytes:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)

    descriptor = os.open(filename, flags)
    try:
        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            raise PreviewError("CR2 path is not a regular file")
        if file_info.st_size > max_file_size:
            raise PreviewError("CR2 is too large to preview safely")

        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            data = source.read(max_file_size + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(data) > max_file_size:
        raise PreviewError("CR2 grew beyond the safe preview limit")
    return data


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: kukni-extract-preview.py FILE.CR2", file=sys.stderr)
        return 2

    try:
        data = read_cr2(sys.argv[1])
        preview = find_best_preview(data)
    except (OSError, PreviewError) as error:
        print(f"could not preview CR2: {error}", file=sys.stderr)
        return 1
    except MemoryError:
        print("could not preview CR2: memory limit exceeded", file=sys.stderr)
        return 1

    if preview is None:
        print("no displayable embedded JPEG found", file=sys.stderr)
        return 1

    try:
        sys.stdout.buffer.write(memoryview(data)[preview.start : preview.end])
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
