# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from dataclasses import replace
from pathlib import Path
import sys
import struct
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gi.repository import Gio
from kukni.renderers.cr2 import Cr2PreviewError, Cr2PreviewCancelled
from kukni.renderers.image import IMAGE_LIMITS, run_image_worker, supports_image
from kukni.renderers.registry import default_registry
from image_fixtures import png, WEBP, GIF
from test_cr2_worker_helper import build_jpeg, worker as fixture_worker


class ImageRoutingTests(unittest.TestCase):
    def test_default_registry_routes_common_images(self):
        registry = default_registry()
        for suffix, kind in [('png', 'image/png'), ('jpeg', 'image/jpeg'),
                             ('webp', 'image/webp'), ('gif', 'image/gif'),
                             ('tiff', 'image/tiff')]:
            info = Gio.FileInfo()
            info.set_file_type(Gio.FileType.REGULAR)
            info.set_content_type(kind)
            selected = registry.select(Gio.File.new_for_path('/tmp/sample.' + suffix), info)
            self.assertEqual(selected.id, 'image')

    def test_visual_xml_is_not_claimed_as_text(self):
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.REGULAR)
        info.set_content_type('image/svg+xml')
        self.assertIsNone(default_registry().select(Gio.File.new_for_path('/tmp/picture.svg'), info))

    def test_suffix_fallback_does_not_override_a_known_other_type(self):
        self.assertTrue(supports_image('PHOTO.PNG', 'application/octet-stream'))
        self.assertFalse(supports_image('script.png', 'application/x-executable'))
        self.assertFalse(supports_image('graphic.svg', 'image/svg+xml'))


class RealImageWorkerTests(unittest.TestCase):
    """Mandatory successful decoding, never a pass-by-fallback or fake worker."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'image'

    def tearDown(self):
        self.temporary.cleanup()

    def test_png_jpeg_webp_and_gif_decode_in_real_workers(self):
        for data, dimensions in [(png(), (120, 80)), (build_jpeg(12, 8), (12, 8)),
                                 (WEBP, (12, 8)), (GIF, (12, 8))]:
            with self.subTest(dimensions=dimensions, signature=data[:8]):
                self.path.write_bytes(data)
                output = run_image_worker(self.path)
                self.assertEqual((output.result.width, output.result.height), dimensions)
                self.assertEqual(len(output.pixels), dimensions[0] * dimensions[1] * 4)
                self.assertEqual(output.pixels[3], 255)
                self.assertEqual(self.path.read_bytes(), data)

    def test_preserves_transparency(self):
        self.path.write_bytes(png(5, 3, transparent=True))
        self.assertEqual(run_image_worker(self.path).pixels[3::4], bytes([128]) * 15)

    def test_tiff_bmp_and_ico_decode_in_real_workers(self):
        pixbuf = fixture_worker.GdkPixbuf.Pixbuf.new(
            fixture_worker.GdkPixbuf.Colorspace.RGB, False, 8, 12, 8
        )
        pixbuf.fill(0x336699FF)
        for kind in ('tiff', 'bmp', 'ico'):
            with self.subTest(kind=kind):
                saved, data = pixbuf.save_to_bufferv(kind, [], [])
                self.assertTrue(saved)
                self.path.write_bytes(bytes(data))
                result = run_image_worker(self.path).result
                self.assertEqual((result.width, result.height), (12, 8))

    def test_jpeg_orientation_also_updates_intrinsic_sizing_metadata(self):
        # A synthetic TIFF orientation tag inside JPEG APP1, not a private EXIF
        # sample. Orientation 6 rotates a landscape image into a portrait.
        exif = (b'Exif\0\0II*\0' + struct.pack('<I', 8)
                + struct.pack('<H', 1)
                + struct.pack('<HHIHHI', 0x112, 3, 1, 6, 0, 0))
        jpeg = build_jpeg(12, 8)
        self.path.write_bytes(jpeg[:2] + b'\xff\xe1'
                              + struct.pack('>H', len(exif) + 2) + exif + jpeg[2:])
        result = run_image_worker(self.path).result
        self.assertEqual((result.width, result.height), (8, 12))
        self.assertEqual((result.source_width, result.source_height), (8, 12))

    def test_downscales_and_retains_original_dimensions(self):
        self.path.write_bytes(png(120, 80))
        limits = replace(IMAGE_LIMITS, max_render_edge=60, max_render_pixels=3600)
        result = run_image_worker(self.path, limits=limits).result
        self.assertEqual((result.width, result.height), (60, 40))
        self.assertEqual((result.source_width, result.source_height), (120, 80))

    def test_malformed_and_non_raster_bytes_fail_without_output(self):
        for data in [b'<svg xmlns="http://www.w3.org/2000/svg"/>', b'\x89PNG\r\n\x1a\ninvalid', b'#!/bin/sh\nexit 0']:
            self.path.write_bytes(data)
            with self.assertRaises(Cr2PreviewError):
                run_image_worker(self.path)

    def test_cancelled_request_does_not_open_file(self):
        with self.assertRaises(Cr2PreviewCancelled):
            run_image_worker(self.path, cancelled=lambda: True)

    def test_source_and_input_limits_are_enforced(self):
        self.path.write_bytes(png(30, 20))
        with self.assertRaises(Cr2PreviewError):
            run_image_worker(self.path, limits=replace(IMAGE_LIMITS, max_input_bytes=10))
        limits = replace(IMAGE_LIMITS, max_source_edge=20, max_render_edge=20,
                         max_source_pixels=400, max_render_pixels=400)
        with self.assertRaises(Cr2PreviewError):
            run_image_worker(self.path, limits=limits)


if __name__ == '__main__':
    unittest.main()
