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


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        sample = Path(directory) / "unknown.kukni-smoke"
        sample.write_bytes(b"Kukni fallback smoke test\n")

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
