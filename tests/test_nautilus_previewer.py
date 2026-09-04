# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio

from kukni.nautilus_previewer import (
    CURRENT_INTERFACE,
    DIRECTION_VALUES,
    INTERFACE_XML,
    LEGACY_INTERFACE,
)
from kukni.session import Direction


class NautilusPreviewerContractTests(unittest.TestCase):
    def test_exports_legacy_and_current_interfaces(self):
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)

        legacy = node.lookup_interface(LEGACY_INTERFACE)
        current = node.lookup_interface(CURRENT_INTERFACE)

        self.assertIsNotNone(legacy.lookup_method("ShowFile"))
        self.assertIsNotNone(legacy.lookup_method("Close"))
        self.assertIsNotNone(current.lookup_method("ShowFile"))
        self.assertIsNotNone(current.lookup_method("Close"))
        self.assertIsNotNone(current.lookup_property("ParentHandle"))
        self.assertIsNotNone(current.lookup_property("Visible"))
        self.assertIsNotNone(current.lookup_signal("SelectionEvent"))

    def test_direction_values_match_gtk_contract(self):
        self.assertEqual(DIRECTION_VALUES[Direction.UP], 2)
        self.assertEqual(DIRECTION_VALUES[Direction.DOWN], 3)
        self.assertEqual(DIRECTION_VALUES[Direction.LEFT], 4)
        self.assertEqual(DIRECTION_VALUES[Direction.RIGHT], 5)


if __name__ == "__main__":
    unittest.main()
