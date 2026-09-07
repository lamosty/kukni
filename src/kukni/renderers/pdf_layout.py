# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Small, GTK-independent geometry model for a lazy continuous PDF canvas."""

from __future__ import annotations

from dataclasses import dataclass


# @constraint Gtk allocation coordinates are signed native integers. Capping
# inexpensive virtual slots (not texture decode size) keeps even 500 extreme
# aspect pages far below that aggregate boundary at maximum UI zoom.
MAX_PAGE_LAYOUT_EDGE = 32_768


@dataclass(frozen=True, slots=True)
class PdfPageRect:
    page: int
    x: int
    y: int
    width: int
    height: int


class PdfDocumentLayout:
    """Lay out bounded page placeholders without retaining their pixels.

    @decision Unknown pages use the first decoded page's aspect ratio. Updating
    one estimate only changes inexpensive geometry; callers can preserve a
    page-relative anchor while GTK reapplies the resulting size requests.
    """

    def __init__(
        self,
        page_count: int,
        first_width: int,
        first_height: int,
        *,
        max_pages: int,
        margin: int = 16,
        gap: int = 16,
    ) -> None:
        if not 1 <= page_count <= max_pages:
            raise ValueError("PDF page count exceeds its placeholder limit")
        if first_width <= 0 or first_height <= 0:
            raise ValueError("PDF page dimensions must be positive")
        self.page_count = page_count
        self.margin = max(0, margin)
        self.gap = max(0, gap)
        self._dimensions = [(first_width, first_height)] * page_count
        self._known = {1}

    def dimensions(self, page: int) -> tuple[int, int]:
        self._check_page(page)
        return self._dimensions[page - 1]

    def set_dimensions(self, page: int, width: int, height: int) -> bool:
        self._check_page(page)
        if width <= 0 or height <= 0:
            raise ValueError("PDF page dimensions must be positive")
        dimensions = (width, height)
        changed = self._dimensions[page - 1] != dimensions
        self._dimensions[page - 1] = dimensions
        self._known.add(page)
        return changed

    def is_known(self, page: int) -> bool:
        self._check_page(page)
        return page in self._known

    def rects(
        self,
        viewport_width: int,
        *,
        basis: str = "width",
        zoom: float = 1.0,
    ) -> tuple[PdfPageRect, ...]:
        if basis not in ("width", "pixels"):
            raise ValueError("Unknown PDF zoom basis")
        zoom = max(0.05, min(8.0, float(zoom)))
        available = max(1, viewport_width - 2 * self.margin)
        sizes: list[tuple[int, int]] = []
        for source_width, source_height in self._dimensions:
            if basis == "width":
                width = max(1, round(available * zoom))
                height = max(1, round(width * source_height / source_width))
            else:
                width = max(1, round(source_width * zoom))
                height = max(1, round(source_height * zoom))
            if max(width, height) > MAX_PAGE_LAYOUT_EDGE:
                scale = MAX_PAGE_LAYOUT_EDGE / max(width, height)
                width = max(1, round(width * scale))
                height = max(1, round(height * scale))
            sizes.append((width, height))
        content_width = max(viewport_width, max(width for width, _height in sizes) + 2 * self.margin)
        y = self.margin
        rects = []
        for page, (width, height) in enumerate(sizes, start=1):
            rects.append(PdfPageRect(page, (content_width - width) // 2, y, width, height))
            y += height + self.gap
        return tuple(rects)

    def visible_pages(
        self,
        rects: tuple[PdfPageRect, ...],
        scroll_y: float,
        viewport_height: float,
        *,
        adjacent: int = 1,
    ) -> tuple[int, ...]:
        if not rects:
            return ()
        top, bottom = max(0.0, scroll_y), max(0.0, scroll_y) + max(1.0, viewport_height)
        visible = [rect.page for rect in rects if rect.y + rect.height >= top and rect.y <= bottom]
        if not visible:
            center = (top + bottom) / 2
            visible = [min(rects, key=lambda rect: abs(rect.y + rect.height / 2 - center)).page]
        low = max(1, min(visible) - max(0, adjacent))
        high = min(self.page_count, max(visible) + max(0, adjacent))
        center = (top + bottom) / 2
        def by_distance(page: int) -> float:
            rect = rects[page - 1]
            return abs(rect.y + rect.height / 2 - center)
        actual = sorted(visible, key=by_distance)
        nearby = sorted(
            (page for page in range(low, high + 1) if page not in visible),
            key=by_distance,
        )
        # @constraint Decode pages intersecting the viewport before prefetching
        # an adjacent page whose center happens to be closer to a tall page.
        return tuple((*actual, *nearby))

    @staticmethod
    def page_at_viewport_center(
        rects: tuple[PdfPageRect, ...], scroll_y: float, viewport_height: float,
    ) -> int:
        center = scroll_y + max(1.0, viewport_height) / 2
        containing = next(
            (rect for rect in rects if rect.y <= center <= rect.y + rect.height),
            None,
        )
        if containing is not None:
            return containing.page
        # The point is in a page gap or outside the document: choose its nearest
        # edge, not its nearest center (which is biased by extreme page heights).
        return min(
            rects,
            key=lambda rect: min(abs(center - rect.y), abs(center - rect.y - rect.height)),
        ).page

    @staticmethod
    def capture_anchor(rects: tuple[PdfPageRect, ...], scroll_y: float) -> tuple[int, float]:
        """Capture the page and fractional position at the viewport's top."""

        if not rects:
            return 1, 0.0
        page = min(
            rects,
            key=lambda rect: 0 if rect.y <= scroll_y <= rect.y + rect.height
            else min(abs(scroll_y - rect.y), abs(scroll_y - rect.y - rect.height)),
        )
        fraction = (scroll_y - page.y) / max(1, page.height)
        return page.page, max(0.0, min(1.0, fraction))

    @staticmethod
    def restore_anchor(rects: tuple[PdfPageRect, ...], anchor: tuple[int, float]) -> float:
        page, fraction = anchor
        if not rects:
            return 0.0
        page = max(1, min(len(rects), page))
        rect = rects[page - 1]
        return rect.y + rect.height * max(0.0, min(1.0, fraction))

    def _check_page(self, page: int) -> None:
        if type(page) is not int or not 1 <= page <= self.page_count:
            raise ValueError("PDF page is outside the document")
