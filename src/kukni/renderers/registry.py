# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Deterministic capability registry for rich preview renderers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .base import Renderer


class RendererProbeError(RuntimeError):
    """A renderer could not safely determine whether it supports a file."""

    def __init__(self, renderer_id: str, detail: str) -> None:
        super().__init__(f"{renderer_id} capability check failed: {detail}")
        self.renderer_id = renderer_id


class RendererRegistry:
    """Select the first registered renderer that accepts a file."""

    def __init__(self, renderers: Iterable[Renderer] = ()) -> None:
        registered = tuple(renderers)
        identifiers = [renderer.id for renderer in registered]
        if any(not identifier or not identifier.strip() for identifier in identifiers):
            raise ValueError("renderer IDs must not be empty")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("renderer IDs must be unique")
        self._renderers = registered

    @property
    def renderers(self) -> tuple[Renderer, ...]:
        return self._renderers

    def select(self, file: Any, info: Any) -> Renderer | None:
        for renderer in self._renderers:
            try:
                supported = renderer.supports(file, info)
            except Exception as error:
                raise RendererProbeError(renderer.id, str(error)) from error
            if supported:
                return renderer
        return None


def default_registry() -> RendererRegistry:
    """Return built-in rich renderers in deterministic priority order."""

    from .html import HtmlRenderer
    from .media import MediaRenderer
    from .pdf import PdfRenderer
    from .spreadsheet import SpreadsheetRenderer
    from .text import TextRenderer

    return RendererRegistry(
        (
            SpreadsheetRenderer(),
            PdfRenderer(),
            MediaRenderer(),
            HtmlRenderer(),
            TextRenderer(),
        )
    )
