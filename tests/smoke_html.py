#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Verify HTML either renders securely or degrades without closing Kukni."""

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import tempfile
import threading


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib

from kukni.renderers.html import HtmlRenderer, webkit_runtime_available
from kukni.renderers.registry import RendererRegistry
from kukni.session import PreviewState
from kukni.window import PreviewWindow


class HtmlSmokeApplication(Adw.Application):
    def __init__(
        self,
        sample: Path,
        server: ThreadingHTTPServer,
        external_url: str,
    ) -> None:
        super().__init__(application_id="io.github.lamosty.Kukni.HtmlSmoke")
        self.sample = sample
        self.server = server
        self.external_url = external_url
        self.failures: list[str] = []
        self.expected = (
            PreviewState.PREVIEW
            if webkit_runtime_available()
            else PreviewState.FALLBACK
        )
        self.window = None
        self.checks = 0

    def do_activate(self) -> None:
        self.window = PreviewWindow(
            self,
            RendererRegistry((HtmlRenderer(),)),
        )
        self.window.show_file(Gio.File.new_for_path(str(self.sample)))
        GLib.timeout_add(100, self._poll_result)

    def _poll_result(self) -> bool:
        self.checks += 1
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and self.checks < 60:
            return GLib.SOURCE_CONTINUE

        if snapshot.state is not self.expected:
            self.failures.append(
                f"expected {self.expected.value}, received {snapshot.state.value}"
            )
        if not self.window.get_visible():
            self.failures.append("HTML result closed the preview window")
        if snapshot.state is PreviewState.PREVIEW:
            wrapper = self.window._stack.get_child_by_name("content")
            document = (
                wrapper.get_child_by_name("document")
                if hasattr(wrapper, "get_child_by_name")
                else None
            )
            if document is None:
                self.failures.append("HTML renderer did not return its protected view")
            else:
                document.load_uri(f"{self.external_url}/post-ready-navigation")
            GLib.timeout_add(350, self._finish_after_policy_check)
            return GLib.SOURCE_REMOVE
        return self._finish_after_policy_check()

    def _finish_after_policy_check(self) -> bool:
        if self.server.request_count:
            self.failures.append(
                f"HTML preview made {self.server.request_count} external request(s)"
            )
        self.window.close()
        self.quit()
        return GLib.SOURCE_REMOVE


class NetworkProbeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), NetworkProbeHandler)
        self.request_count = 0


class NetworkProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.request_count += 1
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        pass


def main() -> int:
    server = NetworkProbeServer()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    external_url = f"http://{host}:{port}"
    try:
        with tempfile.TemporaryDirectory() as temporary:
            sample = Path(temporary, "sample.html")
            sample.write_text(
                "<!doctype html><title>Kukni smoke</title>"
                f'<meta http-equiv="refresh" content="0;url={external_url}/refresh">'
                f'<link rel="stylesheet" href="{external_url}/style.css">'
                f'<img src="{external_url}/image.png">'
                f'<iframe src="{external_url}/frame"></iframe>'
                "<h1>Safe HTML preview</h1>",
                encoding="utf-8",
            )
            application = HtmlSmokeApplication(sample, server, external_url)
            exit_code = application.run(["kukni-html-smoke"])
            failures = application.failures
            expected = application.expected.value
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    if exit_code != 0:
        failures.append(f"application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"smoke failure: {failure}", file=sys.stderr)
        return 1
    print(f"HTML smoke test passed ({expected})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
