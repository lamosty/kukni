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
def preferred_window_size(
    kind: str,
    monitor: Size,
    width: int = 0,
    height: int = 0,
) -> Size:
    maximum = Size(max(1, min(1440, int(monitor.width * .86))),
                   max(1, min(1040, int(monitor.height * .86))))
    if kind in ("image", "pdf") and width > 0 and height > 0:
        chrome = 100
        canvas_width = max(1, min(1120, maximum.width - 24))
        canvas_height = max(1, min(860, maximum.height - chrome))
        scale = min(1.0, canvas_width / width, canvas_height / height)
        wanted = Size(max(360, round(width * scale) + 24),
                      max(280, round(height * scale) + chrome))
    else:
        wanted = {
            "text": Size(900, 720),
            "document": Size(980, 760),
            "audio": Size(600, 320),
            "video": Size(1000, 660),
            "pdf": Size(650, 880),
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
