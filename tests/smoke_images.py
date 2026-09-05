#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise real pixels, bounded adaptive geometry, zoom/pan and continuity."""

import argparse
import ctypes
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
gi.require_version('Graphene', '1.0')
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk
from kukni.application import KukniApplication
from kukni.renderers.image_view import ImagePreviewView
from kukni.session import PreviewState
from kukni.window import PreviewWindow
from image_fixtures import png

FIXTURES = {
    'landscape.png': (1200, 800),
    'similar.png': (1180, 790),
    'portrait.png': (600, 900),
    'square.png': (900, 900),
    'panorama.png': (2400, 400),
    'tiny.png': (64, 48),
}


def capture(window, path):
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    # Render the exact widget viewport; automatic node bounds can crop native
    # shadows or expand a scrolled canvas beyond the actual window screenshot.
    bounds = Graphene.Rect().init(0, 0, window.get_width(), window.get_height())
    texture = window.get_native().get_renderer().render_texture(snapshot.to_node(), bounds)
    if not texture.save_to_png(str(path)):
        raise AssertionError('Could not capture the rendered UI')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class NativeKeys:
    """Optional real X11 input to catch accelerator/popover-grab regressions."""

    def __init__(self):
        gi.require_version('GdkX11', '4.0')
        from gi.repository import GdkX11  # Register the X11 surface methods.
        self.x11 = ctypes.CDLL('libX11.so.6')
        self.xtst = ctypes.CDLL('libXtst.so.6')
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x11.XStringToKeysym.restype = ctypes.c_ulong
        self.x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XKeysymToKeycode.restype = ctypes.c_uint
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.display = self.x11.XOpenDisplay(None)
        require(self.display, 'Native key smoke requires the isolated X11 test display')

    def send(self, window, *keys):
        self.x11.XSetInputFocus(self.display, window.get_surface().get_xid(), 2, 0)
        codes = [self.x11.XKeysymToKeycode(self.display, self.x11.XStringToKeysym(key.encode())) for key in keys]
        for code in codes:
            self.xtst.XTestFakeKeyEvent(self.display, code, 1, 10)
        for code in reversed(codes):
            self.xtst.XTestFakeKeyEvent(self.display, code, 0, 10)
        self.x11.XFlush(self.display)


