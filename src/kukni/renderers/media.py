# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Native, non-autoplaying previews for local audio and video files.

The preferred backend is GStreamer's ``playbin`` connected to
``gtk4paintablesink``.  Some distributions do not package that sink; on those
systems GTK's native ``Gtk.MediaFile`` GStreamer backend provides the same
in-window controls.  Neither backend is asked to open a non-local URI.
"""

from __future__ import annotations

from functools import lru_cache
import math
import os
import stat
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except (ImportError, ValueError):  # pragma: no cover - distro dependent
    Gst = None

from .base import ErrorCallback, ReadyCallback


MEDIA_SUFFIXES = (
    ".3gp",
    ".aac",
    ".aif",
    ".aiff",
    ".avi",
    ".flac",
    ".m4a",
    ".m4v",
    ".mka",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".oga",
    ".ogg",
    ".ogv",
    ".opus",
    ".wav",
    ".webm",
    ".wma",
    ".wmv",
)

APPLICATION_MEDIA_TYPES = frozenset(
    {
        "application/ogg",
        "application/x-matroska",
        "application/x-ogg",
    }
)

PLAYLIST_CONTENT_TYPES = frozenset(
    {
        "application/m3u",
        "application/mpegurl",
        "application/ram",
        "application/smil+xml",
        "application/vnd.apple.mpegurl",
        "application/vnd.ms-asf",
        "application/vnd.ms-wpl",
        "application/x-mpegurl",
        "application/x-ms-asx",
        "application/x-quicktime-media-link",
        "application/x-quicktimeplayer",
        "application/x-xspf+xml",
        "application/xspf+xml",
        "audio/m3u",
        "audio/mpegurl",
        "audio/scpls",
        "audio/x-m3u",
        "audio/x-mp3-playlist",
        "audio/x-ms-asx",
        "audio/x-mpegurl",
        "audio/x-scpls",
        "video/vnd.mpegurl",
        "video/x-mpegurl",
        "video/x-ms-asx",
        "video/x-ms-wax",
        "video/x-ms-wmx",
        "video/x-ms-wvx",
    }
)

GENERIC_CONTENT_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "application/x-empty",
        "inode/x-empty",
    }
)

BACKEND_GSTREAMER_PAINTABLE = "gstreamer-paintable"
BACKEND_GTK_MEDIA = "gtk-media"
PREPARE_TIMEOUT_SECONDS = 12
DEFAULT_VOLUME = 0.75
HEADER_PROBE_BYTES = 8192

# These formats are instructions to fetch or open other resources, rather than
# self-contained media.  MIME metadata is not a security boundary, so reject
# their common byte signatures even when a playlist has been renamed ``.mkv``.
_PLAYLIST_PREFIXES = (
    b"#extm3u",
    b"[playlist]",
    b"[reference]",
    b"<asx",
    b"<playlist",
    b"<smil",
    b"<?quicktime",
    b"rtsptext",
    b"http://",
    b"https://",
    b"rtsp://",
)
_XML_PLAYLIST_MARKERS = (b"<asx", b"<playlist", b"<smil", b"<?quicktime")

# GstPlayFlags is private to the playbin plugin and therefore not exposed as a
# GI enum.  Keep only local audio/video, software volume, buffering and
# deinterlacing.  In particular, progressive download (128), visualisations
# and subtitles are intentionally disabled.
_PLAY_FLAG_VIDEO = 1
_PLAY_FLAG_AUDIO = 2
_PLAY_FLAG_SOFT_VOLUME = 16
_PLAY_FLAG_BUFFERING = 256
_PLAY_FLAG_DEINTERLACE = 512
SAFE_PLAY_FLAGS = (
    _PLAY_FLAG_VIDEO
    | _PLAY_FLAG_AUDIO
    | _PLAY_FLAG_SOFT_VOLUME
    | _PLAY_FLAG_BUFFERING
    | _PLAY_FLAG_DEINTERLACE
)


class MediaPreviewError(RuntimeError):
    """A media preview could not be started safely."""


def _reject_playlist_header(descriptor: int) -> None:
    """Reject obvious network-capable playlist input from the same descriptor."""

    try:
        header = os.pread(descriptor, HEADER_PROBE_BYTES, 0)
    except OSError as error:
        raise MediaPreviewError("The media header could not be read safely") from error

    # Remove only transport-level bytes and ASCII spacing.  This is deliberately
    # not a general text parser; it recognizes strong playlist signatures before
    # any multimedia backend sees the file.
    normalized = header.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    is_playlist = normalized.startswith(_PLAYLIST_PREFIXES)
    if normalized.startswith(b"<?xml"):
        is_playlist = is_playlist or any(
            marker in normalized for marker in _XML_PLAYLIST_MARKERS
        )
    if is_playlist:
        raise MediaPreviewError(
            "Media playlists and external streams are not previewed"
        )


def supports_media(basename: str | None, content_type: str | None) -> bool:
    """Return whether trusted file metadata identifies supported local media.

    MIME information wins.  A suffix is considered only when the MIME type is
    absent or generic, so a file named ``movie.mkv`` but identified as an image
    is not handed to a media decoder.  Playlists are excluded because they can
    reference remote resources.
    """

    normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if normalized_type in PLAYLIST_CONTENT_TYPES:
        return False
    if normalized_type.startswith(("audio/", "video/")):
        return True
    if normalized_type in APPLICATION_MEDIA_TYPES:
        return True

    generic_type = normalized_type in GENERIC_CONTENT_TYPES
    if normalized_type and not generic_type:
        try:
            generic_type = Gio.content_type_is_unknown(normalized_type)
        except (TypeError, GLib.Error):
            generic_type = False
    normalized_name = (basename or "").casefold()
    return generic_type and normalized_name.endswith(MEDIA_SUFFIXES)


def _open_local_regular_file(path: str) -> tuple[int, os.stat_result]:
    """Open a local regular file and return its still-owned descriptor."""

    if not isinstance(path, str) or not path:
        raise MediaPreviewError("Media preview supports local files only")

    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NONBLOCK"):
        flags |= getattr(os, optional_flag, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MediaPreviewError("The media file could not be opened safely") from error
    try:
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode):
            raise MediaPreviewError("Media preview requires a regular local file")
        if result.st_size <= 0:
            raise MediaPreviewError("The media file is empty")
        _reject_playlist_header(descriptor)
        return descriptor, result
    except Exception:
        os.close(descriptor)
        raise


def validate_local_regular_file(path: str) -> os.stat_result:
    """Open and validate a path through one race-resistant descriptor.

    ``O_NONBLOCK`` prevents a malicious FIFO from hanging the GTK process.  No
    bytes are read here; decoders stream the validated kind of local file.
    """

    descriptor, result = _open_local_regular_file(path)
    try:
        return result
    finally:
        os.close(descriptor)


def format_media_time(seconds: float | int | None) -> str:
    """Format a non-negative duration without allowing unbounded output."""

    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value < 0:
        value = 0.0
    whole_seconds = min(int(value), 359_999_999)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds_part:02d}"
    return f"{minutes}:{seconds_part:02d}"


def _friendly_error(message: object) -> str:
    normalized = " ".join(str(message or "").split())
    if not normalized:
        return "The media stream could not be decoded"
    lowered = normalized.casefold()
    if "missing a plug-in" in lowered or "missing plugin" in lowered:
        return (
            "A required media codec is not installed. Add your distribution's "
            "GStreamer codec pack."
        )
    return f"Media playback stopped: {normalized[:240]}"


@lru_cache(maxsize=1)
def gst_runtime_available() -> bool:
    """Probe the GStreamer runtime without constructing playback pipelines."""

    if Gst is None:
        return False
    try:
        initialized, _arguments = Gst.init_check(None)
        return bool(initialized and Gst.ElementFactory.find("playbin"))
    except Exception:
        return False


@lru_cache(maxsize=1)
def gtk4_paintable_sink_available() -> bool:
    """Return whether the optional Rust GTK 4 GStreamer sink is installed."""

    if not gst_runtime_available() or Gst is None:
        return False
    try:
        return Gst.ElementFactory.find("gtk4paintablesink") is not None
    except Exception:
        return False


def available_media_backends() -> tuple[str, ...]:
    """List usable backends in preference order for diagnostics and tests."""

    backends: list[str] = []
    if gtk4_paintable_sink_available():
        backends.append(BACKEND_GSTREAMER_PAINTABLE)
    # Gtk.MediaFile is a GTK API rather than a codec claim.  Any load-time
    # backend/codec error is still handled before the view becomes ready.
    if getattr(Gtk, "MediaFile", None) is not None:
        backends.append(BACKEND_GTK_MEDIA)
    return tuple(backends)


class MediaPreviewView(Gtk.Box):
    """A stable media viewport with pointer-friendly playback controls."""

    __gtype_name__ = "KukniMediaPreviewView"

    def __init__(self, picture: Gtk.Widget) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("media-preview")
        self.playing = False
        self.error_message = ""
        self.cleaned_up = False
        self._updating_seek = False
        self._play_pause: Callable[[], None] = lambda: None
        self._seek: Callable[[float], None] = lambda _seconds: None
        self._set_volume: Callable[[float], None] = lambda _volume: None
        self._toggle_mute: Callable[[], None] = lambda: None

        self.display_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=120,
            hexpand=True,
            vexpand=True,
        )
        picture.set_hexpand(True)
        picture.set_vexpand(True)
        self.display_stack.add_named(picture, "video")
        self.display_stack.add_named(self._audio_page(), "audio")
        self.display_stack.set_visible_child_name("video")
        self.display_stack.add_css_class("media-display")
        self.append(self.display_stack)

        controls = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=10,
            margin_bottom=10,
            margin_start=14,
            margin_end=14,
        )
        controls.add_css_class("media-controls")
        self.play_button = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.play_button.set_tooltip_text("Play")
        self.play_button.set_focusable(False)
        self.play_button.connect("clicked", lambda *_args: self._play_pause())
        controls.append(self.play_button)

        self.seek_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            0.0,
            1.0,
            0.1,
        )
        self.seek_scale.set_draw_value(False)
        self.seek_scale.set_hexpand(True)
        self.seek_scale.set_focusable(False)
        self.seek_scale.set_sensitive(False)
        self.seek_scale.connect("value-changed", self._on_seek_changed)
        controls.append(self.seek_scale)

        self.time_label = Gtk.Label(label="0:00 / 0:00")
        self.time_label.add_css_class("numeric")
        controls.append(self.time_label)

        self.mute_button = Gtk.Button(icon_name="audio-volume-high-symbolic")
        self.mute_button.set_tooltip_text("Mute")
        self.mute_button.set_focusable(False)
        self.mute_button.connect("clicked", lambda *_args: self._toggle_mute())
        controls.append(self.mute_button)

        self.volume_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            0.0,
            1.0,
            0.05,
        )
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_value(DEFAULT_VOLUME)
        self.volume_scale.set_size_request(110, -1)
        self.volume_scale.set_focusable(False)
        self.volume_scale.set_tooltip_text("Volume")
        self.volume_scale.connect("value-changed", self._on_volume_changed)
        controls.append(self.volume_scale)
        self.append(controls)

        self.status_label = Gtk.Label(
            label="Paused · playback starts only when requested",
            ellipsize=3,
            margin_bottom=10,
            margin_start=14,
            margin_end=14,
        )
        self.status_label.add_css_class("dim-label")
        self.append(self.status_label)

    @staticmethod
    def _audio_page() -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_halign(Gtk.Align.CENTER)
        page.set_valign(Gtk.Align.CENTER)
        page.append(Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=96))
        title = Gtk.Label(label="Audio preview")
        title.add_css_class("title-2")
        page.append(title)
        detail = Gtk.Label(label="Playback is paused until you press Play")
        detail.add_css_class("dim-label")
        page.append(detail)
        return page

    def bind_controls(
        self,
        *,
        play_pause: Callable[[], None],
        seek: Callable[[float], None],
        set_volume: Callable[[float], None],
        toggle_mute: Callable[[], None],
    ) -> None:
        self._play_pause = play_pause
        self._seek = seek
        self._set_volume = set_volume
        self._toggle_mute = toggle_mute

    def configure_stream(self, *, has_video: bool, has_audio: bool) -> None:
        self.display_stack.set_visible_child_name("video" if has_video else "audio")
        kind = "Video" if has_video else "Audio" if has_audio else "Media"
        self.status_label.set_label(
            f"{kind} paused · playback starts only when requested"
        )

    def update_playback(
        self,
        *,
        position: float,
        duration: float,
        playing: bool,
        seekable: bool,
        muted: bool,
        ended: bool = False,
    ) -> None:
        if self.cleaned_up:
            return
        raw_duration = float(duration)
        raw_position = float(position)
        safe_duration = (
            min(max(0.0, raw_duration), 359_999_999.0)
            if math.isfinite(raw_duration)
            else 0.0
        )
        safe_position = (
            min(max(0.0, raw_position), 359_999_999.0)
            if math.isfinite(raw_position)
            else 0.0
        )
        if safe_duration:
            safe_position = min(safe_position, safe_duration)
        self.playing = bool(playing)
        self._updating_seek = True
        try:
            self.seek_scale.set_range(0.0, max(1.0, safe_duration))
            self.seek_scale.set_value(safe_position)
        finally:
            self._updating_seek = False
        self.seek_scale.set_sensitive(bool(seekable and safe_duration > 0))
        self.time_label.set_label(
            f"{format_media_time(safe_position)} / {format_media_time(safe_duration)}"
        )
        if ended:
            self.play_button.set_icon_name("media-playback-start-symbolic")
            self.play_button.set_tooltip_text("Replay")
            self.status_label.set_label("Playback finished")
        elif self.playing:
            self.play_button.set_icon_name("media-playback-pause-symbolic")
            self.play_button.set_tooltip_text("Pause")
            self.status_label.set_label("Playing")
        else:
            self.play_button.set_icon_name("media-playback-start-symbolic")
            self.play_button.set_tooltip_text("Play")
            if not self.error_message:
                self.status_label.set_label("Paused")
        self.mute_button.set_icon_name(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        )
        self.mute_button.set_tooltip_text("Unmute" if muted else "Mute")

    def set_status(self, message: str) -> None:
        if not self.error_message:
            self.status_label.set_label(message)

    def show_playback_error(self, message: str) -> None:
        self.error_message = message
        self.playing = False
        self.status_label.set_label(message)
        self.status_label.remove_css_class("dim-label")
        self.status_label.add_css_class("error")
        self.play_button.set_icon_name("dialog-warning-symbolic")
        self.set_controls_sensitive(False)

    def set_controls_sensitive(self, sensitive: bool) -> None:
        self.play_button.set_sensitive(sensitive)
        self.mute_button.set_sensitive(sensitive)
        self.volume_scale.set_sensitive(sensitive)
        if not sensitive:
            self.seek_scale.set_sensitive(False)

    def mark_cleaned_up(self) -> None:
        self.cleaned_up = True
        self.playing = False
        self.set_controls_sensitive(False)

    def _on_seek_changed(self, scale: Gtk.Scale) -> None:
        if not self._updating_seek and scale.get_sensitive():
            self._seek(scale.get_value())

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        if not self.cleaned_up:
            self._set_volume(scale.get_value())


class _ControllerBase:
    """Own one backend and guarantee its renderer callback settles once."""

    def __init__(
        self,
        view: MediaPreviewView,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
        on_finished: Callable[[object], None],
        owned_fd: int,
    ) -> None:
        self.view = view
        self._cancellable = cancellable
        self._on_ready = on_ready
        self._on_error = on_error
        self._on_finished = on_finished
        self._owned_fd = owned_fd
        self._ready_sent = False
        self._closed = False
        self._loading_ref_released = False
        self._was_rooted = False
        self._cancel_id = 0
        self._root_id = 0
        self._timeout_id = 0
        self._position_timer_id = 0

    def start_lifetime(self) -> bool:
        if self._cancellable.is_cancelled():
            self._close()
            return False
        self._root_id = self.view.connect("notify::root", self._on_root_changed)
        self._cancel_id = self._cancellable.connect(self._on_cancelled)
        if self._cancellable.is_cancelled():
            GLib.idle_add(self._close)
            return False
        self._timeout_id = GLib.timeout_add_seconds(
            PREPARE_TIMEOUT_SECONDS,
            self._on_prepare_timeout,
        )
        return True

    def become_ready(self, subtitle: str) -> None:
        if self._closed or self._ready_sent:
            return
        if self._cancellable.is_cancelled():
            self._close()
            return
        self._ready_sent = True
        self._remove_timeout()
        self._release_loading_ref()
        # The view owns the controller after the renderer drops its loading ref.
        self.view._media_controller = self
        self._position_timer_id = GLib.timeout_add(250, self._update_position)
        try:
            self._on_ready(self.view, subtitle)
        except Exception:
            self._close()
            raise

    def fail(self, message: str) -> None:
        if self._closed:
            return
        safe_message = _friendly_error(message)
        if self._ready_sent:
            self.view.show_playback_error(safe_message)
            self._close()
            return
        self._release_loading_ref()
        self._close()
        if not self._cancellable.is_cancelled():
            self._on_error(safe_message)

    def _on_prepare_timeout(self) -> bool:
        self._timeout_id = 0
        self.fail("Media preview timed out while preparing the first frame")
        return GLib.SOURCE_REMOVE

    def _on_cancelled(self, *_args) -> None:
        # Gio.Cancellable.disconnect() must not run inside its own callback.
        GLib.idle_add(self._close)

    def _on_root_changed(self, widget: Gtk.Widget, _parameter) -> None:
        if widget.get_root() is not None:
            self._was_rooted = True
        elif self._was_rooted:
            self._close()

    def _remove_timeout(self) -> None:
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0

    def _release_loading_ref(self) -> None:
        if self._loading_ref_released:
            return
        self._loading_ref_released = True
        self._on_finished(self)

    def _close(self, *, mark_view: bool = True) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._closed = True
        self._release_loading_ref()
        self._remove_timeout()
        if self._position_timer_id:
            GLib.source_remove(self._position_timer_id)
            self._position_timer_id = 0
        if self._root_id and self.view.handler_is_connected(self._root_id):
            self.view.disconnect(self._root_id)
            self._root_id = 0
        if self._cancel_id:
            self._cancellable.disconnect(self._cancel_id)
            self._cancel_id = 0
        try:
            self._stop_backend()
        finally:
            if self._owned_fd >= 0:
                os.close(self._owned_fd)
                self._owned_fd = -1
        if mark_view:
            self.view.mark_cleaned_up()
        return GLib.SOURCE_REMOVE

    def _update_position(self) -> bool:
        return GLib.SOURCE_REMOVE

    def _stop_backend(self) -> None:
        raise NotImplementedError


class _GstPlaybinController(_ControllerBase):
    """Direct playbin controller used when gtk4paintablesink is installed."""

    def __init__(
        self,
        uri: str,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
        on_finished: Callable[[object], None],
        owned_fd: int,
    ) -> None:
        if Gst is None or not gtk4_paintable_sink_available():
            raise MediaPreviewError("The GTK 4 GStreamer video sink is unavailable")
        pipeline = Gst.ElementFactory.make("playbin", "kukni-media-player")
        sink = Gst.ElementFactory.make("gtk4paintablesink", "kukni-video-output")
        if pipeline is None or sink is None:
            raise MediaPreviewError("The GStreamer media pipeline could not be created")
        paintable_property = sink.find_property("paintable")
        if paintable_property is None:
            raise MediaPreviewError("The GTK 4 video sink has no paintable output")
        paintable = sink.get_property("paintable")
        if paintable is None:
            raise MediaPreviewError("The GTK 4 video sink could not create a surface")

        picture = Gtk.Picture.new_for_paintable(paintable)
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        view = MediaPreviewView(picture)
        super().__init__(
            view,
            cancellable,
            on_ready,
            on_error,
            on_finished,
            owned_fd,
        )
        self._pipeline = pipeline
        self._sink = sink
        self._bus = pipeline.get_bus()
        self._bus_signal_id = 0
        self._bus_watch = False
        self._ended = False
        self._playing = False
        self._duration = 0.0
        self._seekable = False
        view.bind_controls(
            play_pause=self._play_pause,
            seek=self._seek,
            set_volume=self._set_volume,
            toggle_mute=self._toggle_mute,
        )
        pipeline.set_property("uri", uri)
        pipeline.set_property("video-sink", sink)
        pipeline.set_property("flags", SAFE_PLAY_FLAGS)
        pipeline.set_property("volume", DEFAULT_VOLUME)
        pipeline.set_property("mute", False)

    def start(self) -> None:
        if not self.start_lifetime():
            return
        self._bus.add_signal_watch()
        self._bus_watch = True
        self._bus_signal_id = self._bus.connect("message", self._on_bus_message)
        result = self._pipeline.set_state(Gst.State.PAUSED)
        if result == Gst.StateChangeReturn.FAILURE:
            self.fail("GStreamer could not prepare this media file")

    def _on_bus_message(self, _bus, message) -> None:
        if self._closed:
            return
        message_type = message.type
        if message_type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self.fail(getattr(error, "message", error))
        elif message_type == Gst.MessageType.ASYNC_DONE:
            self._finish_prepare()
        elif message_type == Gst.MessageType.STATE_CHANGED:
            if message.src == self._pipeline:
                _old, new, pending = message.parse_state_changed()
                self._playing = new == Gst.State.PLAYING
                if new == Gst.State.PAUSED and pending == Gst.State.VOID_PENDING:
                    self._finish_prepare()
                self._refresh_view()
        elif message_type == Gst.MessageType.DURATION_CHANGED:
            self._refresh_view()
        elif message_type == Gst.MessageType.BUFFERING:
            percent = message.parse_buffering()
            if percent < 100:
                self.view.set_status(f"Buffering… {percent}%")
        elif message_type == Gst.MessageType.EOS:
            self._ended = True
            self._playing = False
            self._pipeline.set_state(Gst.State.PAUSED)
            self._refresh_view()

    def _finish_prepare(self) -> None:
        if self._ready_sent or self._closed:
            return
        video_count = int(self._pipeline.get_property("n-video") or 0)
        audio_count = int(self._pipeline.get_property("n-audio") or 0)
        if video_count <= 0 and audio_count <= 0:
            self.fail("No playable audio or video stream was found")
            return
        self.view.configure_stream(
            has_video=video_count > 0,
            has_audio=audio_count > 0,
        )
        self._refresh_view()
        kind = "Video" if video_count > 0 else "Audio"
        duration = f" · {format_media_time(self._duration)}" if self._duration else ""
        self.become_ready(f"{kind}{duration} · paused")

    def _refresh_view(self) -> None:
        if self._closed:
            return
        position = 0.0
        position_ok, position_ns = self._pipeline.query_position(Gst.Format.TIME)
        duration_ok, duration_ns = self._pipeline.query_duration(Gst.Format.TIME)
        if position_ok and position_ns >= 0:
            position = position_ns / Gst.SECOND
        if duration_ok and duration_ns > 0:
            self._duration = duration_ns / Gst.SECOND
        query = Gst.Query.new_seeking(Gst.Format.TIME)
        self._seekable = bool(
            self._pipeline.query(query) and query.parse_seeking()[1]
        )
        self.view.update_playback(
            position=position,
            duration=self._duration,
            playing=self._playing,
            seekable=self._seekable,
            muted=bool(self._pipeline.get_property("mute")),
            ended=self._ended,
        )

    def _update_position(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._refresh_view()
        return GLib.SOURCE_CONTINUE

    def _play_pause(self) -> None:
        if self._closed:
            return
        if self._playing:
            self._pipeline.set_state(Gst.State.PAUSED)
            return
        if self._ended:
            self._seek(0.0)
            self._ended = False
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.fail("GStreamer could not start playback")

    def _seek(self, seconds: float) -> None:
        if self._closed or not self._seekable:
            return
        target = max(0, int(seconds * Gst.SECOND))
        if not self._pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            target,
        ):
            self.view.set_status("This stream could not seek to that position")

    def _set_volume(self, volume: float) -> None:
        if not self._closed:
            self._pipeline.set_property("volume", min(max(float(volume), 0.0), 1.0))

    def _toggle_mute(self) -> None:
        if self._closed:
            return
        muted = not bool(self._pipeline.get_property("mute"))
        self._pipeline.set_property("mute", muted)
        self._refresh_view()

    def _stop_backend(self) -> None:
        if self._bus_signal_id and self._bus.handler_is_connected(self._bus_signal_id):
            self._bus.disconnect(self._bus_signal_id)
            self._bus_signal_id = 0
        if self._bus_watch:
            self._bus.remove_signal_watch()
            self._bus_watch = False
        self._pipeline.set_state(Gst.State.NULL)


class _GtkMediaController(_ControllerBase):
    """GTK's native media backend, used where gtk4paintablesink is absent."""

    def __init__(
        self,
        file: Gio.File,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
        on_finished: Callable[[object], None],
        owned_fd: int,
    ) -> None:
        stream = Gtk.MediaFile.new_for_file(file)
        if stream is None:
            raise MediaPreviewError("GTK's media backend is unavailable")
        video = Gtk.Video(media_stream=stream, autoplay=False, loop=False)
        video.set_hexpand(True)
        video.set_vexpand(True)
        view = MediaPreviewView(video)
        super().__init__(
            view,
            cancellable,
            on_ready,
            on_error,
            on_finished,
            owned_fd,
        )
        self._stream = stream
        self._signal_ids: list[int] = []
        view.bind_controls(
            play_pause=self._play_pause,
            seek=self._seek,
            set_volume=self._set_volume,
            toggle_mute=self._toggle_mute,
        )
        stream.set_loop(False)
        stream.set_volume(DEFAULT_VOLUME)
        stream.set_muted(False)
        stream.pause()

    def start(self) -> None:
        if not self.start_lifetime():
            return
        self._signal_ids.extend(
            (
                self._stream.connect("notify::prepared", self._on_stream_changed),
                self._stream.connect("notify::error", self._on_stream_changed),
                self._stream.connect("notify::playing", self._on_stream_changed),
                self._stream.connect("notify::ended", self._on_stream_changed),
                self._stream.connect("notify::seekable", self._on_stream_changed),
            )
        )
        GLib.idle_add(self._inspect_stream)

    def _on_stream_changed(self, *_args) -> None:
        self._inspect_stream()

    def _inspect_stream(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        error = self._stream.get_error()
        if error is not None:
            self.fail(getattr(error, "message", error))
            return GLib.SOURCE_REMOVE
        if self._stream.is_prepared() and not self._ready_sent:
            has_video = self._stream.has_video()
            has_audio = self._stream.has_audio()
            if not has_video and not has_audio:
                self.fail("No playable audio or video stream was found")
                return GLib.SOURCE_REMOVE
            self.view.configure_stream(has_video=has_video, has_audio=has_audio)
            self._refresh_view()
            kind = "Video" if has_video else "Audio"
            duration_seconds = max(0.0, self._stream.get_duration() / 1_000_000)
            duration = (
                f" · {format_media_time(duration_seconds)}"
                if duration_seconds
                else ""
            )
            self.become_ready(f"{kind}{duration} · paused")
        elif self._ready_sent:
            self._refresh_view()
        return GLib.SOURCE_REMOVE

    def _refresh_view(self) -> None:
        if self._closed:
            return
        duration = max(0.0, self._stream.get_duration() / 1_000_000)
        position = max(0.0, self._stream.get_timestamp() / 1_000_000)
        self.view.update_playback(
            position=position,
            duration=duration,
            playing=self._stream.get_playing(),
            seekable=self._stream.is_seekable(),
            muted=self._stream.get_muted(),
            ended=self._stream.get_ended(),
        )

    def _update_position(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._refresh_view()
        return GLib.SOURCE_CONTINUE

    def _play_pause(self) -> None:
        if self._closed:
            return
        if self._stream.get_playing():
            self._stream.pause()
        else:
            if self._stream.get_ended():
                self._stream.seek(0)
            self._stream.play()
        self._refresh_view()

    def _seek(self, seconds: float) -> None:
        if not self._closed and self._stream.is_seekable():
            self._stream.seek(max(0, int(seconds * 1_000_000)))

    def _set_volume(self, volume: float) -> None:
        if not self._closed:
            self._stream.set_volume(min(max(float(volume), 0.0), 1.0))

    def _toggle_mute(self) -> None:
        if self._closed:
            return
        self._stream.set_muted(not self._stream.get_muted())
        self._refresh_view()

    def _stop_backend(self) -> None:
        for signal_id in self._signal_ids:
            if self._stream.handler_is_connected(signal_id):
                self._stream.disconnect(signal_id)
        self._signal_ids.clear()
        self._stream.pause()
        self._stream.clear()


class MediaRenderer:
    """Render common regular local media files without starting playback."""

    id = "media"

    def __init__(self) -> None:
        self._loading_controllers: dict[int, object] = {}

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        if not file.is_native() or info.get_file_type() != Gio.FileType.REGULAR:
            return False
        return supports_media(file.get_basename(), info.get_content_type())

    def render(
        self,
        file: Gio.File,
        _info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        # Renderer callbacks and all GTK/GStreamer object creation occur on the
        # owning main context, even if an extension calls render from elsewhere.
        GLib.idle_add(
            self._begin_render,
            file,
            cancellable,
            on_ready,
            on_error,
        )

    def _begin_render(
        self,
        file: Gio.File,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> bool:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        path = file.get_path() if file.is_native() else None
        try:
            owned_fd, _file_stat = _open_local_regular_file(path)
        except (MediaPreviewError, TypeError) as error:
            on_error(str(error))
            return GLib.SOURCE_REMOVE

        descriptor_path = f"/proc/self/fd/{owned_fd}"
        if not os.path.exists(descriptor_path):
            os.close(owned_fd)
            on_error("This system cannot provide descriptor-backed media access")
            return GLib.SOURCE_REMOVE
        descriptor_file = Gio.File.new_for_path(descriptor_path)

        errors: list[str] = []
        for backend in available_media_backends():
            try:
                if backend == BACKEND_GSTREAMER_PAINTABLE:
                    controller = _GstPlaybinController(
                        descriptor_file.get_uri(),
                        cancellable,
                        on_ready,
                        on_error,
                        self._controller_finished,
                        owned_fd,
                    )
                elif backend == BACKEND_GTK_MEDIA:
                    controller = _GtkMediaController(
                        descriptor_file,
                        cancellable,
                        on_ready,
                        on_error,
                        self._controller_finished,
                        owned_fd,
                    )
                else:
                    continue
            except Exception as error:
                errors.append(str(error))
                continue
            # The controller now owns the descriptor until cancellation,
            # unparenting, a decoder failure, or application close.
            owned_fd = -1
            self._loading_controllers[id(controller)] = controller
            try:
                controller.start()
            except Exception as error:
                self._controller_finished(controller)
                try:
                    controller.fail(str(error))
                except Exception:
                    on_error("The native media backend could not be started")
            return GLib.SOURCE_REMOVE

        if owned_fd >= 0:
            os.close(owned_fd)
        detail = errors[-1] if errors else (
            "No native GTK media backend is installed; install GStreamer's "
            "GTK 4 media support"
        )
        on_error(detail)
        return GLib.SOURCE_REMOVE

    def _controller_finished(self, controller: object) -> None:
        self._loading_controllers.pop(id(controller), None)
