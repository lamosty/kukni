# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import ast
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = PROJECT_ROOT / "helpers" / "kukni-cr2-worker.py"
EXTRACTOR_PATH = PROJECT_ROOT / "helpers" / "kukni-extract-preview.py"

WORKER_SPEC = importlib.util.spec_from_file_location("kukni_cr2_worker", WORKER_PATH)
worker = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules[WORKER_SPEC.name] = worker
WORKER_SPEC.loader.exec_module(worker)

EXTRACTOR_SPEC = importlib.util.spec_from_file_location(
    "kukni_cr2_extractor_test",
    EXTRACTOR_PATH,
)
extractor = importlib.util.module_from_spec(EXTRACTOR_SPEC)
EXTRACTOR_SPEC.loader.exec_module(extractor)


def arguments(input_fd: int = 3, pixels_fd: int = 4, result_fd: int = 5):
    return {
        "input_fd": input_fd,
        "pixels_fd": pixels_fd,
        "result_fd": result_fd,
        "max_input_bytes": worker.MAX_INPUT_BYTES,
        "max_jpeg_bytes": worker.MAX_JPEG_BYTES,
        "max_source_edge": worker.MAX_SOURCE_EDGE,
        "max_source_pixels": worker.MAX_SOURCE_PIXELS,
        "max_render_edge": worker.MAX_RENDER_EDGE,
        "max_render_pixels": worker.MAX_RENDER_PIXELS,
        "max_pixel_bytes": worker.MAX_PIXEL_BYTES,
        "max_result_bytes": worker.MAX_RESULT_BYTES,
        "max_address_space_bytes": worker.MAX_ADDRESS_SPACE_BYTES,
        "max_cpu_seconds": worker.MAX_CPU_SECONDS,
        "max_open_files": worker.MAX_OPEN_FILES,
    }


def argv_for(values: dict[str, int]) -> list[str]:
    result = ["kukni-cr2-worker.py"]
    for option in worker._ARGUMENTS:
        key = option.removeprefix("--").replace("-", "_")
        result.extend((option, str(values[key])))
    return result


def build_jpeg(width: int = 10, height: int = 5) -> bytes:
    pixbuf = worker.GdkPixbuf.Pixbuf.new(
        worker.GdkPixbuf.Colorspace.RGB,
        False,
        8,
        width,
        height,
    )
    pixbuf.fill(0x6699CCFF)
    saved, data = pixbuf.save_to_bufferv("jpeg", [], [])
    if not saved:
        raise AssertionError("test JPEG encoder failed")
    return bytes(data)


class StrictCliTests(unittest.TestCase):
    def test_accepts_only_the_exact_ordered_parent_contract(self):
        values = arguments()
        self.assertEqual(worker.parse_arguments(argv_for(values)), values)

        valid = argv_for(values)
        for malformed in (
            valid[:-2],
            valid + ["--extra", "1"],
            [valid[0], valid[3], valid[4], valid[1], valid[2], *valid[5:]],
            [*valid[:-1], "+64"],
            [*valid[:-1], "064"],
        ):
            with self.subTest(malformed=malformed[-4:]):
                with self.assertRaises(worker.WorkerError):
                    worker.parse_arguments(list(malformed))

    def test_rejects_duplicate_descriptors_and_inconsistent_limits(self):
        for changes in (
            {"pixels_fd": 3},
            {"max_input_bytes": 0},
            {"max_input_bytes": worker.MAX_INPUT_BYTES + 1},
            {"max_source_edge": 10, "max_render_edge": 11},
            {"max_render_pixels": 100, "max_pixel_bytes": 399},
        ):
            values = {**arguments(), **changes}
            with self.subTest(changes=changes):
                with self.assertRaises(worker.WorkerError):
                    worker.parse_arguments(argv_for(values))


