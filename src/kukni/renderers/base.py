# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Shared contracts for rich Kukni renderers."""

from __future__ import annotations

from typing import Any, Callable, Protocol


ReadyCallback = Callable[[Any, str], None]
ErrorCallback = Callable[[str], None]


class Renderer(Protocol):
    """A capability-probed renderer that never owns application lifecycle."""

    id: str

    def supports(self, file: Any, info: Any) -> bool:
        """Return whether this renderer can handle one local file."""

    def render(
        self,
        file: Any,
        info: Any,
        cancellable: Any,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        """Start rendering and resolve through exactly one callback."""
