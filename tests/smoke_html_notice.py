#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Prove app-shell HTML ends promptly in a native notice, without WebKit."""

import argparse
from pathlib import Path
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from kukni.renderers.html import HtmlRenderer
from kukni.renderers.registry import RendererRegistry
from kukni.session import PreviewState
from kukni.window import PreviewWindow
from smoke_images import capture


class NoticeSmoke(Adw.Application):
    def __init__(self, sample: Path, screenshot: Path | None):
        super().__init__(application_id="io.github.lamosty.Kukni.HtmlNoticeSmoke")
        self.sample, self.screenshot = sample, screenshot
        self.failures = []
        self.elapsed = 0.0

    def do_activate(self):
        self.renderer = HtmlRenderer()
        self.window = PreviewWindow(self, RendererRegistry((self.renderer,)))
        self.started = time.monotonic()
        self.window.show_file(Gio.File.new_for_path(str(self.sample)))
        GLib.timeout_add(25, self.poll)

    def poll(self):
        self.elapsed = time.monotonic() - self.started
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.elapsed < 2:
            return GLib.SOURCE_CONTINUE
        view = self.window._stack.get_visible_child()
        if snapshot.state is not PreviewState.PREVIEW or "Interactive HTML not run" not in snapshot.detail:
            self.failures.append("Interactive HTML did not resolve to its native notice within two seconds")
        if not isinstance(view, Gtk.Box) or getattr(view, "preview_geometry", None) != ("fallback", 0, 0):
            self.failures.append("Interactive HTML did not request a compact native card")
        else:
            icon = view.get_first_child()
            theme = Gtk.IconTheme.get_for_display(view.get_display())
            if not isinstance(icon, Gtk.Image) or not theme.has_icon(icon.get_icon_name()):
                self.failures.append("HTML notice uses a missing theme icon")
        if self.renderer._loading_views:
            self.failures.append("Interactive HTML unexpectedly started a WebKit view")
        if not self.window.get_visible():
            self.failures.append("Interactive HTML closed the preview")
        GLib.timeout_add(350, self.finish)
        return GLib.SOURCE_REMOVE

    def finish(self):
        if self.screenshot:
            self.screenshot.parent.mkdir(parents=True, exist_ok=True)
            capture(self.window, self.screenshot)
        if self.window.get_width() > 600:
            self.failures.append("Explanation grew into a large empty document window")
        self.window.close()
        self.quit()
        return GLib.SOURCE_REMOVE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, help="Optional local app-shell HTML for private inert QA")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        sample = args.sample or Path(temporary) / "interactive.html"
        if args.sample is None:
            sample.write_text('<div id="root"></div><script>fetch("https://invalid.example/app").then(render)</script>')
        # Even the sandbox probe must be unnecessary for an inert native notice.
        with mock.patch("kukni.renderers.html.webkit_runtime_available", side_effect=AssertionError("Unexpected engine probe")) as probe:
            app = NoticeSmoke(sample, args.screenshot)
            status = app.run(["kukni-html-notice-smoke"])
            if probe.called:
                app.failures.append("App-shell HTML probed WebKit before returning its notice")
    if status or app.failures:
        for failure in app.failures:
            print(failure, file=sys.stderr)
        return 1
    print(f"Native HTML notice passed without engine startup ({app.elapsed:.3f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