class WorkerBoundaryTests(unittest.TestCase):
    def test_fails_closed_when_no_new_privileges_cannot_be_enabled(self):
        with mock.patch.object(worker.ctypes, "CDLL", side_effect=OSError("no prctl")):
            with self.assertRaises(worker.WorkerError):
                worker.enable_no_new_privileges()

    def test_rejects_missing_or_infinite_hard_process_limits(self):
        values = arguments()
        with (
            mock.patch.object(worker.os, "geteuid", return_value=1000),
            mock.patch.object(
                worker.resource,
                "getrlimit",
                return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY),
            ),
        ):
            with self.assertRaises(worker.WorkerError):
                worker.verify_process_limits(values)

        def limits(resource_id):
            if resource_id == resource.RLIMIT_NPROC:
                return 1, 1
            return 0, 0

        with (
            mock.patch.object(worker.os, "geteuid", return_value=1000),
            mock.patch.object(worker.resource, "getrlimit", side_effect=limits),
        ):
            with self.assertRaisesRegex(worker.WorkerError, "task creation"):
                worker.verify_process_limits(values)

    def test_rejects_root_where_nproc_would_not_enforce_the_boundary(self):
        with mock.patch.object(worker.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(worker.WorkerError, "unprivileged"):
                worker.verify_process_limits(arguments())

    def test_no_new_privileges_is_set_before_untrusted_decode(self):
        order = []
        values = arguments()
        pixbuf = mock.sentinel.pixbuf
        with (
            mock.patch.object(worker, "parse_arguments", return_value=values),
            mock.patch.object(
                worker,
                "validate_descriptors",
                side_effect=lambda _values: order.append("descriptors"),
            ),
            mock.patch.object(
                worker,
                "verify_process_limits",
                side_effect=lambda _values: order.append("limits"),
            ),
            mock.patch.object(
                worker,
                "load_extractor",
                side_effect=lambda: (
                    order.append("extractor") or mock.sentinel.extractor
                ),
            ),
            mock.patch.object(
                worker,
                "enable_no_new_privileges",
                side_effect=lambda: order.append("no-new-privileges"),
            ),
            mock.patch.object(
                worker,
                "extract_and_decode",
                side_effect=lambda *_args: order.append("decode") or (pixbuf, 10, 5),
            ),
            mock.patch.object(
                worker,
                "write_outputs",
                side_effect=lambda *_args: order.append("write"),
            ),
        ):
            self.assertEqual(worker.run(["worker"]), 0)

        self.assertEqual(
            order,
            [
                "descriptors",
                "limits",
                "extractor",
                "no-new-privileges",
                "decode",
                "write",
            ],
        )

    def test_worker_source_has_no_process_or_thread_creation_path(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertTrue(
            imported_roots.isdisjoint(
                {"multiprocessing", "subprocess", "threading", "concurrent"}
            )
        )


class DecodeAndOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.cr2"
        self.source.write_bytes(b"CR2" + build_jpeg() + b"tail")
        self.input_fd = os.open(self.source, os.O_RDONLY | os.O_CLOEXEC)
        self.pixels_path = self.root / "pixels"
        self.result_path = self.root / "result"
        self.pixels_fd = os.open(
            self.pixels_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        self.result_fd = os.open(
            self.result_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        self.values = arguments(self.input_fd, self.pixels_fd, self.result_fd)

    def tearDown(self):
        for descriptor in (self.input_fd, self.pixels_fd, self.result_fd):
            os.close(descriptor)
        self.temporary.cleanup()

    def test_reuses_extractor_and_decodes_to_validated_rgba(self):
        pixbuf, source_width, source_height = worker.extract_and_decode(
            self.values,
            extractor,
        )

        self.assertEqual((source_width, source_height), (10, 5))
        self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (10, 5))
        self.assertEqual(pixbuf.get_n_channels(), 4)
        self.assertTrue(pixbuf.get_has_alpha())

    def test_writes_tight_pixels_then_tiny_exact_metadata(self):
        pixbuf, source_width, source_height = worker.extract_and_decode(
            self.values,
            extractor,
        )

        worker.write_outputs(self.values, pixbuf, source_width, source_height)

        pixels = self.pixels_path.read_bytes()
        metadata = json.loads(self.result_path.read_bytes())
        self.assertEqual(len(pixels), 10 * 5 * 4)
        self.assertEqual(
            set(metadata),
            {
                "format",
                "height",
                "pixel_bytes",
                "source_height",
                "source_width",
                "stride",
                "version",
                "width",
            },
        )
        self.assertEqual(metadata["pixel_bytes"], len(pixels))
        self.assertEqual(metadata["stride"], 10 * 4)

    def test_descriptor_validation_requires_fresh_distinct_regular_files(self):
        worker.validate_descriptors(self.values)
        os.write(self.pixels_fd, b"not fresh")
        with self.assertRaises(worker.WorkerError):
            worker.validate_descriptors(self.values)

        duplicate = {
            **self.values,
            "pixels_fd": self.result_fd,
        }
        with self.assertRaises(worker.WorkerError):
            worker.validate_descriptors(duplicate)


if __name__ == "__main__":
    unittest.main()
