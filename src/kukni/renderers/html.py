# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded, network-inert HTML previews backed by WebKitGTK 6."""

from __future__ import annotations

import functools
import html
import os
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

# Keep the original HTML-module import path while the shared worker module owns
# the implementation.
from ..worker import probe_bwrap_user_namespace
from .base import ErrorCallback, ReadyCallback

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit
except (ImportError, ValueError):  # pragma: no cover - depends on the distro
    WebKit = None


MAX_HTML_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
LOAD_TIMEOUT_SECONDS = 5
MAX_SANITIZED_HTML_CHARACTERS = MAX_HTML_BYTES * 2
SANITIZE_TIMEOUT_SECONDS = 2.0
SANITIZE_CHUNK_CHARACTERS = 64 * 1024

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

_DOCUMENT_PREFIX = (
    '<!doctype html>'
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


@dataclass(frozen=True)
class StaticHtmlPreview:
    """An inert document plus an honest description of omitted capabilities."""

    document: bytes
    blocked_active_content: bool
    blocked_external_resources: bool
    requires_active_content: bool


_VOID_ELEMENTS = frozenset(
    (
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    )
)
_DROP_WITH_CONTENT = frozenset(
    ("applet", "frameset", "iframe", "object", "script")
)
_DROP_ELEMENTS = frozenset(
    ("base", "embed", "frame", "link", "meta", "param", "source", "track")
)
_VOID_ACTIVE_ELEMENTS = frozenset(("embed", "frame"))
_ACTIVE_ATTRIBUTES = frozenset(("action", "formaction", "ping", "srcdoc"))
_URL_ATTRIBUTES = frozenset(("href", "poster", "src", "srcset", "xlink:href"))


class _StaticHtmlSanitizer(HTMLParser):
    """Serialize only passive markup; WebKit's CSP remains the security boundary."""

    def __init__(self, check_request: Callable[[], None]) -> None:
        super().__init__(convert_charrefs=False)
        self.check_request = check_request
        self.parts: list[str] = []
        self.characters = 0
        self.drop_depth = 0
        self.style_depth = 0
        self.head_depth = 0
        self.script_count = 0
        self.visible_text_characters = 0
        self.has_static_visual = False
        self.has_app_loader_marker = False
        self.blocked_active_content = False
        self.blocked_external_resources = False

    def _append(self, value: str) -> None:
        self.check_request()
        self.characters += len(value)
        if self.characters > MAX_SANITIZED_HTML_CHARACTERS:
            raise HtmlPreviewError("The sanitized HTML preview is too large")
        self.parts.append(value)

    @staticmethod
    def _is_external(value: str) -> bool:
        folded = value.lstrip().casefold()
        return folded.startswith(("http:", "https:", "file:", "//"))

    def _sanitize_css(self, value: str) -> str:
        """Strip imports/external URLs in one forward pass.

        CSS escaping is intentionally left to the mandatory CSP. This pass is
        only an availability optimization and must stay linear for hostile text.
        """

        # CSS keywords are ASCII-insensitive. Unicode casefold can expand a
        # character (for example ß), misaligning offsets into the original CSS.
        folded = value.translate(str.maketrans(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz",
        ))
        parts: list[str] = []
        cursor = 0
        literal_start = 0
        next_check = 0
        length = len(value)
        while cursor < length:
            if cursor >= next_check:
                self.check_request()
                next_check = cursor + 4096
            if folded.startswith("@import", cursor):
                boundary = cursor + len("@import")
                if boundary == length or not (
                    folded[boundary].isalnum() or folded[boundary] in "-_"
                ):
                    end = value.find(";", boundary)
                    self.blocked_external_resources = True
                    parts.append(value[literal_start:cursor])
                    if end < 0:
                        literal_start = length
                        break
                    cursor = end + 1
                    literal_start = cursor
                    continue
            if folded.startswith("url", cursor):
                opening = cursor + 3
                while opening < length and value[opening].isspace():
                    opening += 1
                if opening < length and value[opening] == "(":
                    end = value.find(")", opening + 1)
                    if end < 0:
                        # Treat one unterminated function as the remaining CSS;
                        # do not repeatedly scan the same suffix.
                        payload = value[opening + 1 :]
                        parts.append(value[literal_start:cursor])
                        if self._css_url_is_external(payload):
                            self.blocked_external_resources = True
                            parts.append("url(data:,)")
                        else:
                            parts.append(value[cursor:])
                        literal_start = length
                        break
                    payload = value[opening + 1 : end]
                    if self._css_url_is_external(payload):
                        self.blocked_external_resources = True
                        parts.append(value[literal_start:cursor])
                        parts.append("url(data:,)")
                        literal_start = end + 1
                    else:
                        # Leave passive and data URLs in the pending literal run.
                        pass
                    cursor = end + 1
                    continue
            cursor += 1
        if literal_start < length:
            parts.append(value[literal_start:])
        return "".join(parts)

    @classmethod
    def _css_url_is_external(cls, payload: str) -> bool:
        value = payload.lstrip()
        if value[:1] in ("'", '"'):
            value = value[1:].lstrip()
        return cls._is_external(value)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if self.drop_depth:
            if tag in _DROP_WITH_CONTENT:
                self.drop_depth += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self.blocked_active_content = True
            if tag == "script":
                self.script_count += 1
                if any(
                    self._is_external(value or "")
                    for name, value in attrs
                    if name.casefold() == "src"
                ):
                    self.blocked_external_resources = True
            else:
                self.blocked_external_resources = True
                self.has_app_loader_marker = True
            self.drop_depth = 1
            return
        if tag in _DROP_ELEMENTS:
            if tag in _VOID_ACTIVE_ELEMENTS:
                self.blocked_active_content = True
                self.has_app_loader_marker = True
            elif tag == "base":
                self.blocked_active_content = True
            elif tag == "meta" and any(
                name.casefold() == "http-equiv"
                and (value or "").casefold() == "refresh"
                for name, value in attrs
            ):
                self.blocked_active_content = True
            if any(
                self._is_external(value or "")
                for name, value in attrs
                if name.casefold() in _URL_ATTRIBUTES
            ):
                self.blocked_external_resources = True
            return

        sanitized_attrs: list[tuple[str, str | None]] = []
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            value = raw_value or ""
            if name.startswith("on") or name in _ACTIVE_ATTRIBUTES:
                self.blocked_active_content = True
                continue
            if name in _URL_ATTRIBUTES:
                external = self._is_external(value)
                if external:
                    self.blocked_external_resources = True
                # Only embedded images and same-document SVG references remain.
                keep_data_image = (
                    tag == "img"
                    and name == "src"
                    and value.lstrip().casefold().startswith("data:image/")
                )
                keep_fragment = name in ("href", "xlink:href") and value.startswith("#")
                if not (keep_data_image or keep_fragment):
                    continue
                if keep_data_image:
                    self.has_static_visual = True
            if name == "style":
                value = self._sanitize_css(value)
            sanitized_attrs.append((name, value if raw_value is not None else None))

        if tag == "svg":
            self.has_static_visual = True
        if tag == "head":
            self.head_depth += 1
        if tag == "style":
            self.style_depth += 1
        self._append(f"<{tag}")
        for name, value in sanitized_attrs:
            self._append(f" {html.escape(name, quote=True)}")
            if value is not None:
                self._append(f'="{html.escape(value, quote=True)}"')
        self._append(">")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        normalized = tag.casefold()
        if normalized in _DROP_WITH_CONTENT:
            self.handle_endtag(normalized)
        elif normalized not in _VOID_ELEMENTS and not self.drop_depth:
            self.handle_endtag(normalized)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.drop_depth:
            if tag in _DROP_WITH_CONTENT:
                self.drop_depth -= 1
            return
        if tag in _DROP_ELEMENTS or tag in _VOID_ELEMENTS:
            return
        if tag == "head" and self.head_depth:
            self.head_depth -= 1
        if tag == "style" and self.style_depth:
            self.style_depth -= 1
        self._append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            folded = data.casefold()
            if any(
                marker in folded
                for marker in (
                    "fetch(",
                    "createelement",
                    "document.write",
                    "iframe",
                    "innerhtml",
                )
            ):
                self.has_app_loader_marker = True
            return
        if not self.head_depth and not self.style_depth:
            self.visible_text_characters += len("".join(data.split()))
        self._append(self._sanitize_css(data) if self.style_depth else html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if not self.drop_depth:
            self._append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.drop_depth:
            self._append(f"&#{name};")


def prepare_static_preview(
    source: bytes | str,
    *,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> StaticHtmlPreview:
    """Remove active/external loaders and retain meaningful static HTML.

    @decision The parser is an availability layer, not the sandbox: WebKit still
    receives mandatory settings and two intersecting CSPs. Removing dependencies
    before loading prevents blocked app shells from holding the preview open.
    """

    if isinstance(source, bytes):
        text = source.decode("utf-8", errors="replace")
    elif isinstance(source, str):
        text = source
    else:
        raise TypeError("HTML source must be bytes or text")

    deadline = time.monotonic() + SANITIZE_TIMEOUT_SECONDS

    def check_request() -> None:
        if is_cancelled():
            raise _PreviewCancelled
        if time.monotonic() >= deadline:
            raise HtmlPreviewError("HTML sanitization timed out")

    check_request()
    sanitizer = _StaticHtmlSanitizer(check_request)
    try:
        for offset in range(0, len(text), SANITIZE_CHUNK_CHARACTERS):
            check_request()
            sanitizer.feed(text[offset : offset + SANITIZE_CHUNK_CHARACTERS])
        sanitizer.close()
    except _PreviewCancelled:
        raise
    except HtmlPreviewError:
        raise
    except Exception as error:
        raise HtmlPreviewError("The HTML structure could not be sanitized") from error

    check_request()
    requires_active_content = (
        not sanitizer.has_static_visual
        and (
            (
                sanitizer.visible_text_characters == 0
                and (
                    sanitizer.script_count > 0
                    or sanitizer.has_app_loader_marker
                )
            )
            or (
                sanitizer.script_count > 0
                and sanitizer.visible_text_characters < 128
                and sanitizer.has_app_loader_marker
                and sanitizer.blocked_external_resources
            )
        )
    )
    if requires_active_content:
        passive_source = (
            '<main style="font:16px system-ui;padding:2rem;max-width:44rem">'
            "<h1>Interactive page not run</h1>"
            "<p>This file contains an app shell that depends on scripts or external "
            "content. Kukni blocks both, so there is no meaningful static page to show.</p>"
            "</main>"
        )
    else:
        passive_source = "".join(sanitizer.parts)

    return StaticHtmlPreview(
        document=build_safe_document(passive_source),
        blocked_active_content=sanitizer.blocked_active_content,
        blocked_external_resources=sanitizer.blocked_external_resources,
        requires_active_content=requires_active_content,
    )


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
    """Prefix standards mode and a restrictive CSP before all source bytes."""

    if isinstance(source, str):
        source = source.encode("utf-8", errors="replace")
    elif not isinstance(source, bytes):
        raise TypeError("HTML source must be bytes or text")
    return _DOCUMENT_PREFIX + source


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


def stop_and_terminate_web_view(view) -> None:
    """Stop a pending load and tear down its isolated content process."""

    view.stop_loading()
    try:
        view.terminate_web_process()
    except Exception:
        # Some WebKit builds report an already-exited process as an error.
        pass


class HtmlRenderer:
    """Render local HTML without script, file, or network capabilities."""

    id = "html"

    def __init__(self) -> None:
        # A loading WebView is not parented until it succeeds, so retain it here.
        self._loading_views: dict[int, Gtk.Widget] = {}
        # One parser/probe worker plus one replaceable pending request bounds
        # rapid file-manager selection without making stale requests wait in a
        # thread-pool queue.
        self._worker_lock = threading.Lock()
        self._worker_running = False
        self._pending_worker: tuple[
            Callable[[], None], Gio.Cancellable, ErrorCallback
        ] | None = None

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
        # Capability selection runs on GTK's thread. The active Bubblewrap
        # probe can take seconds, so the worker performs it after selection.
        return is_html

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
        if cancellable.is_cancelled():
            return

        def worker() -> None:
            try:
                source = read_bounded_local_file(
                    path,
                    is_cancelled=cancellable.is_cancelled,
                )
                if cancellable.is_cancelled():
                    raise _PreviewCancelled
                preview = prepare_static_preview(
                    source,
                    is_cancelled=cancellable.is_cancelled,
                )
                if preview.requires_active_content:
                    GLib.idle_add(
                        self._deliver_active_notice,
                        cancellable,
                        on_ready,
                    )
                    return
                if not webkit_runtime_available():
                    GLib.idle_add(
                        self._deliver_error,
                        cancellable,
                        on_error,
                        "Secure WebKitGTK HTML preview is unavailable on this system",
                    )
                    return
            except _PreviewCancelled:
                return
            except HtmlPreviewError as error:
                GLib.idle_add(self._deliver_error, cancellable, on_error, str(error))
                return
            GLib.idle_add(
                self._create_web_view,
                preview,
                cancellable,
                on_ready,
                on_error,
            )

        self._queue_worker(worker, cancellable, on_error)

    def _queue_worker(
        self,
        worker: Callable[[], None],
        cancellable: Gio.Cancellable,
        on_error: ErrorCallback,
    ) -> None:
        request = (worker, cancellable, on_error)
        with self._worker_lock:
            if self._worker_running:
                self._pending_worker = request
                return
            self._worker_running = True
        self._start_worker(request)

    def _start_worker(
        self,
        request: tuple[Callable[[], None], Gio.Cancellable, ErrorCallback],
    ) -> None:
        worker, cancellable, on_error = request

        def run() -> None:
            try:
                worker()
            finally:
                self._worker_finished()

        try:
            threading.Thread(
                target=run,
                name="kukni-html-reader",
                daemon=True,
            ).start()
        except Exception:
            GLib.idle_add(
                self._deliver_error,
                cancellable,
                on_error,
                "The HTML preview worker could not be started",
            )
            self._worker_finished()

    def _worker_finished(self) -> None:
        with self._worker_lock:
            request = self._pending_worker
            self._pending_worker = None
            if request is None or request[1].is_cancelled():
                self._worker_running = False
                return
        self._start_worker(request)

    @staticmethod
    def _deliver_error(
        cancellable: Gio.Cancellable,
        on_error,
        message: str,
    ) -> bool:
        if not cancellable.is_cancelled():
            on_error(message)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _deliver_active_notice(
        cancellable: Gio.Cancellable,
        on_ready,
    ) -> bool:
        if cancellable.is_cancelled():
            return GLib.SOURCE_REMOVE
        page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=32,
            margin_bottom=32,
            margin_start=32,
            margin_end=32,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        # An explanation is a compact card, not a large blank HTML viewport.
        page.preview_geometry = ("fallback", 0, 0)
        page.append(Gtk.Image(icon_name="web-browser-symbolic", pixel_size=56))
        title = Gtk.Label(label="Interactive page not run")
        title.add_css_class("title-2")
        page.append(title)
        detail = Gtk.Label(
            label=(
                "This file is an app shell that depends on scripts or external "
                "content. Kukni blocks both, so there is no meaningful static "
                "page to show."
            ),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=48,
        )
        detail.add_css_class("dim-label")
        page.append(detail)
        on_ready(page, "Interactive HTML not run · active content blocked")
        return GLib.SOURCE_REMOVE

    def _create_web_view(
        self,
        preview: StaticHtmlPreview,
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
            stop_and_terminate_web_view(view)

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
                if preview.requires_active_content:
                    detail = "Interactive HTML not run · active content blocked"
                else:
                    omitted = []
                    if preview.blocked_active_content:
                        omitted.append("active content")
                    if preview.blocked_external_resources:
                        omitted.append("external resources")
                    detail = (
                        f"Static HTML · {' and '.join(omitted)} blocked"
                        if omitted
                        else "Static HTML document"
                    )
                on_ready(wrapper, detail)

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
            fail(
                f"HTML preview did not finish within {LOAD_TIMEOUT_SECONDS} seconds"
            )
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

        try:
            view.load_bytes(
                GLib.Bytes.new(preview.document),
                "text/html",
                "UTF-8",
                "about:blank",
            )
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
