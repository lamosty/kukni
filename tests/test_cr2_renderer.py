# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = PROJECT_ROOT / "helpers" / "kukni-cr2-worker.py"
EXTRACTOR_PATH = PROJECT_ROOT / "helpers" / "kukni-extract-preview.py"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib

import kukni.renderers.cr2 as cr2
from kukni.renderers.cr2 import (
    DEFAULT_LIMITS,
    Cr2Limits,
    Cr2PreviewCancelled,
    Cr2PreviewError,
    Cr2Renderer,
    Cr2WorkerOutput,
    Cr2WorkerResult,
    _default_helper_paths,
    _helper_paths_for_module,
    _resolve_cr2_runtime,
    build_cr2_worker_launch,
    parse_worker_result,
    run_cr2_worker,
    supports_cr2,
)


def build_jpeg(width: int = 10, height: int = 5) -> bytes:
    pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        width,
        height,
    )
    pixbuf.fill(0x336699FF)
    saved, jpeg = pixbuf.save_to_bufferv("jpeg", [], [])
    if not saved:
        raise AssertionError("test JPEG encoder failed")
    return bytes(jpeg)


def result_payload(
    *,
    width: int = 2,
    height: int = 1,
    source_width: int | None = None,
    source_height: int | None = None,
) -> bytes:
    source_width = width if source_width is None else source_width
    source_height = height if source_height is None else source_height
    return json.dumps(
        {
            "format": "rgba8",
            "height": height,
            "pixel_bytes": width * height * 4,
            "source_height": source_height,
            "source_width": source_width,
            "stride": width * 4,
            "version": 1,
            "width": width,
        },
        separators=(",", ":"),
    ).encode("ascii")


class FinishedProcess:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.pid = 424242

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class Cr2SupportTests(unittest.TestCase):
    @staticmethod
    def file_info(content_type: str, file_type=Gio.FileType.REGULAR):
        info = Gio.FileInfo()
        info.set_file_type(file_type)
        info.set_content_type(content_type)
        return info

    def test_detects_cr2_without_claiming_other_image_types(self):
        self.assertTrue(supports_cr2("photo.bin", "image/x-canon-cr2"))
        self.assertTrue(supports_cr2("PHOTO.CR2", "application/octet-stream"))
        self.assertFalse(supports_cr2("photo.cr2", "image/jpeg"))
        self.assertFalse(supports_cr2("photo.jpg", "image/jpeg"))

    def test_renderer_accepts_only_regular_local_cr2_files(self):
        renderer = Cr2Renderer()
        info = self.file_info("image/x-canon-cr2")

        self.assertTrue(
            renderer.supports(Gio.File.new_for_path("/tmp/photo.cr2"), info)
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_uri("https://invalid.example/photo.cr2"),
                info,
            )
        )
        self.assertFalse(
            renderer.supports(
                Gio.File.new_for_path("/tmp/folder.cr2"),
                self.file_info("image/x-canon-cr2", Gio.FileType.DIRECTORY),
            )
        )

    def test_renderer_falls_back_when_worker_boundary_is_unavailable(self):
        renderer = Cr2Renderer()
        info = self.file_info("image/x-canon-cr2")

        with mock.patch(
            "kukni.renderers.cr2.cr2_runtime_available",
            return_value=False,
        ):
            self.assertFalse(
                renderer.supports(Gio.File.new_for_path("/tmp/photo.cr2"), info)
            )


