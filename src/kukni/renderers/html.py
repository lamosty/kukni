# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded, network-inert HTML previews backed by WebKitGTK 6."""

from __future__ import annotations

import functools
import html
import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from .base import ErrorCallback, ReadyCallback

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
except (ImportError, ValueError):  # pragma: no cover - depends on the distro
    WebKit = None


MAX_HTML_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
LOAD_TIMEOUT_SECONDS = 10

HTML_CONTENT_TYPES = frozenset(("text/html", "application/xhtml+xml"))
HTML_SUFFIXES = (".html", ".htm", ".xhtml")

# This policy intentionally allows only resources embedded in the document.  It
# is installed as WebKit's default policy and injected as an early meta policy;
# policies are intersected, so a file cannot weaken it with another meta tag.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "base-uri 'none'",
        "connect-src 'none'",
        "font-src data:",
        "form-action 'none'",
        "frame-src 'none'",
        "img-src data:",
        "manifest-src 'none'",
        "media-src data:",
        "object-src 'none'",
        "script-src 'none'",
        "style-src 'unsafe-inline' data:",
        "worker-src 'none'",
        "sandbox",
    )
)

_CSP_META = (
    '<meta charset="utf-8">'
    '<meta http-equiv="Content-Security-Policy" content="'
    f'{html.escape(CONTENT_SECURITY_POLICY, quote=True)}">'
).encode("ascii")

# Keep this declarative so the security posture is easy to test and audit.
LOCKED_DOWN_SETTINGS: dict[str, bool] = {
    "allow-file-access-from-file-urls": False,
    "allow-modal-dialogs": False,
    "allow-top-navigation-to-data-urls": False,
    "allow-universal-access-from-file-urls": False,
    "enable-back-forward-navigation-gestures": False,
    "enable-developer-extras": False,
    "enable-dns-prefetching": False,
    "enable-encrypted-media": False,
    "enable-fullscreen": False,
    "enable-html5-database": False,
    "enable-html5-local-storage": False,
    "enable-javascript": False,
    "enable-javascript-markup": False,
    "enable-media": False,
    "enable-media-capabilities": False,
    "enable-media-stream": False,
    "enable-mediasource": False,
    "enable-offline-web-application-cache": False,
    "enable-page-cache": False,
    "enable-site-specific-quirks": False,
    "enable-webaudio": False,
    "enable-webgl": False,
    "enable-webrtc": False,
    "javascript-can-access-clipboard": False,
    "javascript-can-open-windows-automatically": False,
    "media-playback-allows-inline": False,
    "media-playback-requires-user-gesture": True,
}


class HtmlPreviewError(RuntimeError):
    """A local HTML file could not be previewed within the safety limits."""


class _PreviewCancelled(Exception):
    pass


def user_namespace_policy_allows_sandbox(
    *,
    apparmor_restriction: str | None,
    apparmor_label: str | None,
    unprivileged_userns_clone: str | None,
    max_user_namespaces: str | None,
) -> bool:
    """Evaluate Linux user-namespace policy without attempting an unsafe launch."""

    def is_zero(value: str | None) -> bool:
        if value is None:
            return False
        try:
            return int(value.strip()) == 0
        except ValueError:
            # A present but unreadable policy value is not evidence of safety.
            return True

    if is_zero(unprivileged_userns_clone) or is_zero(max_user_namespaces):
        return False

    if apparmor_restriction is None:
        return True
    try:
        restricted = int(apparmor_restriction.strip()) != 0
    except ValueError:
        return False
    if not restricted:
        return True

    # Ubuntu's AppArmor user-namespace restriction denies an unconfined process.
    # A packaged Kukni profile can explicitly grant the permission; its label is
    # then visible here before WebKit starts a subprocess.
    if apparmor_label is None:
        return False
    label = apparmor_label.strip().casefold()
    return bool(label) and not label.startswith("unconfined")


def _read_kernel_value(path: str) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        return os.read(descriptor, 256).decode("ascii", errors="replace")
    except OSError:
        return None
    finally:
        os.close(descriptor)


