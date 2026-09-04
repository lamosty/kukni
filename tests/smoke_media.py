#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise a synthetic MKV without depending on private media files."""

from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib

try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except (ImportError, ValueError):
    Gst = None

from kukni.renderers.media import MediaPreviewView, MediaRenderer
from kukni.renderers.registry import RendererRegistry
from kukni.session import PreviewState
from kukni.window import PreviewWindow


REQUIRED_ELEMENTS = (
    "videotestsrc",
    "capsfilter",
    "vp8enc",
    "matroskamux",
    "filesink",
)


def generate_mkv(path: Path) -> bool:
    """Generate a tiny deterministic VP8/MKV using installed GStreamer elements."""

    if Gst is None:
        return False
    Gst.init(None)
    if any(Gst.ElementFactory.find(name) is None for name in REQUIRED_ELEMENTS):
        return False

    pipeline = Gst.Pipeline.new("kukni-media-smoke-generator")
    source = Gst.ElementFactory.make("videotestsrc", "source")
    caps_filter = Gst.ElementFactory.make("capsfilter", "caps")
    encoder = Gst.ElementFactory.make("vp8enc", "encoder")
    muxer = Gst.ElementFactory.make("matroskamux", "muxer")
    output = Gst.ElementFactory.make("filesink", "output")
    elements = (source, caps_filter, encoder, muxer, output)
    if pipeline is None or any(element is None for element in elements):
        return False

    source.set_property("num-buffers", 36)
    source.set_property("is-live", False)
    source.set_property("pattern", 0)
    caps_filter.set_property(
        "caps",
        Gst.Caps.from_string("video/x-raw,width=320,height=180,framerate=18/1"),
    )
    encoder.set_property("deadline", 1)
    output.set_property("location", str(path))
    for element in elements:
        pipeline.add(element)
    if not all(
        left.link(right)
        for left, right in zip(elements, elements[1:])
    ):
        pipeline.set_state(Gst.State.NULL)
        return False

    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        pipeline.set_state(Gst.State.NULL)
        return False
    bus = pipeline.get_bus()
    message = bus.timed_pop_filtered(
        12 * Gst.SECOND,
        Gst.MessageType.EOS | Gst.MessageType.ERROR,
    )
    pipeline.set_state(Gst.State.NULL)
    return bool(message is not None and message.type == Gst.MessageType.EOS)


