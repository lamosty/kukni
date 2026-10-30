# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Passive media previews: validated pixels and duration, without playback."""

import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from ..media_types import format_media_time, supports_media
from ..media_worker import MediaWorkerCancelled, run_media_worker
from .image_view import ImagePreviewView


# Hold admission until GTK consumes the payload, including when navigation
# cancels a request. Otherwise quick completions could queue unbounded frames.
_SLOT = threading.BoundedSemaphore(1)


class MediaStillRenderer:
    id = "media-still"

    def __init__(self):
        self._pending_id = 0

    def supports(self, file, info):
        return (file.is_native() and info.get_file_type() == Gio.FileType.REGULAR
                and supports_media(file.get_basename(), info.get_content_type()))

    def render(self, file, info, cancellable, on_ready, on_error):
        if self._pending_id:
            GLib.source_remove(self._pending_id)
            self._pending_id = 0
        if cancellable.is_cancelled():
            return
        if not file.is_native() or file.get_path() is None:
            GLib.idle_add(self._error, cancellable, on_error, "Media previews require a local file.")
            return
        if not _SLOT.acquire(blocking=False):
            self._pending_id = GLib.timeout_add(
                25, self._retry, file, info, cancellable, on_ready, on_error,
            )
            return

        def prepare():
            queued = False
            try:
                output = run_media_worker(file.get_path(), cancelled=cancellable.is_cancelled)
                if not cancellable.is_cancelled():
                    queued = bool(GLib.idle_add(
                        self._deliver, cancellable, on_ready, on_error, output,
                    ))
            except MediaWorkerCancelled:
                pass
            except Exception:
                GLib.idle_add(self._error, cancellable, on_error,
                              "Media preview unavailable. The file may be damaged, "
                              "or its codec or required sandbox is unavailable.")
            finally:
                if not queued:
                    _SLOT.release()

        try:
            threading.Thread(target=prepare, name="kukni-media-preview", daemon=True).start()
        except (RuntimeError, OSError):
            _SLOT.release()
            GLib.idle_add(self._error, cancellable, on_error, "The media preview could not start.")

    def _retry(self, *args):
        self._pending_id = 0
        self.render(*args)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _error(cancellable, on_error, message):
        if not cancellable.is_cancelled():
            on_error(message)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _deliver(cancellable, on_ready, on_error, output):
        try:
            if cancellable.is_cancelled():
                return GLib.SOURCE_REMOVE
            result = output.result
            duration = (format_media_time(result.duration_usec / 1_000_000)
                        if result.duration_usec else "Duration unknown")
            if result.has_frame:
                texture = Gdk.MemoryTexture.new(
                    result.width, result.height, Gdk.MemoryFormat.R8G8B8A8,
                    GLib.Bytes.new(output.frame), result.width * 4,
                )
                view = ImagePreviewView(texture, result.width, result.height)
                view.picture.update_property([Gtk.AccessibleProperty.LABEL], ["Video still frame"])
                subtitle = f"Video · {duration} · still frame"
            else:
                view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
                icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
                icon.set_pixel_size(64)
                view.append(icon)
                view.append(Gtk.Label(label=duration))
                subtitle = "Audio · duration preview"
            on_ready(view, subtitle)
        except (GLib.Error, TypeError, ValueError):
            on_error("The media preview could not be displayed.")
        finally:
            _SLOT.release()
        return GLib.SOURCE_REMOVE
