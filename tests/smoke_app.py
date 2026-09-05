#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Launch the real GTK shell and verify an unknown file stays previewable."""

from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import GLib

from kukni.application import KukniApplication
from kukni.session import PreviewState
from gi.repository import Gtk


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory) / "unknown.kukni-smoke"
        # An unknown suffix with ordinary prose is correctly detected as text
        # by Gio. Use genuine opaque binary data to exercise the unavailable UI.
        sample.write_bytes(b"\x00\x01\x02Kukni fallback smoke test\x00")

        application = KukniApplication()

        def verify() -> bool:
            try:
                window = application.get_active_window()
                if window is None:
                    raise AssertionError("application did not create a window")
                snapshot = window.session.snapshot
                if snapshot.current_uri != sample.as_uri():
                    raise AssertionError(f"unexpected current URI: {snapshot.current_uri}")
                if snapshot.state is not PreviewState.FALLBACK:
                    raise AssertionError(f"unexpected state: {snapshot.state.value}")
                if not window.get_visible():
                    raise AssertionError("preview window is not visible")
                content = window._stack.get_child_by_name("content")
                widgets = tuple(descendants(content))
                if any(isinstance(widget, Gtk.TextView) for widget in widgets):
                    raise AssertionError("fallback must not expose a byte inspector")
                labels = [
                    widget.get_label() for widget in widgets
                    if isinstance(widget, Gtk.Label)
                ]
                if "Preview unavailable" not in labels:
                    raise AssertionError("fallback has no plain-language heading")
                if any("Kukni fallback smoke test" in label for label in labels):
                    raise AssertionError("fallback exposed selected file contents")
            except Exception as error:  # pragma: no cover - smoke diagnostics
                failures.append(str(error))
            finally:
                application.quit()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(600, verify)
        exit_code = application.run(["kukni-smoke", str(sample)])

    if exit_code != 0:
        failures.append(f"application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"smoke failure: {failure}", file=sys.stderr)
        return 1
    print("GTK application smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
