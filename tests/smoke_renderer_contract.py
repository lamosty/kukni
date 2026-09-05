#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise renderer failures and stale callbacks in the real GTK window."""

from pathlib import Path
import sys
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk

from kukni.renderers.registry import RendererRegistry
from kukni.session import PreviewState
from kukni.window import PreviewWindow


class ContractRenderer:
    id = "contract-smoke"

    def __init__(self) -> None:
        self.slow_ready = None

    def supports(self, _file, _info) -> bool:
        return True

    def render(self, file, _info, _cancellable, on_ready, _on_error) -> None:
        suffix = Path(file.get_path()).suffix
        if suffix == ".exception":
            raise RuntimeError("synthetic renderer failure")
        if suffix == ".invalid":
            on_ready(None, "Invalid")
            return
        if suffix == ".slow":
            self.slow_ready = on_ready
            return
        if suffix == ".hang":
            return
        on_ready(Gtk.Label(label="Current preview"), "Synthetic preview")


class ContractSmokeApplication(Adw.Application):
    def __init__(self, directory: Path) -> None:
        super().__init__(application_id="io.github.lamosty.Kukni.ContractSmoke")
        self.directory = directory
        self.failures: list[str] = []
        self.renderer = ContractRenderer()
        self.window = None
        self.hang_deadline: float | None = None

    def do_activate(self) -> None:
        self.window = PreviewWindow(
            self,
            RendererRegistry((self.renderer,)),
            opening_timeout_seconds=1,
        )
        self._show("failure.exception")
        GLib.timeout_add(160, self._check_exception)
        GLib.timeout_add_seconds(8, self._watchdog)

    def _show(self, name: str) -> None:
        path = self.directory / name
        path.write_text(f"fixture: {name}\n", encoding="utf-8")
        self.window.show_file(Gio.File.new_for_path(str(path)))

    def _expect_state(self, expected: PreviewState, phase: str) -> None:
        actual = self.window.session.snapshot.state
        if actual is not expected:
            self.failures.append(
                f"{phase}: expected {expected.value}, received {actual.value}"
            )
        if not self.window.get_visible():
            self.failures.append(f"{phase}: preview window closed unexpectedly")

    def _check_exception(self) -> bool:
        self._expect_state(PreviewState.FALLBACK, "renderer exception")
        self._show("invalid.invalid")
        GLib.timeout_add(160, self._check_invalid)
        return GLib.SOURCE_REMOVE

    def _check_invalid(self) -> bool:
        self._expect_state(PreviewState.FALLBACK, "invalid renderer widget")
        self._show("earlier.slow")
        GLib.timeout_add(160, self._replace_slow_request)
        return GLib.SOURCE_REMOVE

    def _replace_slow_request(self) -> bool:
        if self.renderer.slow_ready is None:
            self.failures.append("slow renderer was not started")
            self._finish()
            return GLib.SOURCE_REMOVE
        stale_ready = self.renderer.slow_ready
        self._show("current.good")
        GLib.timeout_add(160, self._deliver_stale_result, stale_ready)
        return GLib.SOURCE_REMOVE

    def _deliver_stale_result(self, stale_ready) -> bool:
        self._expect_state(PreviewState.PREVIEW, "current renderer")
        stale_ready(Gtk.Label(label="Stale preview"), "Stale")
        GLib.timeout_add(80, self._check_stale_result)
        return GLib.SOURCE_REMOVE

    def _check_stale_result(self) -> bool:
        snapshot = self.window.session.snapshot
        self._expect_state(PreviewState.PREVIEW, "stale renderer callback")
        if not snapshot.current_uri.endswith("/current.good"):
            self.failures.append("stale callback replaced the current URI")
        if snapshot.detail != "Synthetic preview":
            self.failures.append("stale callback replaced the current preview")
        self._show("bounded.hang")
        # GLib second-based timers intentionally use coarse scheduling. Poll to
        # observe the bounded transition instead of racing it with another
        # arbitrary one-shot timeout.
        self.hang_deadline = time.monotonic() + 4.0
        GLib.timeout_add(100, self._check_hang_timeout)
        return GLib.SOURCE_REMOVE

    def _check_hang_timeout(self) -> bool:
        snapshot = self.window.session.snapshot
        if snapshot.state is PreviewState.OPENING and (
            self.hang_deadline is not None and time.monotonic() < self.hang_deadline
        ):
            return GLib.SOURCE_CONTINUE
        self._expect_state(PreviewState.ERROR, "hung renderer timeout")
        if "exceeded 1 second" not in (snapshot.detail or ""):
            self.failures.append("hung renderer did not report the bounded timeout")
        if self.window.lookup_action("navigate-down") is None:
            self.failures.append("navigation disappeared after renderer timeout")
        self._finish()
        return GLib.SOURCE_REMOVE

    def _finish(self) -> None:
        self.window.close()
        self.quit()

    def _watchdog(self) -> bool:
        self.failures.append("renderer contract smoke test timed out")
        self._finish()
        return GLib.SOURCE_REMOVE


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        application = ContractSmokeApplication(Path(temporary))
        exit_code = application.run(["kukni-renderer-contract-smoke"])
        failures = application.failures

    if exit_code != 0:
        failures.append(f"application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"smoke failure: {failure}", file=sys.stderr)
        return 1
    print("GTK renderer contract smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
