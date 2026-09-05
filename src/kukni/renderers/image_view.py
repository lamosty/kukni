# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""A bounded native zoom canvas shared by photographs and rendered PDF pages."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk


class _ZoomPicture(Gtk.Picture):
    # @why Gtk.Picture's intrinsic natural size otherwise wins over a smaller
    # size request when room is available. Exact min/natural measures keep the
    # displayed zoom percentage truthful without resampling another texture.
    def do_measure(self, orientation, _for_size):
        requested = self.get_size_request()
        size = requested[0 if orientation == Gtk.Orientation.HORIZONTAL else 1]
        size = max(0, size)
        return size, size, -1, -1


class ImagePreviewView(Gtk.Box):
    def __init__(self, texture: Gdk.Texture, source_width: int, source_height: int):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        self.add_css_class("image-preview")
        self.zoom = 1.0
        self.fit_mode = True
        self._viewport_size = (0, 0)
        self._pending_center = None
        self._pan_origin = (0.0, 0.0)
        self._pan_active = False
        self._tick_id = 0
        self.picture = _ZoomPicture(
            content_fit=Gtk.ContentFit.CONTAIN,
            can_shrink=True,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            focusable=False,
        )
        self.picture.update_property([Gtk.AccessibleProperty.LABEL], ["Image preview"])
        canvas = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        canvas.set_valign(Gtk.Align.CENTER)
        canvas.append(self.picture)
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, focusable=False)
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(canvas)
        self.append(self.scroller)
        for adjustment in (self.scroller.get_hadjustment(), self.scroller.get_vadjustment()):
            adjustment.connect("changed", lambda *_args: self._queue_layout())

        # @decision Keep zoom in the content, not the main title bar. PDF adds
        # its page controls to this same toolbar instead of stacking toolbars.
        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.toolbar.add_css_class("preview-controls")
        self.toolbar.set_halign(Gtk.Align.CENTER)
        self.toolbar.append(self._button("zoom-out-symbolic", "Zoom out (−)", self.zoom_out))
        self.zoom_label = Gtk.Label(width_chars=5)
        self.zoom_label.add_css_class("caption")
        self.zoom_label.add_css_class("numeric")
        self.toolbar.append(self.zoom_label)
        self.toolbar.append(self._button("zoom-in-symbolic", "Zoom in (+)", self.zoom_in))
        self.fit_button = Gtk.Button(label="Fit", focus_on_click=False)
        self.fit_button.add_css_class("flat")
        self.fit_button.set_tooltip_text("Fit preview to window (0)")
        self.fit_button.connect("clicked", lambda *_args: self.fit())
        self.toolbar.append(self.fit_button)
        self.actual_button = Gtk.Button(label="1:1", focus_on_click=False)
        self.actual_button.add_css_class("flat")
        self.actual_button.connect("clicked", lambda *_args: self.actual_size())
        self.toolbar.append(self.actual_button)
        self.append(self.toolbar)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self.scroller.add_controller(scroll)
        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.scroller.add_controller(drag)
        self.set_texture(texture, source_width, source_height)
        self.connect("map", lambda *_args: self._queue_layout())

    def set_texture(
        self,
        texture: Gdk.Texture,
        source_width: int | None = None,
        source_height: int | None = None,
    ) -> None:
        """Replace bounded retained pixels; changing a PDF page starts in Fit."""
        if not (0 < texture.get_width() <= 4096 and 0 < texture.get_height() <= 4096):
            raise ValueError("Invalid image dimensions")
        self.texture = texture
        self.source_width = source_width or texture.get_width()
        self.source_height = source_height or texture.get_height()
        kind = getattr(self, "preview_geometry", ("image",))[0]
        self.preview_geometry = (kind, texture.get_width(), texture.get_height())
        self.picture.set_paintable(texture)
        retained = f"{texture.get_width()} × {texture.get_height()}"
        description = f"1:1 retained preview pixels ({retained}); one pixel per logical display unit (1)"
        if (self.source_width, self.source_height) != (texture.get_width(), texture.get_height()):
            description += f". Downscaled from {self.source_width} × {self.source_height}; not full-source detail"
        self.actual_button.set_tooltip_text(description)
        self.actual_button.update_property([Gtk.AccessibleProperty.LABEL], ["Actual preview pixel size"])
        self.fit()

    def fit(self) -> None:
        self.fit_mode = True
        self._pending_center = (.5, .5)
        self._update_fit()

    # @constraint 1:1 means the retained preview pixels, not the original file
    # and not physical monitor pixels on HiDPI. Zoom never decodes, allocates a
    # new texture, or enlarges the toplevel; it only changes native allocation.
    def actual_size(self) -> None:
        self._set_zoom(1.0)

    def zoom_in(self) -> None:
        self._set_zoom(self.zoom * 1.25)

    def zoom_out(self) -> None:
        self._set_zoom(self.zoom / 1.25)

    def _set_zoom(self, zoom: float) -> None:
        self.fit_mode = False
        self._pending_center = tuple(
            (adjustment.get_value() + adjustment.get_page_size() / 2) / max(1, adjustment.get_upper())
            for adjustment in (self.scroller.get_hadjustment(), self.scroller.get_vadjustment())
        )
        self.zoom = max(.05, min(8.0, zoom))
        self._apply_zoom()

    def _update_fit(self) -> None:
        width, height = self.scroller.get_width(), self.scroller.get_height()
        if width <= 1 or height <= 1:
            return
        self.zoom = min(1.0, max(1, width - 24) / self.texture.get_width(),
                        max(1, height - 24) / self.texture.get_height())
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        self.picture.set_size_request(max(1, round(self.texture.get_width() * self.zoom)),
                                      max(1, round(self.texture.get_height() * self.zoom)))
        self.zoom_label.set_label(f"{round(self.zoom * 100)}%")
        self.zoom_label.set_tooltip_text("Scale relative to retained preview pixels")
        self.fit_button.set_sensitive(not self.fit_mode)
        self.scroller.set_cursor_from_name("default" if self.fit_mode else "grab")
        self._queue_layout()

    # @why Observe native adjustment changes and only tick while an allocation
    # is pending. An idle photograph must not request an endless 60 Hz redraw.
    def _queue_layout(self) -> None:
        if not self._tick_id:
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _widget, _clock) -> bool:
        viewport = (self.scroller.get_width(), self.scroller.get_height())
        if viewport != self._viewport_size:
            self._viewport_size = viewport
            if self.fit_mode:
                self._update_fit()
        if self._pending_center is not None:
            requested = self.picture.get_size_request()
            if self.picture.get_width() == requested[0] and self.picture.get_height() == requested[1]:
                for adjustment, fraction in zip(
                    (self.scroller.get_hadjustment(), self.scroller.get_vadjustment()),
                    self._pending_center,
                ):
                    adjustment.set_value(fraction * adjustment.get_upper() - adjustment.get_page_size() / 2)
                self._pending_center = None
        if self._pending_center is not None:
            return True
        self._tick_id = 0
        return False

    def _on_scroll(self, controller, _dx, dy) -> bool:
        if not controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            return False
        if dy < 0:
            self.zoom_in()
        elif dy > 0:
            self.zoom_out()
        return True

    def _on_drag_begin(self, gesture, _x, _y) -> None:
        adjustments = (self.scroller.get_hadjustment(), self.scroller.get_vadjustment())
        self._pan_active = any(a.get_upper() > a.get_page_size() for a in adjustments)
        if self._pan_active:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._pan_origin = tuple(a.get_value() for a in adjustments)
            self.scroller.set_cursor_from_name("grabbing")

    def _on_drag_update(self, _gesture, dx, dy) -> None:
        if self._pan_active:
            self.pan_to(self._pan_origin[0] - dx, self._pan_origin[1] - dy)

    def pan_to(self, x: float, y: float) -> None:
        """Move inside the retained canvas; GTK clamps at its scroll bounds."""
        self._pending_center = None
        self.scroller.get_hadjustment().set_value(x)
        self.scroller.get_vadjustment().set_value(y)

    def _on_drag_end(self, _gesture, _dx, _dy) -> None:
        self._pan_active = False
        self.scroller.set_cursor_from_name("default" if self.fit_mode else "grab")

    @staticmethod
    def _button(icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon, focus_on_click=False)
        button.add_css_class("flat")
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        button.connect("clicked", lambda *_args: callback())
        return button
