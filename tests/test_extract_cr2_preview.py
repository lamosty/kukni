# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = PROJECT_ROOT / "helpers" / "kukni-extract-preview.py"
SPEC = importlib.util.spec_from_file_location("extract_cr2_preview", HELPER_PATH)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def jpeg(width=640, height=480, frame_marker=0xC0, entropy=b"payload"):
    frame = (
        bytes((0xFF, frame_marker))
        + (7).to_bytes(2, "big")
        + bytes((8,))
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
    )
    scan = b"\xff\xda\x00\x02" + entropy.replace(b"\xff", b"\xff\x00")
    return helper.SOI + frame + scan + b"\xff\xd9"


class ParserTests(unittest.TestCase):
    def test_accepts_bounded_display_jpeg(self):
        data = jpeg(1920, 1080)

        parsed = helper.parse_jpeg(data, 0)

        self.assertEqual(parsed, helper.ParsedJpeg(len(data), 1920, 1080))

    def test_selects_largest_display_preview(self):
        small = jpeg(640, 480)
        large = jpeg(4096, 2731, frame_marker=0xC2)
        data = b"container" + small + b"metadata" + large + b"tail"

        preview = helper.find_best_preview(data)

        expected_start = data.index(large)
        self.assertEqual(preview.start, expected_start)
        self.assertEqual(preview.end, expected_start + len(large))
        self.assertEqual((preview.width, preview.height), (4096, 2731))

    def test_rejects_lossless_sensor_frame(self):
        self.assertIsNone(helper.parse_jpeg(jpeg(frame_marker=0xC3), 0))

    def test_rejects_missing_end_marker(self):
        self.assertIsNone(helper.parse_jpeg(jpeg()[:-2], 0))

    def test_rejects_conflicting_frame_headers(self):
        first = jpeg(640, 480)
        second_frame = jpeg(800, 600)[2:11]
        data = first[:11] + second_frame + first[11:]

        self.assertIsNone(helper.parse_jpeg(data, 0))

    def test_accepts_dimension_and_pixel_boundaries(self):
        self.assertIsNotNone(helper.parse_jpeg(jpeg(helper.MAX_DIMENSION, 1), 0))
        self.assertIsNotNone(helper.parse_jpeg(jpeg(10_000, 10_000), 0))

    def test_rejects_oversized_dimensions_and_pixel_count(self):
        self.assertIsNone(helper.parse_jpeg(jpeg(helper.MAX_DIMENSION + 1, 1), 0))
        self.assertIsNone(helper.parse_jpeg(jpeg(10_001, 10_000), 0))

    def test_enforces_shared_scan_byte_budget(self):
        budget = helper.ScanBudget(8)

        with self.assertRaises(helper.ScanLimitExceeded):
            helper.parse_jpeg(jpeg(entropy=b"a" * 128), 0, budget)

    def test_rejects_candidate_flood(self):
        data = (helper.SOI + b"\xff\xd9") * (helper.MAX_CANDIDATES + 1)

        with self.assertRaisesRegex(helper.ScanLimitExceeded, "too many"):
            helper.find_best_preview(data)


class InputTests(unittest.TestCase):
    def test_reads_regular_file_and_symlink_to_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "photo.cr2"
            link = Path(directory) / "linked.cr2"
            target.write_bytes(b"camera-data")
            link.symlink_to(target)

            self.assertEqual(helper.read_cr2(target), b"camera-data")
            self.assertEqual(helper.read_cr2(link), b"camera-data")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "pipe.cr2"
            os.mkfifo(fifo)

            with self.assertRaisesRegex(helper.PreviewError, "regular file"):
                helper.read_cr2(fifo)

    def test_rejects_file_larger_than_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.cr2"
            path.write_bytes(b"12345")

            with self.assertRaisesRegex(helper.PreviewError, "too large"):
                helper.read_cr2(path, max_file_size=4)

    def test_cli_writes_only_the_selected_jpeg(self):
        expected = jpeg(1600, 1200)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "name;still-not-a-shell.cr2"
            path.write_bytes(b"CR2-container" + expected + b"tail")

            result = subprocess.run(
                [str(HELPER_PATH), str(path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.stdout, expected)
            self.assertEqual(result.stderr, b"")


class CameraCorpusTests(unittest.TestCase):
    def test_optional_local_cr2_corpus(self):
        sample_dir = os.environ.get("CR2_SAMPLE_DIR")
        if not sample_dir:
            self.skipTest("set CR2_SAMPLE_DIR to run camera-corpus tests")

        samples = sorted(Path(sample_dir).glob("*.cr2"))
        self.assertTrue(samples, "CR2_SAMPLE_DIR contains no .cr2 files")
        for sample in samples:
            with self.subTest(sample=sample.name):
                data = helper.read_cr2(sample)
                preview = helper.find_best_preview(data)
                self.assertIsNotNone(preview)
                self.assertLessEqual(preview.width, helper.MAX_DIMENSION)
                self.assertLessEqual(preview.height, helper.MAX_DIMENSION)
                self.assertLessEqual(
                    preview.width * preview.height,
                    helper.MAX_PIXELS,
                )


if __name__ == "__main__":
    unittest.main()
