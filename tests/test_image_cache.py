# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kukni.image_cache import ImageCache
from kukni.renderers import cr2
from kukni.renderers.cr2 import Cr2Renderer, Cr2WorkerOutput, Cr2WorkerResult
from kukni.renderers.image import ImageRenderer
from gi.repository import Gdk, Gio, GLib
from image_fixtures import png
from test_cr2_renderer import build_jpeg, TrackingSlot
from test_navigation_integration import spin_until


def output(width=2):
    return Cr2WorkerOutput(Cr2WorkerResult(width, 1, width, 1, width * 4, width * 4), b"x" * (width * 4))


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "image"
        self.path.write_bytes(b"source")
        self.cache = ImageCache()

    def tearDown(self):
        self.directory.cleanup()

    def key(self, renderer="image", path=None):
        return self.cache.key_for(renderer, str(path or self.path))

    def test_hit_returns_the_same_immutable_output(self):
        key, pixels = self.key(), output()
        self.assertTrue(self.cache.put(key, pixels))
        self.assertIs(self.cache.get(key), pixels)
        self.assertEqual(self.cache._bytes, 8)

    def test_renderer_identity_is_part_of_the_key(self):
        self.cache.put(self.key(), output())
        self.assertIsNone(self.cache.get(self.key("cr2")))

    def test_modification_with_restored_mtime_still_invalidates(self):
        key = self.key()
        self.cache.put(key, output())
        time.sleep(0.002)
        self.path.write_bytes(b"change")
        os.utime(self.path, ns=(key.modified_ns, key.modified_ns))
        self.assertNotEqual(self.key(), key)
        self.assertIsNone(self.cache.get(key))
        self.assertFalse(self.cache.put(key, output()))

    def test_atomic_replacement_and_deletion_invalidate(self):
        key = self.key()
        self.cache.put(key, output())
        replacement = self.root / "replacement"
        replacement.write_bytes(self.path.read_bytes())
        os.utime(replacement, ns=(key.modified_ns, key.modified_ns))
        replacement.replace(self.path)
        self.assertIsNone(self.cache.get(key))
        key = self.key()
        self.cache.put(key, output())
        self.path.unlink()
        self.assertIsNone(self.cache.get(key))

    def test_unavailable_or_nonregular_metadata_disables_cache(self):
        for path in (self.root / "missing", self.root):
            self.assertIsNone(self.cache.key_for("image", str(path)))
        self.path.write_bytes(b"")
        self.assertIsNone(self.key())
        self.assertIsNone(self.cache.get(None))
        self.assertFalse(self.cache.put(None, output()))

    def test_only_valid_immutable_output_is_accepted(self):
        valid = output()
        for invalid in (None, b"encoded", replace(valid, pixels=bytearray(valid.pixels)),
                        replace(valid, pixels=b"short"),
                        replace(valid, result=replace(valid.result, stride=4)),
                        replace(valid, result=replace(valid.result, width=True))):
            with self.subTest(invalid=type(invalid)):
                self.assertFalse(self.cache.put(self.key(), invalid))
        self.assertEqual(self.cache._bytes, 0)

    def test_byte_entry_caps_and_lru(self):
        self.cache = ImageCache(max_bytes=16, max_entries=2)
        keys = []
        for index in range(3):
            path = self.root / str(index)
            path.write_bytes(b"source")
            keys.append(self.key(path=path))
        self.cache.put(keys[0], output())
        self.cache.put(keys[1], output())
        self.cache.get(keys[0])
        self.cache.put(keys[2], output())
        self.assertIsNone(self.cache.get(keys[1]))
        self.assertIsNotNone(self.cache.get(keys[0]))
        self.assertEqual(self.cache._bytes, 16)
        self.assertFalse(self.cache.put(keys[1], output(5)))
        self.assertEqual(self.cache._bytes, 16)
        # Byte cap, not entry count, now forces both smaller entries out.
        self.cache.put(keys[1], output(4))
        self.assertEqual(len(self.cache._entries), 1)
        self.assertEqual(self.cache._bytes, 16)

    def test_ttl_is_not_extended_by_hits_and_releases_payload(self):
        now = [0.0]
        self.cache = ImageCache(ttl_seconds=5, clock=lambda: now[0])
        key = self.key()
        self.cache.put(key, output())
        now[0] = 4
        self.assertIsNotNone(self.cache.get(key))
        now[0] = 5
        self.assertIsNone(self.cache.get(key))
        self.assertEqual(self.cache._bytes, 0)

    def test_new_version_replaces_old_retained_pixels(self):
        self.cache.put(self.key(), output())
        self.path.write_bytes(b"new version")
        self.cache.put(self.key(), output())
        self.assertEqual(len(self.cache._entries), 1)
        self.assertEqual(self.cache._bytes, 8)

    def test_invalid_limits_rejected(self):
        for kwargs in ({"max_bytes": 0}, {"max_entries": True},
                       {"ttl_seconds": float("inf")}, {"ttl_seconds": -1}):
            with self.assertRaises(ValueError):
                ImageCache(**kwargs)


