#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Prove executable scripts are displayed, never run, and remain navigable."""

from pathlib import Path
import shlex
import stat
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from kukni.renderers.registry import RendererRegistry
from kukni.renderers.text import TextPreviewView, TextRenderer
from kukni.session import PreviewState
from kukni.window import PreviewWindow


class TextSmokeApplication(Adw.Application):
    def __init__(self, script: Path, marker: Path, gpx: Path) -> None:
        super().__init__(application_id="io.github.lamosty.Kukni.TextSmoke")
        self.script = script
        self.marker = marker
        self.gpx = gpx
        self.failures: list[str] = []
        self.navigation: list[str] = []
        self.window = None
        self.polls = 0

    def do_activate(self) -> None:
        self.window = PreviewWindow(self, RendererRegistry((TextRenderer(),)))
        self.window.set_navigation_available(True)  # Synthetic attached session.
        self.window.connect(
            "navigation-requested",
            lambda _window, direction: self.navigation.append(direction),
        )
        self.window.show_file(Gio.File.new_for_path(str(self.script)))
        GLib.timeout_add(50, self._poll_preview)
        GLib.timeout_add_seconds(5, self._watchdog)

    def _poll_preview(self) -> bool:
        self.polls += 1
        state = self.window.session.snapshot.state
        if state is PreviewState.OPENING and self.polls < 80:
            return GLib.SOURCE_CONTINUE
        if state is not PreviewState.PREVIEW:
            self.failures.append(f"expected preview, received {state.value}")

        view = self.window._stack.get_child_by_name("content")
        if not isinstance(view, TextPreviewView):
            self.failures.append("text renderer did not return its native source view")
        else:
            if view.text_view.get_editable():
                self.failures.append("source view is editable")
            if view.text_view.get_focusable():
                self.failures.append("source view can steal keyboard navigation focus")
            if view.text_view.grab_focus():
                self.failures.append("source view accepted keyboard focus")
            if view.safety_banner is None or not view.safety_banner.get_visible():
                self.failures.append("executable safety banner is missing")
            start, end = view.text_view.get_buffer().get_bounds()
            displayed = view.text_view.get_buffer().get_text(start, end, True)
            if "KUKNI_TEXT_SMOKE_EXECUTED" not in displayed:
                self.failures.append("script source was not displayed")
            if "<literal-tag>" not in displayed:
                self.failures.append("source text was interpreted as markup")
            if "␛[2J" not in displayed or "⟦RLO⟧" not in displayed:
                self.failures.append("hidden control characters were not revealed")
            if "\x1b" in displayed or "\u202e" in displayed:
                self.failures.append("unsafe control characters remain in the view")

        if self.marker.exists():
            self.failures.append("preview executed the selected script")
        action = self.window.lookup_action("navigate-down")
        if action is None:
            self.failures.append("down-navigation action is unavailable")
            self._finish()
            return GLib.SOURCE_REMOVE
        action.activate(None)
        GLib.timeout_add(220, self._verify_navigation)
        return GLib.SOURCE_REMOVE

    def _verify_navigation(self) -> bool:
        if self.navigation != ["down"]:
            self.failures.append(f"unexpected navigation events: {self.navigation}")
        if self.window.session.snapshot.state is not PreviewState.PREVIEW:
            self.failures.append("navigation closed or replaced the text preview")
        if not self.window.get_visible():
            self.failures.append("text preview window closed during navigation")
        self.text_window_size = self.window.get_default_size()
        if not all(value > 0 for value in self.text_window_size):
            self.failures.append("text preview has invalid window geometry")
        if self.marker.exists():
            self.failures.append("navigation executed the selected script")
        self.window.show_file(Gio.File.new_for_path(str(self.gpx)))
        self.polls = 0
        GLib.timeout_add(50, self._poll_gpx)
        return GLib.SOURCE_REMOVE

    def _poll_gpx(self) -> bool:
        self.polls += 1
        state = self.window.session.snapshot.state
        if state is PreviewState.OPENING and self.polls < 80:
            return GLib.SOURCE_CONTINUE
        if state is not PreviewState.PREVIEW:
            self.failures.append(f"expected GPX preview, received {state.value}")

        view = self.window._stack.get_child_by_name("content")
        if not isinstance(view, TextPreviewView):
            self.failures.append("GPX did not use the bounded inert-text renderer")
        else:
            start, end = view.text_view.get_buffer().get_bounds()
            displayed = view.text_view.get_buffer().get_text(start, end, True)
            if "<gpx" not in displayed or "48.1486" not in displayed:
                self.failures.append("GPX source content was not displayed")
            if view.text_view.get_focusable():
                self.failures.append("GPX source view can steal navigation focus")
        if not self.window.get_visible():
            self.failures.append("GPX preview closed the window")
        if self.window.get_default_size() != self.text_window_size:
            self.failures.append("same-family text navigation changed window geometry")
        self._finish()
        return GLib.SOURCE_REMOVE

    def _finish(self) -> None:
        self.window.close()
        self.quit()

    def _watchdog(self) -> bool:
        self.failures.append("text renderer smoke test timed out")
        self._finish()
        return GLib.SOURCE_REMOVE


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        marker = directory / "KUKNI_TEXT_SMOKE_EXECUTED"
        script = directory / "dangerous.sh"
        gpx = directory / "synthetic.gpx"
        script.write_text(
            "#!/bin/sh\n"
            "# This command must be shown but never executed by the preview.\n"
            "# <literal-tag> \x1b[2J \u202econtrol\n"
            f"printf executed > {shlex.quote(str(marker))}\n",
            encoding="utf-8",
        )
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        gpx.write_text(
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            "<gpx version='1.1'><trk><trkseg>"
            "<trkpt lat='48.1486' lon='17.1077'/>"
            "</trkseg></trk></gpx>\n",
            encoding="utf-8",
        )

        application = TextSmokeApplication(script, marker, gpx)
        exit_code = application.run(["kukni-text-smoke"])
        failures = application.failures
        if marker.exists():
            failures.append("script marker exists after application shutdown")

    if exit_code != 0:
        failures.append(f"application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"smoke failure: {failure}", file=sys.stderr)
        return 1
    print("Read-only executable text preview smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
