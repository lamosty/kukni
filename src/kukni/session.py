# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Preview-session state independent of any desktop or rendering toolkit."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PreviewState(str, Enum):
    CLOSED = "closed"
    OPENING = "opening"
    PREVIEW = "preview"
    FALLBACK = "fallback"
    ERROR = "error"


class Direction(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class PreviewToken:
    """Identifies one file request so stale renderer work can be ignored."""

    generation: int
    uri: str


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    generation: int
    current_uri: str
    direction: Direction


@dataclass(frozen=True, slots=True)
class PreviewSnapshot:
    generation: int
    current_uri: str | None
    state: PreviewState
    detail: str | None = None


class PreviewSession:
    """Owns one persistent preview session across file changes."""

    _RESULT_STATES = {
        PreviewState.PREVIEW,
        PreviewState.FALLBACK,
        PreviewState.ERROR,
    }

    def __init__(self) -> None:
        self._generation = 0
        self._current_uri: str | None = None
        self._state = PreviewState.CLOSED
        self._detail: str | None = None

    @property
    def snapshot(self) -> PreviewSnapshot:
        return PreviewSnapshot(
            generation=self._generation,
            current_uri=self._current_uri,
            state=self._state,
            detail=self._detail,
        )

    @property
    def is_open(self) -> bool:
        return self._state is not PreviewState.CLOSED

    def show(
        self,
        uri: str,
        *,
        close_if_already_shown: bool = False,
    ) -> PreviewToken | None:
        if not uri:
            raise ValueError("preview URI must not be empty")

        if (
            close_if_already_shown
            and self.is_open
            and self._current_uri == uri
        ):
            self.close()
            return None

        self._generation += 1
        self._current_uri = uri
        self._state = PreviewState.OPENING
        self._detail = None
        return PreviewToken(self._generation, uri)

    def resolve(
        self,
        token: PreviewToken,
        state: PreviewState,
        detail: str | None = None,
    ) -> bool:
        if state not in self._RESULT_STATES:
            raise ValueError(f"invalid renderer result state: {state.value}")
        if (
            self._state is PreviewState.CLOSED
            or token.generation != self._generation
            or token.uri != self._current_uri
        ):
            return False

        self._state = state
        self._detail = detail
        return True

    def request_navigation(self, direction: Direction) -> NavigationRequest:
        if not self.is_open or self._current_uri is None:
            raise RuntimeError("cannot navigate a closed preview session")
        return NavigationRequest(
            generation=self._generation,
            current_uri=self._current_uri,
            direction=direction,
        )

    def close(self) -> None:
        # Advancing the generation invalidates all outstanding renderer work.
        self._generation += 1
        self._current_uri = None
        self._state = PreviewState.CLOSED
        self._detail = None
