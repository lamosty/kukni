#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Check native media delivery and navigation; installed checks verify codecs."""

from pathlib import Path
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk
from kukni.media_worker import MediaWorkerOutput, MediaWorkerResult, MediaWorkerError
from kukni.renderers.image_view import ImagePreviewView
from kukni.session import PreviewState
from kukni.window import PreviewWindow


def prepare(path, **_kwargs):
    if Path(path).name == "broken.wmv":
        raise MediaWorkerError("synthetic failure")
    if path.endswith(".wav"):
        return MediaWorkerOutput(MediaWorkerResult("audio", False, True, 65_000_000,
                                                   0, 0, "none", 0), b"")
    return MediaWorkerOutput(MediaWorkerResult("video", True, False, 1_000_000,
                                               64, 48, "rgba8", 64 * 48 * 4),
                             b"\0\0\xff\xff" * (64 * 48))


class App(Adw.Application):
    def __init__(self, root):
        super().__init__(application_id="io.github.lamosty.Kukni.MediaStillTest")
        self.root = root
        self.failures = []
        self.phase = 0
        self.polls = 0

    def do_activate(self):
        self.window = PreviewWindow(self)
        self.show("clip.wmv")
        GLib.timeout_add(50, self.poll)

    def show(self, name):
        self.window.show_file(Gio.File.new_for_path(str(self.root / name)))

    def poll(self):
        self.polls += 1
        try:
            assert self.polls < 150, "media UI timed out"
            state = self.window.session.snapshot
            if state.state is PreviewState.OPENING:
                return GLib.SOURCE_CONTINUE
            assert self.window.get_visible()
            if self.phase == 0:
                assert state.state is PreviewState.PREVIEW, state.detail
                view = self.window._stack.get_child_by_name("content")
                assert isinstance(view, ImagePreviewView)
                assert view.texture.get_width() == 64
                assert "still frame" in state.detail
                self.surface = self.window.get_surface()
                self.show("sound.wav")
            elif self.phase == 1:
                assert state.state is PreviewState.PREVIEW, state.detail
                view = self.window._stack.get_child_by_name("content")
                assert isinstance(view, Gtk.Box)
                assert "duration preview" in state.detail
                assert self.surface == self.window.get_surface()
                self.show("broken.wmv")
            else:
                assert state.state is PreviewState.FALLBACK, state.detail
                assert self.surface == self.window.get_surface()
                self.window.close()
                self.quit()
                return GLib.SOURCE_REMOVE
            self.phase += 1
            return GLib.SOURCE_CONTINUE
        except Exception as error:
            self.failures.append(str(error))
            self.window.close()
            self.quit()
            return GLib.SOURCE_REMOVE


def main():
    with tempfile.TemporaryDirectory(prefix="kukni-media-ui-") as temporary:
        root = Path(temporary)
        for name in ("clip.wmv", "broken.wmv"):
            (root / name).write_bytes(bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c"))
        (root / "sound.wav").write_bytes(b"RIFF\x24\0\0\0WAVEfmt ")
        app = App(root)
        with mock.patch("kukni.renderers.media_still.run_media_worker", side_effect=prepare):
            result = app.run(["kukni-media-still-test"])
        if result or app.failures:
            raise AssertionError(app.failures)
    print("GTK media still, audio and fallback navigation: Passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
