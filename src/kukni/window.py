# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Stable, keyboard-first Kukni preview window."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

try:
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import GdkWayland
except (ImportError, ValueError):  # pragma: no cover - backend dependent
    GdkWayland = None

from .geometry import AdaptiveSizing, Size, preferred_window_size
from .renderers.fallback import FallbackView
from .renderers.image_view import ImagePreviewView
from .renderers.registry import RendererRegistry, default_registry
from .renderers.text import TextPreviewView, sanitize_display_label
from .session import Direction, PreviewSession, PreviewState, PreviewToken


FILE_ATTRIBUTES = ",".join(
    (
        Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
        Gio.FILE_ATTRIBUTE_STANDARD_CONTENT_TYPE,
        Gio.FILE_ATTRIBUTE_STANDARD_ICON,
        Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
        Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
        Gio.FILE_ATTRIBUTE_ACCESS_CAN_EXECUTE,
        Gio.FILE_ATTRIBUTE_TIME_MODIFIED,
        Gio.FILE_ATTRIBUTE_TIME_MODIFIED_USEC,
    )
)
OPENING_TIMEOUT_SECONDS = 12


class PreviewWindow(Adw.ApplicationWindow):
    __gtype_name__ = "KukniPreviewWindow"
    __gsignals__ = {
        "navigation-requested": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (str,),
        ),
        "file-chosen": (GObject.SignalFlags.RUN_LAST, None, (Gio.File,)),
    }

    def __init__(
        self,
        application: Adw.Application,
        renderer_registry: RendererRegistry | None = None,
        *,
        opening_timeout_seconds: int = OPENING_TIMEOUT_SECONDS,
    ) -> None:
        opening_timeout_seconds = int(opening_timeout_seconds)
        if opening_timeout_seconds <= 0:
            raise ValueError("opening timeout must be positive")
        super().__init__(
            application=application,
            title="Kukni",
            default_width=640,
            default_height=480,
        )
        self.set_size_request(320, 240)
        self._session = PreviewSession()
        self._renderer_registry = (
            renderer_registry
            if renderer_registry is not None
            else default_registry()
        )
        self._cancellable: Gio.Cancellable | None = None
        self._opening_timeout_id = 0
        self._opening_timeout_seconds = opening_timeout_seconds
        self._current_file: Gio.File | None = None
        self._external_parent_handle = ""
        self._navigation_available = False
        self._sizing = AdaptiveSizing()
        self._resize_timeout_id = 0
        self._current_info: Gio.FileInfo | None = None
        self._preview_detail = ""

        self._title = Adw.WindowTitle(title="Kukni")
        header = Adw.HeaderBar()
        header.set_decoration_layout(":close")
        header.set_title_widget(self._title)
        self._navigation_buttons = (
            self._icon_button("go-previous-symbolic", "win.navigate-left", "Previous file (Left)"),
            self._icon_button("go-next-symbolic", "win.navigate-right", "Next file (Right)"),
        )
        for button in self._navigation_buttons:
            header.pack_start(button)
        header.pack_end(self._icon_button("document-open-symbolic", "win.choose-file", "Choose file (Ctrl+O)"))
        header.pack_end(self._icon_button("view-fullscreen-symbolic", "win.fullscreen", "Fullscreen (F)"))
        self._info_label = Gtk.Label(wrap=True, max_width_chars=38,
                                     xalign=0, margin_top=16, margin_bottom=16,
                                     margin_start=16, margin_end=16)
        self._info_popover = Gtk.Popover()
        self._info_popover.set_child(self._info_label)
        self._info_popover.connect("show", lambda *_args: self._update_info(self._stack.get_visible_child()))
        info_keys = Gtk.EventControllerKey()
        info_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        info_keys.connect("key-pressed", self._on_info_key)
        self._info_popover.add_controller(info_keys)
        self._info_button = Gtk.MenuButton(label="Info", popover=self._info_popover)
        self._info_button.add_css_class("flat")
        self._info_button.set_tooltip_text("File information (Ctrl+I)")
        self._info_button.update_property([Gtk.AccessibleProperty.LABEL], ["File information"])
        header.pack_end(self._info_button)

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=140,
            hexpand=True,
            vexpand=True,
            hhomogeneous=False,
            vhomogeneous=False,
        )
        self._stack.add_named(self._empty_page(), "empty")
        self._stack.add_named(self._loading_page(), "loading")

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._stack)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._toast_overlay)
        self.set_content(toolbar_view)

        self._install_actions()
        self.set_navigation_available(False)
        self.connect("close-request", self._on_close_request)
        self.connect("realize", self._on_realize)

    @property
    def session(self) -> PreviewSession:
        return self._session

    def show_file(
        self,
        file: Gio.File,
        *,
        close_if_already_shown: bool = False,
    ) -> None:
        token = self._session.show(
            file.get_uri(),
            close_if_already_shown=close_if_already_shown,
        )
        if token is None:
            self.close()
            return

        self._cancel_current_work()
        self._cancellable = Gio.Cancellable()
        self._current_file = file
        self._current_info = None
        self._preview_detail = "Preparing preview…"
        self._info_popover.popdown()
        self._update_info()
        self._set_zoom_available(False)
        self._title.set_title(sanitize_display_label(file.get_basename()))
        self._title.set_subtitle("")
        self._stack.set_visible_child_name("loading")
        self.present()
        self._start_opening_timeout(
            token,
            sanitize_display_label(file.get_basename(), "File"),
        )

        if not file.is_native():
            message = "Remote files are not read until portal-based access is available"
            if self._session.resolve(token, PreviewState.ERROR, message):
                self._clear_opening_timeout()
                self._show_error(
                    sanitize_display_label(file.get_basename(), "Remote file"),
                    message,
                )
            return

        file.query_info_async(
            FILE_ATTRIBUTES,
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            lambda source, result: self._on_info_ready(source, result, token),
        )

    def show_empty(self) -> None:
        self._cancel_current_work()
        self._session.close()
        self._current_file = None
        self._current_info = None
        self._preview_detail = ""
        self._title.set_title("Kukni")
        self._title.set_subtitle("")
        self._info_popover.popdown()
        self._update_info()
        self.set_navigation_available(False)
        self._set_zoom_available(False)
        self._stack.set_visible_child_name("empty")
        self.present()

    def show_toast(self, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast(title=message))

    def set_navigation_available(self, available: bool) -> None:
        """Expose arrows only while an external file manager owns selection."""
        self._navigation_available = bool(available)
        for direction in Direction:
            self.lookup_action(f"navigate-{direction.value}").set_enabled(available)
        for button in self._navigation_buttons:
            button.set_visible(available)

    def _set_zoom_available(self, available: bool) -> None:
        for name in ("zoom-in", "zoom-out", "fit", "actual-size"):
            self.lookup_action(name).set_enabled(available)
        page_available = available and callable(getattr(self._stack.get_visible_child(), "change_page", None))
        for name in ("page-previous", "page-next"):
            self.lookup_action(name).set_enabled(page_available)

    def _image_action(self, method: str, *args) -> None:
        widget = self._stack.get_visible_child()
        if isinstance(widget, ImagePreviewView) and callable(getattr(widget, method, None)):
            getattr(widget, method)(*args)

    def _toggle_info(self) -> None:
        if self._info_popover.get_visible():
            self._info_popover.popdown()
        else:
            self._update_info(self._stack.get_visible_child())
            self._info_popover.popup()

    # @why A native popover has its own grab and may consume application
    # accelerators. Preserve close/navigation and the Info toggle even while
    # that surface has focus; its label is deliberately not an editor.
    def _on_info_key(self, _controller, keyval, _keycode, state) -> bool:
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_space):
            self.close()
            return True
        if keyval in (Gdk.KEY_i, Gdk.KEY_I) and state & Gdk.ModifierType.CONTROL_MASK:
            self._info_popover.popdown()
            return True
        direction = {Gdk.KEY_Left: Direction.LEFT, Gdk.KEY_Right: Direction.RIGHT,
                     Gdk.KEY_Up: Direction.UP, Gdk.KEY_Down: Direction.DOWN}.get(keyval)
        if direction is not None and self._navigation_available:
            self._request_navigation(direction)
            return True
        return False

    def _update_info(self, widget: Gtk.Widget | None = None) -> None:
        # @constraint Only already-queried file metadata goes here. Do not open
        # the source or add EXIF/GPS parsing to the UI thread for an Info panel.
        lines = []
        if self._current_info is not None:
            info = self._current_info
            kind = info.get_content_type()
            lines.append(Gio.content_type_get_description(kind) if kind else "File")
            if info.get_file_type() == Gio.FileType.REGULAR:
                lines.append(GLib.format_size(info.get_size()))
        pdf_page = getattr(widget, "page_number", None)
        if isinstance(widget, ImagePreviewView):
            if pdf_page is None:
                lines.append(f"Source: {widget.source_width} × {widget.source_height}")
            lines.append(f"Preview: {widget.texture.get_width()} × {widget.texture.get_height()} pixels")
        if pdf_page is not None:
            total = getattr(widget, "page_count", None)
            lines.append(f"Page {pdf_page} of {total}" if total else f"Page {pdf_page}")
        elif self._preview_detail:
            lines.append(sanitize_display_label(self._preview_detail)[:512])
        self._info_label.set_label("\n".join(lines) or "Choose a file to inspect its information.")

    def _monitor_size(self) -> Size:
        display = self.get_display()
        surface = self.get_surface()
        monitor = display.get_monitor_at_surface(surface) if surface else None
        if monitor is None:
            monitors = display.get_monitors()
            monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is not None:
            bounds = monitor.get_geometry()
            return Size(max(1, bounds.width), max(1, bounds.height))
        return Size(1280, 800)

    def _schedule_content_size(self, widget: Gtk.Widget, info: Gio.FileInfo | None) -> None:
        # @decision Resize only once a current result has arrived. Coalesce
        # fast selections, never resize for a loading state, and retain this
        # toplevel/parent. Exact position is intentionally compositor-owned.
        if self._resize_timeout_id:
            GLib.source_remove(self._resize_timeout_id)
        geometry = getattr(widget, "preview_geometry", None)
        if geometry is None:
            kind = "fallback"
            if isinstance(widget, TextPreviewView):
                kind = "text"
            elif widget.has_css_class("media-preview"):
                content_type = info.get_content_type() if info else ""
                kind = "audio" if (content_type or "").startswith("audio/") else "video"
            elif not isinstance(widget, (FallbackView, Adw.StatusPage)):
                kind = "document"
            geometry = (kind, 0, 0)
        generation = self._session.snapshot.generation

        def apply() -> bool:
            self._resize_timeout_id = 0
            if generation != self._session.snapshot.generation:
                return GLib.SOURCE_REMOVE
            if self.is_fullscreen() or self.is_maximized():
                return GLib.SOURCE_REMOVE
            wanted = preferred_window_size(geometry[0], self._monitor_size(), *geometry[1:])
            if self._sizing.request(wanted, GLib.get_monotonic_time() / 1_000_000):
                self.set_default_size(wanted.width, wanted.height)
            return GLib.SOURCE_REMOVE

        self._resize_timeout_id = GLib.timeout_add(140, apply)

    def _on_realize(self, _widget) -> None:
        self._apply_external_parent()
        surface = self.get_surface()
        # Native resize notifications avoid polling an otherwise idle window.
        surface.connect("notify::width", self._observe_window_size)
        surface.connect("notify::height", self._observe_window_size)
        self._observe_window_size(surface, None)

    def _observe_window_size(self, surface, _property) -> None:
        width, height = surface.get_width(), surface.get_height()
        # A newly realized native surface starts at 1×1 before first layout;
        # that transition is not a manual resize.
        if width > 1 and height > 1:
            if self.is_fullscreen() or self.is_maximized():
                self._sizing.manual = True
            self._sizing.observe(Size(width, height), GLib.get_monotonic_time() / 1_000_000)

    def set_external_parent_handle(self, handle: str) -> None:
        self._external_parent_handle = handle if len(handle) <= 4096 else ""
        self._apply_external_parent()

    def _apply_external_parent(self) -> None:
        surface = self.get_surface()
        if surface is None:
            return
        # Clear the native relationship as well as the stored handle when a
        # Nautilus session ends; do not keep a stale parent on a direct open.
        if isinstance(surface, Gdk.Toplevel):
            surface.set_property("transient-for", None)
        if not self._external_parent_handle or GdkWayland is None:
            return
        prefix = "wayland:"
        if (
            self._external_parent_handle.startswith(prefix)
            and isinstance(surface, GdkWayland.WaylandToplevel)
        ):
            surface.set_transient_for_exported(
                self._external_parent_handle[len(prefix) :]
            )

    def _on_info_ready(
        self,
        file: Gio.File,
        result: Gio.AsyncResult,
        token: PreviewToken,
    ) -> None:
        try:
            info = file.query_info_finish(result)
        except GLib.Error as error:
            if (
                error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
                or not self._is_current(token)
            ):
                return
            if self._session.resolve(token, PreviewState.ERROR, error.message):
                self._clear_opening_timeout()
                self._show_error(
                    sanitize_display_label(file.get_basename(), "File"),
                    error.message,
                )
            return

        if not self._is_current(token):
            return
        self._set_file_title(info)
        try:
            renderer = self._renderer_registry.select(file, info)
        except Exception as error:
            self._show_fallback(file, info, token, str(error), notify=True)
            return

        if renderer is None:
            self._show_fallback(file, info, token, "No rich renderer selected")
            return

        try:
            renderer.render(
                file,
                info,
                self._cancellable,
                lambda widget, subtitle: self._on_renderer_ready(
                    file,
                    info,
                    token,
                    widget,
                    subtitle,
                ),
                lambda message: self._on_renderer_error(
                    file,
                    info,
                    token,
                    message,
                ),
            )
        except Exception as error:
            self._on_renderer_error(file, info, token, str(error))

    def _on_renderer_ready(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        token: PreviewToken,
        widget: Gtk.Widget,
        subtitle: str,
    ) -> None:
        if not self._is_current(token):
            return
        if not isinstance(widget, Gtk.Widget) or widget.get_parent() is not None:
            self._show_fallback(
                file,
                info,
                token,
                "Renderer returned an invalid view",
                notify=True,
            )
            return
        safe_subtitle = subtitle.strip() if isinstance(subtitle, str) else ""
        safe_subtitle = safe_subtitle or "Preview"
        if not self._session.resolve(token, PreviewState.PREVIEW, safe_subtitle):
            return
        self._clear_opening_timeout()
        self._preview_detail = safe_subtitle
        self._update_info(widget)
        self._replace_content(widget, "content")
        self._set_zoom_available(isinstance(widget, ImagePreviewView))
        self._schedule_content_size(widget, info)

    def _on_renderer_error(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        token: PreviewToken,
        message: str,
    ) -> None:
        self._show_fallback(file, info, token, message, notify=True)

    def _show_fallback(
        self,
        file: Gio.File,
        info: Gio.FileInfo,
        token: PreviewToken,
        detail: str,
        *,
        notify: bool = False,
    ) -> None:
        if not self._is_current(token):
            return
        try:
            view = FallbackView(
                file, info, self._cancellable, detail=detail if notify else ""
            )
        except Exception as error:
            if self._session.resolve(token, PreviewState.ERROR, str(error)):
                self._clear_opening_timeout()
                self._show_error(
                    info.get_display_name() or file.get_basename() or "File",
                    "File details could not be displayed",
                )
            return
        if not self._session.resolve(token, PreviewState.FALLBACK, detail):
            return
        self._clear_opening_timeout()
        try:
            self._preview_detail = detail if notify else "Preview unavailable"
            self._update_info()
            self._replace_content(view, "content")
            self._schedule_content_size(view, info)
        except Exception:
            self._show_error(
                info.get_display_name() or file.get_basename() or "File",
                "File details could not be displayed",
            )

    def _set_file_title(self, info: Gio.FileInfo) -> None:
        self._current_info = info
        self._title.set_title(sanitize_display_label(info.get_display_name()))
        self._title.set_subtitle("")
        self._update_info()

    def _show_error(self, filename: str, detail: str) -> None:
        page = Adw.StatusPage(
            title="Preview unavailable",
            description=f"{filename}\n{detail}",
            icon_name="dialog-warning-symbolic",
        )
        page.set_vexpand(True)
        self._replace_content(page, "error")
        self._preview_detail = detail
        self._update_info()
        self._schedule_content_size(page, None)

    def _replace_content(self, widget: Gtk.Widget, name: str) -> None:
        previous = self._stack.get_child_by_name(name)
        if previous is not None:
            self._stack.remove(previous)
        self._stack.add_named(widget, name)
        self._stack.set_visible_child_name(name)

    def _is_current(self, token: PreviewToken) -> bool:
        snapshot = self._session.snapshot
        return (
            snapshot.generation == token.generation
            and snapshot.current_uri == token.uri
            and snapshot.state is PreviewState.OPENING
        )

    def _cancel_current_work(self) -> None:
        self._clear_opening_timeout()
        if self._resize_timeout_id:
            GLib.source_remove(self._resize_timeout_id)
            self._resize_timeout_id = 0
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = None

    def _start_opening_timeout(
        self,
        token: PreviewToken,
        filename: str,
    ) -> None:
        self._clear_opening_timeout()
        self._opening_timeout_id = GLib.timeout_add_seconds(
            self._opening_timeout_seconds,
            self._on_opening_timeout,
            token,
            filename,
        )

    def _clear_opening_timeout(self) -> None:
        if self._opening_timeout_id:
            GLib.source_remove(self._opening_timeout_id)
            self._opening_timeout_id = 0

    def _on_opening_timeout(
        self,
        token: PreviewToken,
        filename: str,
    ) -> bool:
        self._opening_timeout_id = 0
        if not self._is_current(token):
            return GLib.SOURCE_REMOVE
        if self._cancellable is not None:
            self._cancellable.cancel()
        unit = "second" if self._opening_timeout_seconds == 1 else "seconds"
        message = (
            f"Preview preparation exceeded {self._opening_timeout_seconds} {unit} "
            "and was stopped"
        )
        if self._session.resolve(token, PreviewState.ERROR, message):
            self._show_error(filename, message)
            self.show_toast("Preview stopped safely · use arrows to continue")
        return GLib.SOURCE_REMOVE

    def _request_navigation(self, direction: Direction) -> None:
        if not self._navigation_available:
            return
        try:
            self._session.request_navigation(direction)
        except RuntimeError:
            return
        self.emit("navigation-requested", direction.value)

    def _install_actions(self) -> None:
        self._add_action("choose-file", lambda *_args: self._choose_file())
        self._add_action("close", lambda *_args: self.close())
        self._add_action("fullscreen", lambda *_args: self._toggle_fullscreen())
        self._add_action("info", lambda *_args: self._toggle_info())
        for action, method in (("zoom-in", "zoom_in"), ("zoom-out", "zoom_out"),
                               ("fit", "fit"), ("actual-size", "actual_size")):
            self._add_action(action, lambda _a, _p, name=method: self._image_action(name))
        self._add_action("page-previous", lambda *_args: self._image_action("change_page", -1))
        self._add_action("page-next", lambda *_args: self._image_action("change_page", 1))
        self._set_zoom_available(False)
        for direction in Direction:
            self._add_action(
                f"navigate-{direction.value}",
                lambda _action, _parameter, value=direction: self._request_navigation(
                    value
                ),
            )

        accelerators = {
            "win.close": ("Escape", "space"),
            "win.fullscreen": ("f", "F11"),
            "win.choose-file": ("<Primary>o",),
            "win.navigate-left": ("Left",),
            "win.navigate-right": ("Right",),
            "win.navigate-up": ("Up",),
            "win.navigate-down": ("Down",),
            "win.info": ("<Primary>i",),
            "win.zoom-in": ("plus", "equal", "KP_Add"),
            "win.zoom-out": ("minus", "KP_Subtract"),
            "win.fit": ("0", "KP_0"),
            "win.actual-size": ("1", "KP_1"),
            "win.page-previous": ("Page_Up",),
            "win.page-next": ("Page_Down",),
        }
        for action, keys in accelerators.items():
            self.get_application().set_accels_for_action(action, keys)

    def _add_action(self, name: str, callback) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def _choose_file(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a file to preview", modal=True)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self._toast_overlay.add_toast(Adw.Toast(title=error.message))
            return
        self.emit("file-chosen", file)
        self.show_file(file)

    def _toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self._cancel_current_work()
        self._session.close()
        return False

    @staticmethod
    def _icon_button(icon_name: str, action_name: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon_name, action_name=action_name, focus_on_click=False)
        button.add_css_class("flat")
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        return button

    @staticmethod
    def _empty_page() -> Gtk.Widget:
        page = Adw.StatusPage(
            title="Kukni",
            description="Choose a file, or invoke Kukni from your file manager.",
            icon_name="document-open-symbolic",
        )
        page.set_vexpand(True)
        button = Gtk.Button(label="Choose File", action_name="win.choose-file")
        button.add_css_class("suggested-action")
        page.set_child(button)
        return page

    @staticmethod
    def _loading_page() -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        box.append(spinner)
        label = Gtk.Label(label="Preparing preview…")
        label.add_css_class("dim-label")
        box.append(label)
        return box
