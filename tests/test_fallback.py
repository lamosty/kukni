# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.renderers.fallback import format_hex_sample, is_probably_text


class FallbackFormattingTests(unittest.TestCase):
    def test_detects_declared_and_valid_utf8_text(self):
        self.assertTrue(is_probably_text(b"hello", "text/plain"))
        self.assertTrue(is_probably_text("ahoj svet".encode(), None))

    def test_rejects_binary_and_invalid_utf8(self):
        self.assertFalse(is_probably_text(b"abc\x00def", None))
        self.assertFalse(is_probably_text(b"\xff\xfe", None))

    def test_formats_bounded_hex_dump(self):
        rendered = format_hex_sample(b"Hello\x00world" + bytes(range(32)), limit=16)

        self.assertIn("00000000", rendered)
        self.assertIn("48 65 6c 6c 6f", rendered)
        self.assertIn("|Hello.world", rendered)
        self.assertNotIn("00000010", rendered)


if __name__ == "__main__":
    unittest.main()
