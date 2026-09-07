# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Pure logical-pixel sizing policy; the compositor still owns placement."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Size:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Sizes must be positive")


# @decision Leave room for desktop chrome instead of using physical pixels or
# assuming a Wayland workarea/position API. Gdk monitor geometry is already in
# logical pixels, including mixed-DPI displays; never multiply it by scale.
# @why The former 1120×860 canvas cap wasted usable space on larger monitors.
# Let real images use the bounded screen area, without stretching tiny images
# or making metadata/audio cards needlessly large.
def preferred_window_size(
    kind: str,
    monitor: Size,
    width: int = 0,
    height: int = 0,
) -> Size:
    maximum = Size(max(1, min(1800, int(monitor.width * .90))),
                   max(1, min(1440, int(monitor.height * .90))))
    if kind == "image" and width > 0 and height > 0:
        chrome = 100
        canvas_width = max(1, maximum.width - 24)
        canvas_height = max(1, maximum.height - chrome)
        scale = min(1.0, canvas_width / width, canvas_height / height)
        wanted = Size(max(360, round(width * scale) + 24),
                      max(280, round(height * scale) + chrome))
    elif kind == "pdf":
        # @decision A document is a readable, scrolling page column, not a
        # photograph that must fit in its entirety. Give portrait pages enough
        # width for text; scrolling reveals the rest. Only the initial page
        # suggests an orientation, so later pages never bounce the window.
        wanted = Size(1280 if width > height > 0 else 1040, 1280)
    else:
        wanted = {
            "text": Size(1024, 1040),
            "document": Size(1200, 1040),
            "folder": Size(700, 600),
            "audio": Size(600, 320),
            "video": Size(1280, 800),
        }.get(kind, Size(520, 360))
    return Size(min(wanted.width, maximum.width),
                min(wanted.height, maximum.height))


def meaningfully_different(current: Size, wanted: Size) -> bool:
    """Ignore small image-size differences, but adapt to a changed aspect."""
    return any(abs(new - old) >= max(64, old * .12) for old, new in (
        (current.width, wanted.width), (current.height, wanted.height)))


class AdaptiveSizing:
    """Respect an unsolicited resize for the rest of this window's lifetime."""

    def __init__(self) -> None:
        self.manual = False
        self.observed: Size | None = None
        self.settle_until = 0.0

    # @constraint GTK/Wayland cannot identify who changed an allocation. Ignore
    # our short compositor settling interval, then conservatively treat any
    # changed allocation as the user's choice (including tiling). Never fight
    # that choice. A resize during this short interval is indistinguishable.
    def observe(self, size: Size, now: float) -> None:
        if self.observed is not None and size != self.observed:
            if now >= self.settle_until:
                self.manual = True
        self.observed = size

    def request(self, wanted: Size, now: float) -> bool:
        if self.manual:
            return False
        if self.observed is not None and not meaningfully_different(self.observed, wanted):
            return False
        self.settle_until = now + .7
        return True
