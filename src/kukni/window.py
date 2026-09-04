# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Stable, keyboard-first Kukni preview window."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

try:
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import GdkWayland
except (ImportError, ValueError):  # pragma: no cover - backend dependent
    GdkWayland = None

from .renderers.fallback import FallbackView
from .renderers.registry import RendererRegistry, default_registry
from .session import Direction, PreviewSession, PreviewState, PreviewToken


FILE_ATTRIBUTES = ",".join(
    (
        Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
        Gio.FILE_ATTRIBUTE_STANDARD_CONTENT_TYPE,
        Gio.FILE_ATTRIBUTE_STANDARD_ICON,
        Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
        Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
        Gio.FILE_ATTRIBUTE_TIME_MODIFIED,
        Gio.FILE_ATTRIBUTE_TIME_MODIFIED_USEC,
    )
)


class PreviewWindow(Adw.ApplicationWindow):
    __gtype_name__ = "KukniPreviewWindow"
    __gsignals__ = {
        "navigation-requested": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (str,),
        ),
    }

    def __init__(
        self,
        application: Adw.Application,
        renderer_registry: RendererRegistry | None = None,
    ) -> None:
        super().__init__(
            application=application,
            title="Kukni",
            default_width=1180,
            default_height=760,
        )
        self.set_size_request(680, 440)
        self._session = PreviewSession()
        self._renderer_registry = (
            renderer_registry
            if renderer_registry is not None
            else default_registry()
        )
        self._cancellable: Gio.Cancellable | None = None
        self._current_file: Gio.File | None = None
        self._external_parent_handle = ""

        self._title = Adw.WindowTitle(title="Kukni", subtitle="Quick Look for Linux")
        header = Adw.HeaderBar()
        header.set_title_widget(self._title)
        header.pack_start(self._icon_button("go-previous-symbolic", "win.navigate-left"))
        header.pack_start(self._icon_button("go-next-symbolic", "win.navigate-right"))
        header.pack_end(self._icon_button("document-open-symbolic", "win.choose-file"))
        header.pack_end(self._icon_button("view-fullscreen-symbolic", "win.fullscreen"))

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=140,
            hexpand=True,
            vexpand=True,
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
        self.connect("close-request", self._on_close_request)
        self.connect("realize", lambda *_args: self._apply_external_parent())

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
        self._title.set_title(file.get_basename() or "Untitled")
        self._title.set_subtitle("Inspecting file…")
        self._stack.set_visible_child_name("loading")
        self.present()

        if not file.is_native():
            message = "Remote files are not read until portal-based access is available"
            if self._session.resolve(token, PreviewState.ERROR, message):
                self._show_error(file.get_basename() or "Remote file", message)
            return

        file.query_info_async(
            FILE_ATTRIBUTES,
            Gio.FileQueryInfoFlags.NONE,
            GLib.PRIORITY_DEFAULT,
            self._cancellable,
            lambda source, result: self._on_info_ready(source, result, token),
        )

    def show_empty(self) -> None:
        self._stack.set_visible_child_name("empty")
        self.present()

    def show_toast(self, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast(title=message))

    def set_external_parent_handle(self, handle: str) -> None:
        self._external_parent_handle = handle if len(handle) <= 4096 else ""
        self._apply_external_parent()

    def _apply_external_parent(self) -> None:
        if not self._external_parent_handle or GdkWayland is None:
            return
        surface = self.get_surface()
        if surface is None:
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
                self._show_error(file.get_basename() or "File", error.message)
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
        self._title.set_subtitle(safe_subtitle)
        self._replace_content(widget, "content")

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
            view = FallbackView(file, info, self._cancellable)
        except Exception as error:
            if self._session.resolve(token, PreviewState.ERROR, str(error)):
                self._show_error(
                    info.get_display_name() or file.get_basename() or "File",
                    "File details could not be displayed",
                )
            return
        if not self._session.resolve(token, PreviewState.FALLBACK, detail):
            return
        try:
            self._replace_content(view, "content")
        except Exception:
            self._show_error(
                info.get_display_name() or file.get_basename() or "File",
                "File details could not be displayed",
            )
        if notify:
            self.show_toast("Rich preview unavailable · showing file details")

    def _set_file_title(self, info: Gio.FileInfo) -> None:
        content_type = info.get_content_type()
        description = (
            Gio.content_type_get_description(content_type) if content_type else None
        )
        self._title.set_title(info.get_display_name() or "Untitled")
        self._title.set_subtitle(
            description
            or content_type
            or "Unknown file type"
        )

    def _show_error(self, filename: str, detail: str) -> None:
        page = Adw.StatusPage(
            title="Preview unavailable",
            description=f"{filename}\n{detail}",
            icon_name="dialog-warning-symbolic",
        )
        page.set_vexpand(True)
        self._replace_content(page, "error")
        self._title.set_subtitle("Preview unavailable · use arrows to continue")

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
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = None

    def _request_navigation(self, direction: Direction) -> None:
        try:
            self._session.request_navigation(direction)
        except RuntimeError:
            return
        self.emit("navigation-requested", direction.value)

    def _install_actions(self) -> None:
        self._add_action("choose-file", lambda *_args: self._choose_file())
        self._add_action("close", lambda *_args: self.close())
        self._add_action("fullscreen", lambda *_args: self._toggle_fullscreen())
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
    def _icon_button(icon_name: str, action_name: str) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon_name, action_name=action_name)
        button.add_css_class("flat")
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
