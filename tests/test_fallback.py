# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio
from kukni.renderers.fallback import unavailable_message


class FallbackMessageTests(unittest.TestCase):
    def info(self, kind=Gio.FileType.REGULAR, size=100):
        info = Gio.FileInfo()
        info.set_file_type(kind)
        info.set_size(size)
        return info

    def test_unknown_regular_file_gets_a_plain_explanation(self):
        self.assertEqual(
            unavailable_message(self.info()),
            "A preview isn't available for this file type yet.",
        )

    def test_empty_file_is_not_described_as_unsupported(self):
        self.assertEqual(unavailable_message(self.info(size=0)), "This file is empty.")

    def test_folder_is_not_described_as_empty_or_binary(self):
        self.assertEqual(
            unavailable_message(self.info(Gio.FileType.DIRECTORY, 0)),
            "Folder previews aren't available yet.",
        )

    def test_special_file_has_no_content_inspection(self):
        self.assertEqual(
            unavailable_message(self.info(Gio.FileType.SPECIAL)),
            "This item doesn't have a preview.",
        )


if __name__ == "__main__":
    unittest.main()
