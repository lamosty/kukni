# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""A content-first canvas shared by ordinary photographs and camera previews."""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk


class ImagePreviewView(Gtk.Box):
    def __init__(self, texture: Gdk.Texture, source_width: int, source_height: int):
        if not (0 < texture.get_width() <= 4096 and 0 < texture.get_height() <= 4096):
            raise ValueError("Invalid image dimensions")
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        self.add_css_class("image-preview")
        self.texture = texture
        self.source_width = source_width
        self.source_height = source_height
        self.picture = Gtk.Picture(
            paintable=texture,
            content_fit=Gtk.ContentFit.SCALE_DOWN,
            can_shrink=True,
            hexpand=True,
            vexpand=True,
            focusable=False,
        )
        self.append(self.picture)
