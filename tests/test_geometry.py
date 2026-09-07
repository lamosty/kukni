# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kukni.geometry import AdaptiveSizing, Size, meaningfully_different, preferred_window_size


class GeometryTests(unittest.TestCase):
    def test_image_aspects_get_distinct_content_first_windows(self):
        monitor = Size(1920, 1080)
        portrait = preferred_window_size('image', monitor, 600, 900)
        landscape = preferred_window_size('image', monitor, 1200, 800)
        square = preferred_window_size('image', monitor, 900, 900)
        panorama = preferred_window_size('image', monitor, 2400, 400)
        self.assertGreater(portrait.height, portrait.width)
        self.assertGreater(landscape.width, landscape.height)
        self.assertGreater(square.width, portrait.width)
        self.assertGreater(panorama.width / panorama.height, 3)
        self.assertEqual(len({portrait, landscape, square, panorama}), 4)

    def test_small_images_do_not_request_upscaling(self):
        self.assertEqual(preferred_window_size('image', Size(1920, 1080), 64, 48), Size(360, 280))

    def test_family_presets_are_not_one_fixed_size(self):
        monitor = Size(1920, 1080)
        families = ('text', 'document', 'folder', 'audio', 'video', 'pdf', 'fallback')
        self.assertEqual(len({preferred_window_size(kind, monitor) for kind in families}), len(families))
        self.assertLess(preferred_window_size('audio', monitor).height,
                        preferred_window_size('text', monitor).height)

    def test_all_results_fit_logical_monitor_bounds(self):
        # 960×540 also represents a 1920×1080 display at 200% scaling. This
        # pure function must never convert it back to physical pixel bounds.
        for monitor in (Size(1920, 1080), Size(960, 540), Size(800, 600),
                        Size(320, 240), Size(1, 1), Size(7680, 4320)):
            for kind in ('image', 'pdf', 'document', 'folder', 'audio', 'text', 'video', 'fallback'):
                for dimensions in ((12000, 8000), (800, 6000), (24000, 100), (0, 0)):
                    with self.subTest(monitor=monitor, kind=kind, dimensions=dimensions):
                        wanted = preferred_window_size(kind, monitor, *dimensions)
                        self.assertLessEqual(wanted.width, max(1, min(1800, int(monitor.width * .90))))
                        self.assertLessEqual(wanted.height, max(1, min(1440, int(monitor.height * .90))))

    def test_missing_and_invalid_intrinsics_use_safe_family_preset(self):
        monitor = Size(1920, 1080)
        self.assertEqual(preferred_window_size('pdf', monitor, -1, 5), Size(1040, 972))
        self.assertEqual(preferred_window_size('future-kind', monitor), Size(520, 360))
        with self.assertRaises(ValueError):
            Size(0, 100)

    def test_large_images_use_more_available_space_without_old_canvas_cap(self):
        normal = preferred_window_size('image', Size(1920, 1080), 6000, 4000)
        large = preferred_window_size('image', Size(2560, 1440), 6000, 4000)
        self.assertGreater(normal.width, 1120 + 24)
        self.assertGreater(normal.height, 860 + 100)
        self.assertGreater(large.width, normal.width)
        self.assertGreater(large.height, normal.height)
        self.assertAlmostEqual((large.width - 24) / (large.height - 100), 1.5, places=2)

    def test_pdf_is_a_readable_column_not_a_tiny_fit_page_image(self):
        monitor = Size(1920, 1080)
        portrait = preferred_window_size('pdf', monitor, 1273, 1800)
        very_tall = preferred_window_size('pdf', monitor, 300, 1800)
        landscape = preferred_window_size('pdf', monitor, 1800, 1273)
        self.assertGreaterEqual(portrait.width, 1000)
        self.assertGreaterEqual(portrait.height, 900)
        self.assertEqual(very_tall, portrait)
        self.assertGreater(landscape.width, portrait.width)

    def test_reading_views_grow_but_cards_stay_compact(self):
        monitor = Size(2560, 1440)
        self.assertGreaterEqual(preferred_window_size('text', monitor).height, 1000)
        self.assertGreater(preferred_window_size('document', monitor).width, 1100)
        self.assertEqual(preferred_window_size('folder', monitor), Size(700, 600))
        self.assertEqual(preferred_window_size('fallback', monitor), Size(520, 360))
        self.assertEqual(preferred_window_size('audio', monitor), Size(600, 320))

    def test_small_size_differences_do_not_bounce_window(self):
        self.assertFalse(meaningfully_different(Size(1000, 800), Size(1040, 830)))
        self.assertTrue(meaningfully_different(Size(1000, 800), Size(600, 900)))

    def test_programmatic_resize_can_settle_without_becoming_manual(self):
        policy = AdaptiveSizing()
        policy.observe(Size(640, 480), 1)
        self.assertTrue(policy.request(Size(1100, 830), 2))
        policy.observe(Size(900, 700), 2.1)
        policy.observe(Size(1100, 830), 2.3)
        policy.observe(Size(1100, 830), 3)
        self.assertFalse(policy.manual)
        self.assertTrue(policy.request(Size(600, 900), 4))

    def test_user_resize_freezes_later_automatic_size_requests(self):
        policy = AdaptiveSizing()
        policy.observe(Size(640, 480), 1)
        self.assertTrue(policy.request(Size(1100, 830), 2))
        policy.observe(Size(1100, 830), 2.3)
        policy.observe(Size(730, 510), 3)
        self.assertTrue(policy.manual)
        self.assertFalse(policy.request(Size(600, 900), 4))
        self.assertFalse(policy.request(Size(1100, 830), 5))


if __name__ == '__main__':
    unittest.main()