class RendererCacheTests(unittest.TestCase):
    """Real decoder processes and Gdk textures; only the enclosing view is omitted."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.cache = ImageCache()
        self.patch = mock.patch.object(cr2, "_IMAGE_CACHE", self.cache)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def render(self, renderer, path):
        ready, failures = [], []
        with mock.patch.object(renderer, "_create_view", side_effect=lambda texture, _result: texture):
            renderer.render(Gio.File.new_for_path(str(path)), Gio.FileInfo(), Gio.Cancellable(),
                            lambda texture, _subtitle: ready.append(texture), failures.append)
            spin_until(lambda: ready or failures)
        self.assertFalse(failures)
        self.assertIsInstance(ready[0], Gdk.Texture)
        return ready[0]

    def test_image_and_cr2_revisits_avoid_decode_and_modification_redecodes(self):
        for renderer, contents, replacement in (
            (ImageRenderer(), png(12, 8), png(20, 10)),
            (Cr2Renderer(), b"CR2" + build_jpeg(12, 8), b"CR2" + build_jpeg(20, 10)),
        ):
            with self.subTest(renderer=renderer.id):
                path = self.root / renderer.id
                path.write_bytes(contents)
                with mock.patch.object(renderer, "_prepare", wraps=renderer._prepare) as prepare:
                    first = self.render(renderer, path)
                    second = self.render(renderer, path)
                    self.assertEqual(prepare.call_count, 1)
                    self.assertEqual((first.get_width(), second.get_height()), (12, 8))
                    path.write_bytes(replacement)
                    third = self.render(renderer, path)
                    self.assertEqual(prepare.call_count, 2)
                    self.assertEqual((third.get_width(), third.get_height()), (20, 10))

    def test_metadata_and_cache_admission_are_off_the_main_thread(self):
        path = self.root / "sample.png"
        path.write_bytes(png(12, 8))
        threads = []
        original = self.cache.key_for

        def key_for(*args):
            threads.append(threading.get_ident())
            return original(*args)

        with mock.patch.object(self.cache, "key_for", side_effect=key_for):
            self.render(ImageRenderer(), path)
            self.render(ImageRenderer(), path)
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    def test_real_revisits_decode_again_after_cap_and_ttl_eviction(self):
        first, second = self.root / "first.png", self.root / "second.png"
        first.write_bytes(png(12, 8))
        second.write_bytes(png(13, 9))
        for limits in ({"max_entries": 1}, {"max_bytes": 500}):
            with self.subTest(limits=limits):
                now = [0.0]
                cache = ImageCache(**limits, ttl_seconds=5, clock=lambda: now[0])
                renderer = ImageRenderer()
                with (mock.patch.object(cr2, "_IMAGE_CACHE", cache),
                      mock.patch.object(renderer, "_prepare", wraps=renderer._prepare) as prepare):
                    self.render(renderer, first)
                    self.render(renderer, second)
                    self.render(renderer, first)
                    self.assertEqual(prepare.call_count, 3)
                    self.render(renderer, first)
                    self.assertEqual(prepare.call_count, 3)
                    now[0] = 5
                    self.render(renderer, first)
                    self.assertEqual(prepare.call_count, 4)

    def test_cached_delivery_keeps_slot_and_discards_cancelled_view(self):
        path = self.root / "sample.png"
        path.write_bytes(png(12, 8))
        renderer = ImageRenderer()
        self.render(renderer, path)
        slot, queued = TrackingSlot(), []
        cancelled = Gio.Cancellable()
        ready, failed = mock.Mock(), mock.Mock()
        with (mock.patch.object(cr2, "_WORKER_SLOT", slot),
              mock.patch.object(renderer, "_prepare") as decode,
              mock.patch.object(GLib, "idle_add", side_effect=lambda callback, *args: queued.append((callback, args)) or 1)):
            renderer.render(Gio.File.new_for_path(str(path)), Gio.FileInfo(), cancelled, ready, failed)
            spin_until(lambda: queued)
            self.assertFalse(slot.available)
            cancelled.cancel()
            callback, args = queued.pop()
            callback(*args)
            self.assertTrue(slot.available)
            decode.assert_not_called()
            ready.assert_not_called()
            failed.assert_not_called()

    def test_cancelled_decode_and_failure_are_not_cached(self):
        path = self.root / "sample.png"
        path.write_bytes(png(12, 8))
        renderer = ImageRenderer()
        cancelled = Gio.Cancellable()
        slot = TrackingSlot()

        def cancel_decode(*_args, **_kwargs):
            cancelled.cancel()
            return output()

        with (mock.patch.object(cr2, "_WORKER_SLOT", slot),
              mock.patch.object(renderer, "_prepare", side_effect=cancel_decode)):
            renderer.render(Gio.File.new_for_path(str(path)), Gio.FileInfo(), cancelled, mock.Mock(), mock.Mock())
            spin_until(lambda: slot.available)
        self.assertFalse(self.cache._entries)
        path.write_bytes(b"not an image")
        failures = []
        renderer.render(Gio.File.new_for_path(str(path)), Gio.FileInfo(), Gio.Cancellable(), mock.Mock(), failures.append)
        spin_until(lambda: failures)
        self.assertFalse(self.cache._entries)


if __name__ == "__main__":
    unittest.main()