class ImageSmoke(Adw.Application):
    def __init__(self, root, screenshots, native_input=False):
        super().__init__(application_id='io.github.lamosty.Kukni.ImageSmoke')
        self.root, self.screenshots = root, screenshots
        self.failures = []
        self.window = None
        self.surface = None
        self.native_keys = NativeKeys() if native_input else None

    def do_activate(self):
        self.hold()
        KukniApplication._load_styles()
        self.window = PreviewWindow(self)
        self.steps = self.run_checks()
        self.advance()

    def advance(self):
        try:
            GLib.timeout_add(next(self.steps), self.advance)
        except StopIteration:
            self.window.close()
            self.release()
            self.quit()
        except Exception as error:
            import traceback
            traceback.print_exc()
            self.failures.append(str(error))
            self.quit()
        return GLib.SOURCE_REMOVE

    def show(self, name):
        self.window.show_file(Gio.File.new_for_path(str(self.root / name)))

    def action(self, name):
        keys = {'win.actual-size': ('1',), 'win.zoom-in': ('equal',),
                'win.zoom-out': ('minus',), 'win.fit': ('0',),
                'win.info': ('Control_L', 'i')}
        if self.native_keys and name in keys:
            self.native_keys.send(self.window, *keys[name])
        else:
            self.window.activate_action(name, None)

    def ready(self, name):
        for _ in range(200):
            if self.window.session.snapshot.state is not PreviewState.OPENING:
                break
            yield 50
        snapshot = self.window.session.snapshot
        expected = PreviewState.FALLBACK if name == 'unknown.bin' else PreviewState.PREVIEW
        require(snapshot.state is expected, f'{name}: got {snapshot.state.value}: {snapshot.detail}')
        require(snapshot.current_uri == (self.root / name).as_uri(), 'Obsolete content replaced the latest file')
        require(self.window.get_visible(), 'Window closed during navigation')
        if self.surface is None:
            self.surface = self.window.get_surface()
        require(self.window.get_surface() == self.surface, 'Selection recreated the toplevel surface')
        yield 350  # Wait for coalesced sizing and the native frame, not only a callback.
        width, height = self.window.get_width(), self.window.get_height()
        monitor = self.window._monitor_size()
        require(width <= monitor.width and height <= monitor.height, 'Preview escaped logical monitor bounds')
        if expected is PreviewState.PREVIEW:
            view = self.view()
            require((view.texture.get_width(), view.texture.get_height()) == FIXTURES[name], 'Wrong retained pixels')
            require(view.fit_mode and view.zoom <= 1, 'Default fit upscaled the retained preview')
            require(view.picture.get_width() <= view.scroller.get_width(), 'Fit canvas overflows horizontally')
            require(view.picture.get_height() <= view.scroller.get_height(), 'Fit canvas overflows vertically')
            require(view._tick_id == 0, 'Idle preview keeps scheduling animation frames')
        self.snapshot(name.removesuffix('.png').removesuffix('.bin'))

    def view(self):
        view = self.window._stack.get_child_by_name('content')
        require(isinstance(view, ImagePreviewView), 'An image route did not display pixels')
        return view

    def snapshot(self, name):
        if self.screenshots:
            capture(self.window, self.screenshots / f'{name}.png')

    def run_checks(self):
        require(not self.window.lookup_action('navigate-right').get_enabled(), 'Standalone navigation misleadingly enabled')
        self.show('landscape.png')
        yield from self.ready('landscape.png')
        landscape = self.window.get_default_size()
        require(landscape[0] > landscape[1], 'Landscape got a portrait window')
        self.show('similar.png')
        yield from self.ready('similar.png')
        require(self.window.get_default_size() == landscape, 'Similar images bounced the window size')
        self.show('unknown.bin')
        yield from self.ready('unknown.bin')
        require(self.window.get_default_size()[0] < landscape[0], 'Fallback did not become compact')
        require(not self.window.lookup_action('zoom-in').get_enabled(), 'Fallback exposes image actions')

        # Cancel one real worker and ignore its result plus its pending size.
        self.show('landscape.png')
        yield 5
        self.show('portrait.png')
        yield from self.ready('portrait.png')
        portrait = self.window.get_default_size()
        require(portrait[1] > portrait[0], 'Portrait did not get a tall window')
        for name in ('square.png', 'panorama.png', 'tiny.png'):
            self.show(name)
            yield from self.ready(name)
        require(self.view().zoom == 1, 'Tiny image was upscaled by Fit')
        require(self.view().picture.get_width() == 64, 'Tiny pixels stretched to fill the window')

        self.show('landscape.png')
        yield from self.ready('landscape.png')
        yield 800
        view = self.view()
        texture = view.texture
        initial = (self.window.get_width(), self.window.get_height())
        self.action('win.actual-size')
        yield 100
        require(view.zoom == 1 and not view.fit_mode, '1:1 action did not select preview pixel size')
        require(view.picture.get_width() == 1200, '1:1 has the wrong native pixel allocation')
        self.action('win.zoom-in')
        yield 100
        require(view.zoom == 1.25, 'Zoom-in action did not change scale')
        self.snapshot('zoomed')
        view.pan_to(150, 80)
        yield 100
        require(view.scroller.get_hadjustment().get_value() > 0, 'Zoomed image cannot pan horizontally')
        require(view.scroller.get_vadjustment().get_value() > 0, 'Zoomed image cannot pan vertically')
        require(view.texture is texture, 'Zoom allocated a new decoded texture')
        require(initial == (self.window.get_width(), self.window.get_height()), 'Zoom/pan resized the outer window')
        self.action('win.zoom-out')
        yield 50
        require(view.zoom == 1, 'Zoom-out action did not change scale')
        class ControlScroll:
            def get_current_event_state(self):
                return Gdk.ModifierType.CONTROL_MASK
        require(view._on_scroll(ControlScroll(), 0, -1), 'Ctrl-wheel was not consumed')
        require(view.zoom == 1.25, 'Ctrl-wheel did not zoom')
        self.action('win.fit')
        yield 100
        require(view.fit_mode and view.zoom <= 1, 'Fit action did not restore fit')
        self.action('win.info')
        yield 100
        require(self.window._info_popover.get_visible(), 'Info action did not open metadata')
        require('1200 × 800' in self.window._info_label.get_label(), 'Info lacks retained dimensions')
        self.snapshot('info')
        if self.screenshots:
            capture(self.window._info_popover, self.screenshots / 'info-popup.png')
        self.action('win.info')
        yield 100
        require(not self.window._info_popover.get_visible(), 'Info action did not close metadata')
        require(self.window._title.get_subtitle() == '', 'Technical metadata crowds the title')

        # No decoder/re-render is needed to truthfully explain a bounded source.
        view.set_texture(texture, 6000, 4000)
        require('not full-source detail' in view.actual_button.get_tooltip_text(), 'Downscaled 1:1 is misleading')
        view.actual_size()
        yield 100
        self.snapshot('retained-pixels')
        self.action('win.fit')
        yield 100

        # Set a native size outside the sizing policy, just as a compositor's
        # manual resize changes allocation. Subsequent content must respect it.
        self.window.set_default_size(740, 540)
        yield 150
        require(self.window._sizing.manual, 'Unsolicited resize did not take precedence')
        manual = (self.window.get_width(), self.window.get_height())
        self.show('portrait.png')
        yield from self.ready('portrait.png')
        require((self.window.get_width(), self.window.get_height()) == manual, 'Portrait overrode manual size')
        self.show('unknown.bin')
        yield from self.ready('unknown.bin')
        require((self.window.get_width(), self.window.get_height()) == manual, 'Fallback overrode manual size')
        self.snapshot('manual-size-fallback')

        # Empty activation must invalidate in-flight work, not resurrect it.
        self.show('landscape.png')
        self.window.show_empty()
        yield 350
        require(self.window.session.snapshot.state is PreviewState.CLOSED, 'Empty activation kept old session')
        require(self.window._stack.get_visible_child_name() == 'empty', 'Late image replaced empty activation')
        if self.native_keys:
            # A native popover must not turn Escape into "close only Info" or
            # prevent Space from closing the preview on the next invocation.
            self.action('win.info')
            yield 100
            self.native_keys.send(self.window, 'Escape')
            yield 100
            require(not self.window.get_visible(), 'Info swallowed Escape instead of closing preview')
            self.window = PreviewWindow(self)
            self.window.show_empty()
            yield 150
            self.native_keys.send(self.window, 'space')
            yield 100
            require(not self.window.get_visible(), 'Space did not close the preview')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots', type=Path)
    parser.add_argument('--native-input', action='store_true', help='Inject real keys on the isolated X11 test display (requires libXtst)')
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name, dimensions in FIXTURES.items():
            (root / name).write_bytes(png(*dimensions, transparent=name == 'portrait.png'))
        (root / 'unknown.bin').write_bytes(b'\0\1\2opaque binary data')
        app = ImageSmoke(root, args.screenshots, args.native_input)
        status = app.run(['kukni-image-smoke'])
    for failure in app.failures:
        print('Image smoke failure:', failure, file=sys.stderr)
    if status or app.failures:
        return 1
    print('Real pixels, adaptive geometry, zoom/pan, Info, manual resize and stale-selection smoke passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
