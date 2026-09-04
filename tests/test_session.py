# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.session import Direction, PreviewSession, PreviewState


class PreviewSessionTests(unittest.TestCase):
    def test_supported_fallback_supported_navigation_stays_open(self):
        session = PreviewSession()

        file_a = session.show("file:///photos/a.cr2")
        self.assertTrue(session.resolve(file_a, PreviewState.PREVIEW))
        request = session.request_navigation(Direction.RIGHT)
        self.assertEqual(request.current_uri, "file:///photos/a.cr2")

        file_b = session.show("file:///photos/b.html")
        self.assertEqual(session.snapshot.state, PreviewState.OPENING)
        self.assertTrue(
            session.resolve(
                file_b,
                PreviewState.FALLBACK,
                "No rich renderer is installed",
            )
        )
        self.assertTrue(session.is_open)
        request = session.request_navigation(Direction.RIGHT)
        self.assertEqual(request.current_uri, "file:///photos/b.html")

        file_c = session.show("file:///photos/c.jpg")
        self.assertTrue(session.resolve(file_c, PreviewState.PREVIEW))
        self.assertEqual(session.snapshot.current_uri, "file:///photos/c.jpg")
        self.assertEqual(session.snapshot.state, PreviewState.PREVIEW)

    def test_renderer_error_is_an_in_window_state(self):
        session = PreviewSession()
        token = session.show("file:///documents/damaged.xlsx")

        self.assertTrue(
            session.resolve(token, PreviewState.ERROR, "Conversion failed")
        )

        self.assertTrue(session.is_open)
        self.assertEqual(session.snapshot.state, PreviewState.ERROR)
        self.assertEqual(
            session.request_navigation(Direction.DOWN).current_uri,
            "file:///documents/damaged.xlsx",
        )

    def test_late_renderer_result_cannot_replace_new_file(self):
        session = PreviewSession()
        stale = session.show("file:///slow-a.pdf")
        current = session.show("file:///current-b.txt")

        self.assertFalse(session.resolve(stale, PreviewState.PREVIEW))
        self.assertTrue(session.resolve(current, PreviewState.PREVIEW))
        self.assertEqual(session.snapshot.current_uri, "file:///current-b.txt")

    def test_only_explicit_actions_close_the_session(self):
        session = PreviewSession()
        first = session.show("file:///same-file")
        session.resolve(first, PreviewState.FALLBACK)

        refreshed = session.show("file:///same-file")
        self.assertIsNotNone(refreshed)
        self.assertTrue(session.is_open)

        toggled = session.show(
            "file:///same-file",
            close_if_already_shown=True,
        )
        self.assertIsNone(toggled)
        self.assertFalse(session.is_open)

    def test_close_invalidates_outstanding_work(self):
        session = PreviewSession()
        token = session.show("file:///slow.raw")

        session.close()

        self.assertFalse(session.resolve(token, PreviewState.PREVIEW))
        self.assertEqual(session.snapshot.state, PreviewState.CLOSED)

    def test_navigation_requires_an_open_session(self):
        session = PreviewSession()

        with self.assertRaisesRegex(RuntimeError, "closed"):
            session.request_navigation(Direction.LEFT)

    def test_rejects_invalid_result_state(self):
        session = PreviewSession()
        token = session.show("file:///file")

        with self.assertRaisesRegex(ValueError, "invalid renderer"):
            session.resolve(token, PreviewState.OPENING)


if __name__ == "__main__":
    unittest.main()
