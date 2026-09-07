# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
from pathlib import Path
import tempfile
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
            ("folder", "cr2", "image", "xlsx", "pdf", "html", "text"),
        )

    def test_default_registry_routes_folders_without_claiming_regular_formats(self):
        from gi.repository import Gio

        attributes = ",".join(
            (
                Gio.FILE_ATTRIBUTE_STANDARD_TYPE,
                Gio.FILE_ATTRIBUTE_STANDARD_CONTENT_TYPE,
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                "folder": root,
                "image": root / "picture.png",
                "pdf": root / "document.pdf",
                "text": root / "notes.txt",
            }
            fixtures["image"].write_bytes(b"\x89PNG\r\n\x1a\n")
            fixtures["pdf"].write_bytes(b"%PDF-1.7\n")
            fixtures["text"].write_text("hello\n", encoding="utf-8")
            registry = default_registry()

            selected = {}
            for expected, path in fixtures.items():
                file = Gio.File.new_for_path(str(path))
                info = file.query_info(attributes, Gio.FileQueryInfoFlags.NONE, None)
                selected[expected] = registry.select(file, info).id

        self.assertEqual(selected, {name: name for name in fixtures})

    def test_media_renderer_remains_available_for_explicit_opt_in(self):
        from kukni.renderers.media import MediaRenderer

        registry = RendererRegistry((MediaRenderer(),))

        self.assertEqual(
            tuple(renderer.id for renderer in registry.renderers),
            ("media",),
        )


if __name__ == "__main__":
    unittest.main()