def probe_bwrap_user_namespace(bwrap_path: str, true_path: str) -> bool:
    """Probe the exact primitive WebKit needs in an isolated child process."""

    try:
        result = subprocess.run(
            (
                bwrap_path,
                "--unshare-all",
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--",
                true_path,
            ),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@functools.lru_cache(maxsize=1)
def webkit_sandbox_available() -> bool:
    """Return whether WebKit can keep its process sandbox enabled on this host."""

    if WebKit is None:
        return False
    if "WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS" in os.environ:
        return False
    bwrap_path = shutil.which("bwrap")
    true_path = shutil.which("true")
    if bwrap_path is None or true_path is None:
        return False
    policy_allows = user_namespace_policy_allows_sandbox(
        apparmor_restriction=_read_kernel_value(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
        ),
        apparmor_label=_read_kernel_value("/proc/self/attr/current"),
        unprivileged_userns_clone=_read_kernel_value(
            "/proc/sys/kernel/unprivileged_userns_clone"
        ),
        max_user_namespaces=_read_kernel_value(
            "/proc/sys/user/max_user_namespaces"
        ),
    )
    return policy_allows and probe_bwrap_user_namespace(bwrap_path, true_path)


def webkit_runtime_available() -> bool:
    """Return whether the required engine and its mandatory sandbox are usable."""

    return WebKit is not None and webkit_sandbox_available()


def build_safe_document(source: bytes | str) -> bytes:
    """Prefix a restrictive CSP while preserving all source bytes after it."""

    if isinstance(source, str):
        source = source.encode("utf-8", errors="replace")
    elif not isinstance(source, bytes):
        raise TypeError("HTML source must be bytes or text")
    return _CSP_META + source


def build_error_document(message: str) -> bytes:
    """Build a tiny internal error page without interpreting error text."""

    escaped = html.escape(message, quote=True)
    body = (
        '<main style="font: 16px system-ui; padding: 2rem">'
        "<h1>Preview unavailable</h1>"
        f"<p>{escaped}</p>"
        "</main>"
    )
    return build_safe_document(body)


def read_bounded_local_file(
    path: str,
    *,
    limit: int = MAX_HTML_BYTES,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> bytes:
    """Read one regular file from a single descriptor, never past ``limit``."""

    if limit < 0:
        raise ValueError("HTML byte limit must not be negative")
    if is_cancelled():
        raise _PreviewCancelled

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        detail = error.strerror or "I/O error"
        raise HtmlPreviewError(f"Could not open the HTML file: {detail}") from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HtmlPreviewError("HTML preview requires a regular local file")
        if metadata.st_size > limit:
            raise HtmlPreviewError(
                f"HTML preview is limited to {limit // (1024 * 1024)} MiB"
            )

        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            if is_cancelled():
                raise _PreviewCancelled
            try:
                chunk = os.read(
                    descriptor,
                    min(READ_CHUNK_BYTES, limit + 1 - total),
                )
            except OSError as error:
                detail = error.strerror or "I/O error"
                raise HtmlPreviewError(
                    f"Could not read the HTML file: {detail}"
                ) from error
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)

        if total > limit:
            raise HtmlPreviewError(
                f"HTML preview is limited to {limit // (1024 * 1024)} MiB"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def apply_locked_down_settings(settings) -> None:
    """Disable active content, persistence, file access, and risky media APIs."""

    for property_name, value in LOCKED_DOWN_SETTINGS.items():
        if settings.find_property(property_name) is not None:
            settings.set_property(property_name, value)


class HtmlRenderer:
    """Render local HTML without script, file, or network capabilities."""

    id = "html"

    def __init__(self) -> None:
        # A loading WebView is not parented until it succeeds, so retain it here.
        self._loading_views: dict[int, Gtk.Widget] = {}

    def supports(self, file: Gio.File, info: Gio.FileInfo) -> bool:
        if not file.is_native() or info.get_file_type() != Gio.FileType.REGULAR:
            return False
        content_type = info.get_content_type()
        basename = (file.get_basename() or "").casefold()
        generic_type = (
            not content_type
            or content_type in ("inode/x-empty", "application/x-empty")
            or Gio.content_type_is_unknown(content_type)
        )
        is_html = content_type in HTML_CONTENT_TYPES or (
            generic_type and basename.endswith(HTML_SUFFIXES)
        )
        return is_html and webkit_runtime_available()

    def render(
        self,
        file: Gio.File,
        _info: Gio.FileInfo,
        cancellable: Gio.Cancellable,
        on_ready: ReadyCallback,
        on_error: ErrorCallback,
    ) -> None:
        """Read off-thread and resolve on GTK's main context unless cancelled."""

        path = file.get_path() if file.is_native() else None
        if path is None:
            on_error("HTML preview supports local files only")
            return
        if not webkit_runtime_available():
            on_error("Secure WebKitGTK HTML preview is unavailable on this system")
            return
        if cancellable.is_cancelled():
            return

        def worker() -> None:
            try:
                source = read_bounded_local_file(
                    path,
                    is_cancelled=cancellable.is_cancelled,
                )
            except _PreviewCancelled:
                return
            except HtmlPreviewError as error:
                GLib.idle_add(self._deliver_error, cancellable, on_error, str(error))
                return
            GLib.idle_add(
                self._create_web_view,
                source,
                cancellable,
                on_ready,
                on_error,
            )

        threading.Thread(
            target=worker,
            name="kukni-html-reader",
            daemon=True,
        ).start()

    @staticmethod
    def _deliver_error(
        cancellable: Gio.Cancellable,
        on_error,
        message: str,
    ) -> bool:
        if not cancellable.is_cancelled():
            on_error(message)
        return GLib.SOURCE_REMOVE

    def _create_web_view(
        self,
        source: bytes,
        cancellable: Gio.Cancellable,
        on_ready,
        on_error,
    ) -> bool:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        if WebKit is None:
            on_error("WebKitGTK 6 is required for HTML previews")
            return GLib.SOURCE_REMOVE

        try:
            settings = WebKit.Settings()
            apply_locked_down_settings(settings)
            network_session = WebKit.NetworkSession.new_ephemeral()
            view_properties = {
                "settings": settings,
                "network_session": network_session,
                "hexpand": True,
                "vexpand": True,
            }
            if (
                WebKit.WebView.find_property("default-content-security-policy")
                is not None
            ):
                view_properties["default_content_security_policy"] = (
                    CONTENT_SECURITY_POLICY
                )
            view = WebKit.WebView(**view_properties)
            wrapper = Gtk.Stack(
                transition_type=Gtk.StackTransitionType.CROSSFADE,
                transition_duration=120,
                hexpand=True,
                vexpand=True,
            )
            wrapper.add_named(view, "document")
        except Exception:
            on_error("The secure HTML preview engine could not be started")
            return GLib.SOURCE_REMOVE

        view_key = id(view)
        self._loading_views[view_key] = view
        settled = False
        preview_ready = False
        lifetime_cleaned = False
        timeout_id = 0
        settlement_signal_ids: list[int] = []
        lifetime_signal_ids: list[int] = []
        cancel_id = 0

        def clean_up_settlement() -> None:
            nonlocal timeout_id
            if timeout_id:
                GLib.source_remove(timeout_id)
                timeout_id = 0
            for signal_id in settlement_signal_ids:
                if view.handler_is_connected(signal_id):
                    view.disconnect(signal_id)
            settlement_signal_ids.clear()
            self._loading_views.pop(view_key, None)

        def clean_up_lifetime() -> None:
            nonlocal cancel_id, lifetime_cleaned
            if lifetime_cleaned:
                return
            lifetime_cleaned = True
            if cancel_id:
                cancellable.disconnect(cancel_id)
                cancel_id = 0
            for signal_id in lifetime_signal_ids:
                if view.handler_is_connected(signal_id):
                    view.disconnect(signal_id)
            lifetime_signal_ids.clear()

        def terminate_view() -> None:
            view.stop_loading()
            try:
                view.terminate_web_process()
            except Exception:
                pass

        def fail(message: str) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            clean_up_settlement()
            clean_up_lifetime()
            terminate_view()
            if not cancellable.is_cancelled():
                on_error(message)

        def ready() -> None:
            nonlocal preview_ready, settled
            if settled:
                return
            settled = True
            preview_ready = True
            clean_up_settlement()
            if not cancellable.is_cancelled():
                on_ready(wrapper, "HTML document · active content blocked")

        def on_load_changed(_view, event) -> None:
            if event == WebKit.LoadEvent.FINISHED:
                ready()

        def on_load_failed(_view, _event, _uri, _error) -> bool:
            fail("The HTML document could not be rendered safely")
            return True

        def on_decide_policy(_view, decision, decision_type) -> bool:
            if decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                return True
            if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                return False

            action = decision.get_navigation_action()
            navigation_type = action.get_navigation_type()
            uri = action.get_request().get_uri() or ""
            initial_load = (
                not settled
                and navigation_type == WebKit.NavigationType.OTHER
                and uri.split("#", 1)[0] == "about:blank"
            )
            if initial_load:
                return False
            decision.ignore()
            return True

        def on_permission_request(_view, request) -> bool:
            request.deny()
            return True

        def on_context_menu(*_args) -> bool:
            return True

        def on_web_process_terminated(_view, _reason) -> None:
            if cancellable.is_cancelled():
                return
            if not preview_ready:
                fail("The isolated HTML renderer stopped unexpectedly")
                return
            clean_up_lifetime()
            page = self._renderer_stopped_page()
            wrapper.add_named(page, "renderer-stopped")
            wrapper.set_visible_child_name("renderer-stopped")
            wrapper.remove(view)

        def finish_cancellation() -> bool:
            nonlocal settled
            if not settled:
                settled = True
                clean_up_settlement()
            clean_up_lifetime()
            terminate_view()
            return GLib.SOURCE_REMOVE

        def on_cancelled(*_args) -> None:
            # Gio.Cancellable.disconnect() must not run from its own callback.
            GLib.idle_add(finish_cancellation)

        def on_timeout() -> bool:
            nonlocal timeout_id
            timeout_id = 0
            fail("HTML preview timed out")
            return GLib.SOURCE_REMOVE

        settlement_signal_ids.extend(
            (
                view.connect("load-changed", on_load_changed),
                view.connect("load-failed", on_load_failed),
            )
        )
        lifetime_signal_ids.extend(
            (
                view.connect("decide-policy", on_decide_policy),
                view.connect("permission-request", on_permission_request),
                view.connect("context-menu", on_context_menu),
                view.connect("web-process-terminated", on_web_process_terminated),
            )
        )
        cancel_id = cancellable.connect(on_cancelled)
        if cancellable.is_cancelled():
            finish_cancellation()
            return GLib.SOURCE_REMOVE
        timeout_id = GLib.timeout_add_seconds(LOAD_TIMEOUT_SECONDS, on_timeout)

        document = build_safe_document(source)
        try:
            view.load_bytes(GLib.Bytes.new(document), "text/html", "UTF-8", None)
        except Exception:
            fail("The HTML document could not be loaded safely")
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _renderer_stopped_page() -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_halign(Gtk.Align.CENTER)
        page.set_valign(Gtk.Align.CENTER)
        page.set_margin_top(32)
        page.set_margin_bottom(32)
        page.set_margin_start(32)
        page.set_margin_end(32)

        icon = Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=56)
        page.append(icon)
        title = Gtk.Label(label="HTML preview stopped")
        title.add_css_class("title-2")
        page.append(title)
        detail = Gtk.Label(
            label="The isolated renderer exited. Use the arrow keys to keep browsing.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        detail.add_css_class("dim-label")
        page.append(detail)
        return page