class Cr2ProtocolTests(unittest.TestCase):
    def test_accepts_exact_bounded_rgba_metadata(self):
        result = parse_worker_result(result_payload(width=20, height=10))

        self.assertEqual(
            result,
            Cr2WorkerResult(20, 10, 20, 10, 80, 800),
        )

    def test_rejects_unknown_duplicate_and_wrong_typed_fields(self):
        valid = json.loads(result_payload())
        cases = (
            b'{"version":1,"version":1}',
            json.dumps({**valid, "extra": 1}).encode(),
            json.dumps({**valid, "width": True}).encode(),
            json.dumps({**valid, "format": "jpeg"}).encode(),
        )
        for payload in cases:
            with self.subTest(payload=payload[:40]):
                with self.assertRaises(Cr2PreviewError):
                    parse_worker_result(payload)

    def test_rejects_inconsistent_and_oversized_dimensions(self):
        valid = json.loads(result_payload())
        cases = (
            {**valid, "stride": 7},
            {**valid, "pixel_bytes": 7},
            {**valid, "width": DEFAULT_LIMITS.max_render_edge + 1},
            {**valid, "source_width": DEFAULT_LIMITS.max_source_edge + 1},
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(Cr2PreviewError):
                    parse_worker_result(json.dumps(value).encode())

    def test_limit_configuration_is_positive_and_consistent(self):
        for arguments in (
            {"max_input_bytes": 0},
            {"max_pixel_bytes": True},
            {"wall_timeout_seconds": float("inf")},
            {"max_source_edge": 100, "max_render_edge": 101},
            {"max_source_pixels": 100, "max_render_pixels": 101},
            {"max_render_pixels": 100, "max_pixel_bytes": 399},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    Cr2Limits(**arguments)


class Cr2LayoutAndLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "input.cr2"
        self.input_path.write_bytes(b"CR2 data")
        self.input_fd = os.open(self.input_path, os.O_RDONLY | os.O_CLOEXEC)
        self.pixels_fd = os.open(
            self.root / "pixels",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        self.result_fd = os.open(
            self.root / "result",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )

    def tearDown(self):
        for descriptor in (self.input_fd, self.pixels_fd, self.result_fd):
            os.close(descriptor)
        self.temporary.cleanup()

    def test_source_layout_resolves_both_bundled_helpers(self):
        worker, extractor = _default_helper_paths()

        self.assertEqual(worker.resolve(), WORKER_PATH.resolve())
        self.assertEqual(extractor.resolve(), EXTRACTOR_PATH.resolve())
        runtime = _resolve_cr2_runtime(
            prlimit_path=None,
            python_path=None,
            worker_path=None,
        )
        self.assertEqual(Path(runtime[2]), WORKER_PATH.resolve())
        self.assertEqual(Path(runtime[3]), EXTRACTOR_PATH.resolve())

    def test_temp_installed_layout_resolves_helpers_beside_each_other(self):
        module = (
            self.root
            / "prefix/lib/kukni/src/kukni/renderers/cr2.py"
        )
        module.parent.mkdir(parents=True)
        module.write_text("# installed module\n", encoding="utf-8")
        expected_root = self.root / "prefix/lib/kukni/helpers"
        expected_root.mkdir(parents=True)
        worker = expected_root / WORKER_PATH.name
        extractor = expected_root / EXTRACTOR_PATH.name
        shutil.copyfile(WORKER_PATH, worker)
        shutil.copyfile(EXTRACTOR_PATH, extractor)

        self.assertEqual(
            _helper_paths_for_module(module),
            (worker, extractor),
        )
        runtime = _resolve_cr2_runtime(
            prlimit_path="/usr/bin/prlimit",
            python_path=sys.executable,
            worker_path=worker,
        )
        self.assertEqual(Path(runtime[2]), worker.resolve())
        self.assertEqual(Path(runtime[3]), extractor.resolve())

    def test_launch_is_fixed_and_hard_limits_tasks_before_python(self):
        launch = build_cr2_worker_launch(
            prlimit_path="/usr/bin/prlimit",
            python_path=os.fspath(Path(sys.executable).resolve()),
            worker_path=os.fspath(WORKER_PATH.resolve()),
            input_fd=self.input_fd,
            pixels_fd=self.pixels_fd,
            result_fd=self.result_fd,
        )

        self.assertEqual(launch.argv[0], "/usr/bin/prlimit")
        self.assertIn(f"--as={DEFAULT_LIMITS.max_address_space_bytes}", launch.argv)
        self.assertIn(f"--cpu={DEFAULT_LIMITS.max_cpu_seconds}", launch.argv)
        self.assertIn(f"--nofile={DEFAULT_LIMITS.max_open_files}", launch.argv)
        self.assertIn("--nproc=0:0", launch.argv)
        self.assertIn("--core=0", launch.argv)
        self.assertEqual(
            launch.pass_fds,
            (self.input_fd, self.pixels_fd, self.result_fd),
        )
        python_index = launch.argv.index(os.fspath(Path(sys.executable).resolve()))
        self.assertEqual(launch.argv[python_index + 1 : python_index + 3], ("-I", "-B"))
        self.assertNotIn(os.fspath(self.input_path), launch.argv)


class Cr2WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.jpeg = build_jpeg()
        self.source = self.root / "private;$(not-a-shell).cr2"
        self.source.write_bytes(b"Canon CR2 container" + self.jpeg + b"tail")

    def tearDown(self):
        self.temporary.cleanup()

    def test_real_worker_returns_only_tightly_packed_rgba(self):
        captured = {}

        def process_factory(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return subprocess.Popen(argv, **kwargs)

        output = run_cr2_worker(self.source, process_factory=process_factory)

        self.assertEqual(
            output.result,
            Cr2WorkerResult(10, 5, 10, 5, 40, 200),
        )
        self.assertEqual(len(output.pixels), 200)
        texture = cr2.Gdk.MemoryTexture.new(
            output.result.width,
            output.result.height,
            cr2.Gdk.MemoryFormat.R8G8B8A8,
            GLib.Bytes.new(output.pixels),
            output.result.stride,
        )
        self.assertEqual((texture.get_width(), texture.get_height()), (10, 5))
        self.assertNotIn(os.fspath(self.source), captured["argv"])
        self.assertFalse(captured["kwargs"]["shell"])
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertEqual(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"]["stdout"], subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"]["stderr"], subprocess.DEVNULL)
        self.assertEqual(
            captured["kwargs"]["env"],
            {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )

    def test_real_worker_downscales_inside_the_process(self):
        source = self.root / "large-preview.cr2"
        source.write_bytes(b"CR2" + build_jpeg(500, 250) + b"tail")
        limits = replace(
            DEFAULT_LIMITS,
            max_render_edge=100,
            max_render_pixels=10_000,
            max_pixel_bytes=40_000,
        )

        output = run_cr2_worker(source, limits=limits)

        self.assertEqual(
            (output.result.source_width, output.result.source_height),
            (500, 250),
        )
        self.assertLessEqual(output.result.width, 100)
        self.assertLessEqual(output.result.height, 100)
        self.assertEqual(len(output.pixels), output.result.pixel_bytes)

    def test_invalid_inputs_are_rejected_before_process_start(self):
        factory = mock.Mock()
        empty = self.root / "empty.cr2"
        empty.write_bytes(b"")
        oversized = self.root / "oversized.cr2"
        oversized.write_bytes(b"12345")
        for path, limits, message in (
            (self.root, DEFAULT_LIMITS, "regular local file"),
            (empty, DEFAULT_LIMITS, "empty"),
            (oversized, replace(DEFAULT_LIMITS, max_input_bytes=4), "input size"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(Cr2PreviewError, message):
                    run_cr2_worker(path, limits=limits, process_factory=factory)
        factory.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_fifo_is_rejected_without_blocking_or_spawning(self):
        fifo = self.root / "pipe.cr2"
        os.mkfifo(fifo)
        factory = mock.Mock()

        with self.assertRaisesRegex(Cr2PreviewError, "regular local file"):
            run_cr2_worker(fifo, process_factory=factory)
        factory.assert_not_called()

    def test_timeout_and_cancellation_kill_and_reap_worker(self):
        worker = self.root / "kukni-cr2-worker.py"
        worker.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        shutil.copyfile(EXTRACTOR_PATH, self.root / EXTRACTOR_PATH.name)
        processes = []

        def process_factory(argv, **kwargs):
            process = subprocess.Popen(argv, **kwargs)
            processes.append(process)
            return process

        with self.assertRaisesRegex(Cr2PreviewError, "timed out"):
            run_cr2_worker(
                self.source,
                limits=replace(DEFAULT_LIMITS, wall_timeout_seconds=0.05),
                worker_path=worker,
                process_factory=process_factory,
            )
        self.assertIsNotNone(processes[-1].poll())

        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 3

        with self.assertRaises(Cr2PreviewCancelled):
            run_cr2_worker(
                self.source,
                worker_path=worker,
                process_factory=process_factory,
                cancelled=cancelled,
            )
        self.assertIsNotNone(processes[-1].poll())

    def test_rejects_worker_output_if_input_mutates(self):
        def process_factory(_argv, **kwargs):
            _input_fd, pixels_fd, metadata_fd = kwargs["pass_fds"]
            os.write(pixels_fd, b"12345678")
            os.write(metadata_fd, result_payload())
            self.source.write_bytes(self.source.read_bytes() + b"changed")
            return FinishedProcess()

        with self.assertRaisesRegex(Cr2PreviewError, "changed"):
            run_cr2_worker(self.source, process_factory=process_factory)

    def test_rejects_malformed_or_mismatched_worker_outputs(self):
        cases = (
            (b"12345678", b'{"attacker":"text"}'),
            (b"1234", result_payload()),
            (b"12345678", result_payload(width=3, height=1)),
        )
        for pixels, metadata in cases:
            with self.subTest(pixels=len(pixels), metadata=metadata[:20]):
                def process_factory(_argv, **kwargs):
                    os.write(kwargs["pass_fds"][1], pixels)
                    os.write(kwargs["pass_fds"][2], metadata)
                    return FinishedProcess()

                with self.assertRaises(Cr2PreviewError) as raised:
                    run_cr2_worker(self.source, process_factory=process_factory)
                self.assertNotIn("attacker", str(raised.exception))


class TrackingSlot:
    def __init__(self, available: bool = True):
        self.available = available
        self.release_calls = 0

    def acquire(self, *, blocking: bool):
        self.asserted_nonblocking = not blocking
        if not self.available:
            return False
        self.available = False
        return True

    def release(self):
        if self.available:
            raise AssertionError("slot released twice")
        self.available = True
        self.release_calls += 1


class InlineThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


class FailingThread(InlineThread):
    def start(self):
        raise RuntimeError("thread unavailable")


class Cr2RendererLifecycleTests(unittest.TestCase):
    @staticmethod
    def output() -> Cr2WorkerOutput:
        result = Cr2WorkerResult(2, 1, 20, 10, 8, 8)
        return Cr2WorkerOutput(result, b"12345678")

    def test_busy_slot_falls_back_without_starting_a_thread(self):
        slot = TrackingSlot(available=False)
        failed = mock.Mock()

        with (
            mock.patch.object(cr2, "_WORKER_SLOT", slot),
            mock.patch("kukni.renderers.cr2.threading.Thread") as thread,
            mock.patch(
                "kukni.renderers.cr2.GLib.idle_add",
                side_effect=lambda callback, *args: callback(*args),
            ),
        ):
            Cr2Renderer().render(
                Gio.File.new_for_path("/tmp/photo.cr2"),
                Gio.FileInfo(),
                Gio.Cancellable(),
                mock.Mock(),
                failed,
            )

        thread.assert_not_called()
        failed.assert_called_once_with(
            "CR2 preview is busy; showing file details instead"
        )

    def test_thread_start_failure_releases_the_slot(self):
        slot = TrackingSlot()
        failed = mock.Mock()

        with (
            mock.patch.object(cr2, "_WORKER_SLOT", slot),
            mock.patch("kukni.renderers.cr2.threading.Thread", FailingThread),
            mock.patch(
                "kukni.renderers.cr2.GLib.idle_add",
                side_effect=lambda callback, *args: callback(*args),
            ),
        ):
            Cr2Renderer().render(
                Gio.File.new_for_path("/tmp/photo.cr2"),
                Gio.FileInfo(),
                Gio.Cancellable(),
                mock.Mock(),
                failed,
            )

        self.assertEqual(slot.release_calls, 1)
        failed.assert_called_once_with(
            "The CR2 preview worker thread could not be started"
        )

    def test_slot_is_held_until_queued_delivery_consumes_payload(self):
        slot = TrackingSlot()
        queued = []
        ready = mock.Mock()
        failed = mock.Mock()
        view = mock.sentinel.view

        def queue(callback, *arguments):
            queued.append((callback, arguments))
            return 17

        with (
            mock.patch.object(cr2, "_WORKER_SLOT", slot),
            mock.patch("kukni.renderers.cr2.threading.Thread", InlineThread),
            mock.patch(
                "kukni.renderers.cr2.run_cr2_worker",
                return_value=self.output(),
            ),
            mock.patch("kukni.renderers.cr2.GLib.idle_add", side_effect=queue),
            mock.patch("kukni.renderers.cr2.GLib.Bytes.new"),
            mock.patch("kukni.renderers.cr2.Gdk.MemoryTexture.new"),
            mock.patch("kukni.renderers.cr2.Cr2PreviewView", return_value=view),
        ):
            Cr2Renderer().render(
                Gio.File.new_for_path("/tmp/photo.cr2"),
                Gio.FileInfo(),
                Gio.Cancellable(),
                ready,
                failed,
            )
            self.assertEqual(slot.release_calls, 0)
            self.assertFalse(slot.available)
            callback, arguments = queued.pop()
            callback(*arguments)

        self.assertEqual(slot.release_calls, 1)
        self.assertTrue(slot.available)
        ready.assert_called_once_with(
            view,
            "Canon CR2 image · embedded JPEG · fit",
        )
        failed.assert_not_called()

    def test_cancelled_queued_delivery_discards_payload_and_releases(self):
        slot = TrackingSlot()
        queued = []
        cancellable = Gio.Cancellable()

        with (
            mock.patch.object(cr2, "_WORKER_SLOT", slot),
            mock.patch("kukni.renderers.cr2.threading.Thread", InlineThread),
            mock.patch(
                "kukni.renderers.cr2.run_cr2_worker",
                return_value=self.output(),
            ),
            mock.patch(
                "kukni.renderers.cr2.GLib.idle_add",
                side_effect=lambda callback, *args: (
                    queued.append((callback, args)) or 9
                ),
            ),
        ):
            Cr2Renderer().render(
                Gio.File.new_for_path("/tmp/photo.cr2"),
                Gio.FileInfo(),
                cancellable,
                mock.Mock(),
                mock.Mock(),
            )
            cancellable.cancel()
            callback, arguments = queued.pop()
            callback(*arguments)

        self.assertEqual(slot.release_calls, 1)

    def test_failed_queued_delivery_reports_error_and_releases(self):
        slot = TrackingSlot()
        queued = []
        failed = mock.Mock()

        with (
            mock.patch.object(cr2, "_WORKER_SLOT", slot),
            mock.patch("kukni.renderers.cr2.threading.Thread", InlineThread),
            mock.patch(
                "kukni.renderers.cr2.run_cr2_worker",
                return_value=self.output(),
            ),
            mock.patch(
                "kukni.renderers.cr2.GLib.idle_add",
                side_effect=lambda callback, *args: (
                    queued.append((callback, args)) or 9
                ),
            ),
            mock.patch(
                "kukni.renderers.cr2.GLib.Bytes.new",
                side_effect=TypeError("invalid bytes"),
            ),
        ):
            Cr2Renderer().render(
                Gio.File.new_for_path("/tmp/photo.cr2"),
                Gio.FileInfo(),
                Gio.Cancellable(),
                mock.Mock(),
                failed,
            )
            callback, arguments = queued.pop()
            callback(*arguments)

        self.assertEqual(slot.release_calls, 1)
        failed.assert_called_once_with(
            "The decoded CR2 preview could not be displayed"
        )

    def test_idle_add_failure_releases_before_reporting_error(self):
        slot = TrackingSlot()
        attempts = 0

        def idle_add(_callback, *_arguments):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("main context unavailable")
            return 1

        with (
            mock.patch.object(cr2, "_WORKER_SLOT", slot),
            mock.patch("kukni.renderers.cr2.threading.Thread", InlineThread),
            mock.patch(
                "kukni.renderers.cr2.run_cr2_worker",
                return_value=self.output(),
            ),
            mock.patch("kukni.renderers.cr2.GLib.idle_add", side_effect=idle_add),
        ):
            Cr2Renderer().render(
                Gio.File.new_for_path("/tmp/photo.cr2"),
                Gio.FileInfo(),
                Gio.Cancellable(),
                mock.Mock(),
                mock.Mock(),
            )

        self.assertEqual(slot.release_calls, 1)
        self.assertEqual(attempts, 2)

    def test_parent_module_never_imports_an_encoded_image_decoder(self):
        self.assertNotIn("GdkPixbuf", cr2.__dict__)


if __name__ == "__main__":
    unittest.main()