class MediaSmokeApplication(Adw.Application):
    def __init__(self, sample: Path, corrupt: Path, replacement: Path) -> None:
        super().__init__(application_id="io.github.lamosty.Kukni.MediaSmoke")
        self.sample = sample
        self.corrupt = corrupt
        self.replacement = replacement
        self.window: PreviewWindow | None = None
        self.media_view: MediaPreviewView | None = None
        self.failures: list[str] = []
        self.checks = 0

    def do_activate(self) -> None:
        self.window = PreviewWindow(
            self,
            RendererRegistry((MediaRenderer(),)),
        )
        self.window.show_file(Gio.File.new_for_path(str(self.sample)))
        GLib.timeout_add(100, self._poll_ready)
        GLib.timeout_add_seconds(12, self._watchdog)

    def _poll_ready(self) -> bool:
        self.checks += 1
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.checks < 90:
            return GLib.SOURCE_CONTINUE
        if snapshot.state is not PreviewState.PREVIEW:
            self.failures.append(
                f"expected MKV preview, got {snapshot.state.value}: {snapshot.detail}"
            )
            self._finish()
            return GLib.SOURCE_REMOVE

        widget = self.window._stack.get_child_by_name("content")
        if not isinstance(widget, MediaPreviewView):
            self.failures.append("media renderer did not return its native GTK view")
            self._finish()
            return GLib.SOURCE_REMOVE
        self.media_view = widget
        if widget.playing:
            self.failures.append("media autoplayed before the user pressed Play")
        if widget.display_stack.get_visible_child_name() != "video":
            self.failures.append("MKV video stream was not shown in the video viewport")

        widget.play_button.emit("clicked")
        GLib.timeout_add(700, self._check_playing)
        return GLib.SOURCE_REMOVE

    def _check_playing(self) -> bool:
        if not self.media_view.playing:
            self.failures.append("Play did not start the synthetic MKV")
        self.media_view.play_button.emit("clicked")
        GLib.timeout_add(250, self._check_paused)
        return GLib.SOURCE_REMOVE

    def _check_paused(self) -> bool:
        if self.media_view.playing:
            self.failures.append("Pause did not stop the synthetic MKV")
        if not self.media_view.seek_scale.get_sensitive():
            self.failures.append("seek control was not enabled for the MKV")
        if " / " not in self.media_view.time_label.get_label():
            self.failures.append("media time and duration were not displayed")
        self.media_view.seek_scale.set_value(0.5)
        old_mute_icon = self.media_view.mute_button.get_icon_name()
        self.media_view.mute_button.emit("clicked")
        if self.media_view.mute_button.get_icon_name() == old_mute_icon:
            self.failures.append("mute control did not update")
        self.media_view.volume_scale.set_value(0.4)

        # Navigating away cancels and tears down an otherwise live backend.
        self.window.show_file(Gio.File.new_for_path(str(self.replacement)))
        GLib.timeout_add(250, self._check_cleanup)
        return GLib.SOURCE_REMOVE

    def _check_cleanup(self) -> bool:
        if not self.media_view.cleaned_up:
            self.failures.append("media backend survived preview cancellation")
        if not self.window.get_visible():
            self.failures.append("navigating away from media closed Kukni")
        self.window.show_file(Gio.File.new_for_path(str(self.sample)))
        self.checks = 0
        GLib.timeout_add(100, self._poll_second_ready)
        return GLib.SOURCE_REMOVE

    def _poll_second_ready(self) -> bool:
        self.checks += 1
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.checks < 70:
            return GLib.SOURCE_CONTINUE
        if snapshot.state is not PreviewState.PREVIEW:
            self.failures.append("MKV could not be reopened after navigation")
            self._finish()
            return GLib.SOURCE_REMOVE

        widget = self.window._stack.get_child_by_name("content")
        if not isinstance(widget, MediaPreviewView):
            self.failures.append("reopened media did not return a media view")
            self._finish()
            return GLib.SOURCE_REMOVE

        # A post-ready decoder failure must stay inside the current preview.
        controller = widget._media_controller
        controller.fail("synthetic post-ready decoder failure")
        if self.window.session.snapshot.state is not PreviewState.PREVIEW:
            self.failures.append("post-ready error closed or replaced the preview")
        if "synthetic post-ready" not in widget.error_message:
            self.failures.append("post-ready error was not shown inside the media view")
        if not widget.cleaned_up:
            self.failures.append("post-ready error did not stop its media backend")
        self.window.show_file(Gio.File.new_for_path(str(self.corrupt)))
        self.checks = 0
        GLib.timeout_add(100, self._poll_corrupt_result)
        return GLib.SOURCE_REMOVE

    def _poll_corrupt_result(self) -> bool:
        self.checks += 1
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.checks < 70:
            return GLib.SOURCE_CONTINUE
        if snapshot.state is not PreviewState.FALLBACK:
            self.failures.append(
                "pre-ready decoder error did not use the universal fallback"
            )
        if not self.window.get_visible():
            self.failures.append("invalid media closed the preview window")
        self._finish()
        return GLib.SOURCE_REMOVE

    def _watchdog(self) -> bool:
        self.failures.append("media smoke test timed out")
        self._finish()
        return GLib.SOURCE_REMOVE

    def _finish(self) -> None:
        if self.window is not None:
            self.window.close()
        self.quit()


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory, "synthetic.mkv")
        corrupt = Path(directory, "corrupt.mkv")
        replacement = Path(directory, "unsupported.bin")
        corrupt.write_bytes(b"not a media container")
        replacement.write_bytes(b"fallback")
        if not generate_mkv(sample):
            print("SKIP: synthetic MKV generation elements are unavailable")
            return 0
        application = MediaSmokeApplication(sample, corrupt, replacement)
        status = application.run([])
        for failure in application.failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        if application.failures:
            return 1
        print("Native MKV media preview smoke test passed")
        return status


if __name__ == "__main__":
    raise SystemExit(main())
