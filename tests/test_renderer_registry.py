# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.renderers.registry import (
    RendererProbeError,
    RendererRegistry,
    default_registry,
)


class FakeRenderer:
    def __init__(self, identifier, result=False, error=None):
        self.id = identifier
        self.result = result
        self.error = error
        self.probes = 0

    def supports(self, _file, _info):
        self.probes += 1
        if self.error is not None:
            raise self.error
        return self.result

    def render(self, *_args):
        raise AssertionError("registry selection must not start rendering")


class RendererRegistryTests(unittest.TestCase):
    def test_selects_first_matching_renderer(self):
        first = FakeRenderer("first")
        second = FakeRenderer("second", result=True)
        third = FakeRenderer("third", result=True)

        selected = RendererRegistry((first, second, third)).select(object(), object())

        self.assertIs(selected, second)
        self.assertEqual((first.probes, second.probes, third.probes), (1, 1, 0))

    def test_returns_none_when_no_renderer_matches(self):
        renderer = FakeRenderer("only")

        self.assertIsNone(RendererRegistry((renderer,)).select(object(), object()))

    def test_rejects_duplicate_and_empty_ids(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            RendererRegistry((FakeRenderer("same"), FakeRenderer("same")))
        with self.assertRaisesRegex(ValueError, "empty"):
            RendererRegistry((FakeRenderer(""),))

    def test_wraps_capability_probe_errors_with_renderer_identity(self):
        registry = RendererRegistry((FakeRenderer("broken", error=ValueError("bad")),))

        with self.assertRaisesRegex(RendererProbeError, "broken.*bad"):
            registry.select(object(), object())

    def test_default_registry_has_deterministic_built_in_order(self):
        self.assertEqual(
            tuple(renderer.id for renderer in default_registry().renderers),
            ("xlsx", "pdf", "media", "html", "text"),
        )


if __name__ == "__main__":
    unittest.main()
