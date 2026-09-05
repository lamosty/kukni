#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Require real pixels, continuous navigation, and a clean unavailable view."""

import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
from gi.repository import Adw, Gio, GLib, Gtk
from kukni.application import KukniApplication
from kukni.renderers.image_view import ImagePreviewView
from kukni.session import PreviewState
from kukni.window import PreviewWindow
from image_fixtures import png


def capture(window, path):
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
    if not texture.save_to_png(str(path)):
        raise AssertionError('Could not capture the rendered UI')


class ImageSmoke(Adw.Application):
    def __init__(self, root, screenshots):
        super().__init__(application_id='io.github.lamosty.Kukni.ImageSmoke')
        self.root, self.screenshots = root, screenshots
        self.failures = []
        self.phase = 0
        self.polls = 0
        self.window = None

    def do_activate(self):
        KukniApplication._load_styles()
        self.window = PreviewWindow(self)
        self.show('landscape.png')
        GLib.timeout_add(50, self.poll)

    def show(self, name):
        self.name = name
        self.polls = 0
        self.window.show_file(Gio.File.new_for_path(str(self.root / name)))

    def poll(self):
        self.polls += 1
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.polls < 200:
            return GLib.SOURCE_CONTINUE
        try:
            expected = PreviewState.FALLBACK if self.phase == 1 else PreviewState.PREVIEW
            if snapshot.state is not expected:
                raise AssertionError(f'{self.name}: expected {expected.value}, got {snapshot.state.value}: {snapshot.detail}')
            if snapshot.current_uri != (self.root / self.name).as_uri():
                raise AssertionError('An obsolete file replaced the latest selection')
            if not self.window.get_visible():
                raise AssertionError('The preview window closed during navigation')
            if expected is PreviewState.PREVIEW:
                view = self.window._stack.get_child_by_name('content')
                if not isinstance(view, ImagePreviewView):
                    raise AssertionError('A successful image route must display pixels')
                expected_size = (600, 900) if self.name == 'portrait.png' else (1200, 800)
                if (view.texture.get_width(), view.texture.get_height()) != expected_size:
                    raise AssertionError('The displayed texture does not match the image')
            GLib.timeout_add(200, self.advance)
        except Exception as error:
            self.failures.append(str(error))
            self.quit()
        return GLib.SOURCE_REMOVE

    def advance(self):
        try:
            if self.screenshots:
                capture(self.window, self.screenshots / f'preview-{self.phase}.png')
            self.phase += 1
            if self.phase == 1:
                self.show('unknown.bin')
            elif self.phase == 2:
                # Cancel one real worker request and select another immediately.
                self.show('landscape.png')
                GLib.timeout_add(5, lambda: self.show('portrait.png'))
            elif self.phase == 3:
                self.show('landscape.png')
            else:
                self.window.close()
                self.quit()
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(50, self.poll)
        except Exception as error:
            self.failures.append(str(error))
            self.quit()
        return GLib.SOURCE_REMOVE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / 'landscape.png').write_bytes(png(1200, 800))
        (root / 'portrait.png').write_bytes(png(600, 900, transparent=True))
        (root / 'unknown.bin').write_bytes(b'\0\1\2opaque binary data')
        app = ImageSmoke(root, args.screenshots)
        status = app.run(['kukni-image-smoke'])
    for failure in app.failures:
        print('Image smoke failure:', failure, file=sys.stderr)
    if status or app.failures:
        return 1
    print('Real image rendering and mixed-file navigation smoke test passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
