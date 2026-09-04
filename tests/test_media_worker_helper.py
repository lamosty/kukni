# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.media_worker import parse_worker_result, validate_frame_bytes


def _load_helper():
    path = PROJECT_ROOT / "helpers" / "kukni-media-worker.py"
    spec = importlib.util.spec_from_file_location("kukni_media_worker_helper", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("media worker helper could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


worker = _load_helper()


def valid_argv(**overrides):
    values = {
        "input": worker.INPUT_PATH,
        "frame_output": worker.FRAME_OUTPUT_PATH,
        "result_output": worker.RESULT_OUTPUT_PATH,
        "max_edge": "1800",
        "max_frame_bytes": str(1800 * 1800 * 4),
        "max_result_bytes": str(64 * 1024),
        "max_input_bytes": str(16 * 1024 * 1024 * 1024),
    }
    values.update(overrides)
    return [
        "--input",
        values["input"],
        "--frame-output",
        values["frame_output"],
        "--result-output",
        values["result_output"],
        "--max-edge",
        values["max_edge"],
        "--max-frame-bytes",
        values["max_frame_bytes"],
        "--max-result-bytes",
        values["max_result_bytes"],
        "--max-input-bytes",
        values["max_input_bytes"],
    ]


class StrictCliTests(unittest.TestCase):
    def test_accepts_the_exact_parent_command(self):
        limits = worker.parse_cli(valid_argv())

        self.assertEqual(limits.max_edge_pixels, 1800)
        self.assertEqual(limits.max_frame_bytes, 12_960_000)
        self.assertEqual(limits.max_result_bytes, 65_536)
        self.assertEqual(limits.max_input_bytes, 17_179_869_184)

    def test_rejects_changed_paths_order_duplicates_and_extra_arguments(self):
        cases = [
            valid_argv(input="/tmp/media"),
            valid_argv(frame_output="/tmp/frame"),
            valid_argv(result_output="/tmp/result"),
            valid_argv()[2:4] + valid_argv()[:2] + valid_argv()[4:],
            valid_argv()[:-2] + ["--max-result-bytes", "1"],
            valid_argv() + ["--help"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(worker.WorkerError):
                    worker.parse_cli(arguments)

    def test_rejects_noncanonical_out_of_range_and_inconsistent_limits(self):
        cases = [
            valid_argv(max_edge="0"),
            valid_argv(max_edge="01800"),
            valid_argv(max_edge="+1800"),
            valid_argv(max_edge="1801"),
            valid_argv(max_frame_bytes=str(12_960_001)),
            valid_argv(max_input_bytes=str(16 * 1024 * 1024 * 1024 + 1)),
            valid_argv(max_edge="100", max_frame_bytes="399"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(worker.WorkerError):
                    worker.parse_cli(arguments)


class RowPackingTests(unittest.TestCase):
    def test_copies_tightly_packed_rgba(self):
        frame = bytes(range(24))
        self.assertEqual(
            worker.pack_rgba_rows(
                frame,
                width=3,
                height=2,
                stride=12,
                offset=0,
                max_frame_bytes=24,
            ),
            frame,
        )

    def test_removes_row_padding_and_honours_offset(self):
        data = b"xx" + b"ABCD" + b"pad!" + b"EFGH" + b"tail"
        self.assertEqual(
            worker.pack_rgba_rows(
                data,
                width=1,
                height=2,
                stride=8,
                offset=2,
                max_frame_bytes=8,
            ),
            b"ABCDEFGH",
        )

    def test_supports_a_bounded_negative_stride(self):
        data = b"EFGHpad!ABCD"
        self.assertEqual(
            worker.pack_rgba_rows(
                data,
                width=1,
                height=2,
                stride=-8,
                offset=8,
                max_frame_bytes=8,
            ),
            b"ABCDEFGH",
        )

    def test_rejects_truncated_small_stride_and_oversized_frames(self):
        cases = [
            {
                "data": b"1234567",
                "width": 1,
                "height": 2,
                "stride": 4,
                "offset": 0,
                "max_frame_bytes": 8,
            },
            {
                "data": b"12345678",
                "width": 1,
                "height": 2,
                "stride": 3,
                "offset": 0,
                "max_frame_bytes": 8,
            },
            {
                "data": b"12345678",
                "width": 1,
                "height": 2,
                "stride": 4,
                "offset": 0,
                "max_frame_bytes": 7,
            },
            {
                "data": b"12345678",
                "width": 1,
                "height": 2,
                "stride": -4,
                "offset": 0,
                "max_frame_bytes": 8,
            },
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(worker.WorkerError):
                    worker.pack_rgba_rows(**arguments)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.limits = worker.parse_cli(valid_argv())

    def test_video_protocol_matches_the_parent_contract(self):
        frame = bytes(range(24))
        raw_frame, payload = worker.encode_result(
            worker.DecodedMedia(
                kind="video",
                has_video=True,
                has_audio=True,
                duration_usec=2_000_000,
                width=3,
                height=2,
                frame=frame,
            ),
            self.limits,
        )

        parsed = parse_worker_result(payload)
        validate_frame_bytes(raw_frame, parsed)
        self.assertEqual(parsed.frame_format, "rgba8")
        self.assertEqual(parsed.frame_bytes, 24)
        self.assertEqual(set(json.loads(payload)), {
            "version", "kind", "has_video", "has_audio", "duration_usec",
            "width", "height", "frame_format", "frame_bytes",
        })

    def test_audio_protocol_has_no_frame(self):
        raw_frame, payload = worker.encode_result(
            worker.DecodedMedia(
                kind="audio",
                has_video=False,
                has_audio=True,
                duration_usec=750_000,
                width=0,
                height=0,
                frame=b"",
            ),
            self.limits,
        )

        parsed = parse_worker_result(payload)
        validate_frame_bytes(raw_frame, parsed)
        self.assertFalse(parsed.has_frame)

    def test_rejects_inconsistent_or_oversized_results_before_writing(self):
        invalid = [
            worker.DecodedMedia("audio", False, True, 0, 1, 0, b""),
            worker.DecodedMedia("video", False, True, 0, 1, 1, b"1234"),
            worker.DecodedMedia("video", True, False, 0, 1, 1, b"123"),
            worker.DecodedMedia("video", True, False, 0, 1801, 1, b""),
        ]
        for media in invalid:
            with self.subTest(media=media):
                with self.assertRaises(worker.WorkerError):
                    worker.encode_result(media, self.limits)

        tiny_result_limit = worker.WorkerLimits(
            max_edge_pixels=1,
            max_frame_bytes=4,
            max_result_bytes=1,
            max_input_bytes=1,
        )
        with self.assertRaisesRegex(worker.WorkerError, "result exceeds"):
            worker.encode_result(
                worker.DecodedMedia("audio", False, True, 0, 0, 0, b""),
                tiny_result_limit,
            )


class FileAndFailureTests(unittest.TestCase):
    def test_opens_regular_fixed_files_checks_input_and_truncates_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "media"
            frame_path = root / "frame.rgba"
            result_path = root / "result.json"
            input_path.write_bytes(b"media")
            frame_path.write_bytes(b"stale frame")
            result_path.write_bytes(b"stale result")
            limits = worker.WorkerLimits(10, 400, 1024, 5)

            with (
                mock.patch.object(worker, "INPUT_PATH", str(input_path)),
                mock.patch.object(worker, "FRAME_OUTPUT_PATH", str(frame_path)),
                mock.patch.object(worker, "RESULT_OUTPUT_PATH", str(result_path)),
            ):
                with worker.open_worker_files(limits) as files:
                    self.assertTrue(
                        os.path.samestat(os.fstat(files.input_fd), input_path.stat())
                    )
                    self.assertEqual(os.fstat(files.frame_fd).st_size, 0)
                    self.assertEqual(os.fstat(files.result_fd).st_size, 0)

            self.assertEqual(frame_path.read_bytes(), b"")
            self.assertEqual(result_path.read_bytes(), b"")

    def test_rejects_empty_oversized_and_nonregular_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame_path = root / "frame.rgba"
            result_path = root / "result.json"
            frame_path.touch()
            result_path.touch()
            limits = worker.WorkerLimits(10, 400, 1024, 4)

            for name, create in (
                ("empty", lambda path: path.touch()),
                ("large", lambda path: path.write_bytes(b"12345")),
                ("directory", lambda path: path.mkdir()),
            ):
                input_path = root / name
                create(input_path)
                with self.subTest(name=name):
                    with (
                        mock.patch.object(worker, "INPUT_PATH", str(input_path)),
                        mock.patch.object(
                            worker, "FRAME_OUTPUT_PATH", str(frame_path)
                        ),
                        mock.patch.object(
                            worker, "RESULT_OUTPUT_PATH", str(result_path)
                        ),
                    ):
                        with self.assertRaises(worker.WorkerError):
                            with worker.open_worker_files(limits):
                                pass

    def test_rejects_same_inode_and_nonregular_outputs_before_truncating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "media"
            input_path.write_bytes(b"media")
            limits = worker.WorkerLimits(10, 400, 1024, 5)

            frame_path = root / "shared-output"
            result_path = root / "shared-output-link"
            frame_path.write_bytes(b"must survive")
            os.link(frame_path, result_path)
            with (
                mock.patch.object(worker, "INPUT_PATH", str(input_path)),
                mock.patch.object(worker, "FRAME_OUTPUT_PATH", str(frame_path)),
                mock.patch.object(worker, "RESULT_OUTPUT_PATH", str(result_path)),
            ):
                with self.assertRaises(worker.WorkerError):
                    with worker.open_worker_files(limits):
                        pass
            self.assertEqual(frame_path.read_bytes(), b"must survive")

            directory_output = root / "not-regular"
            directory_output.mkdir()
            regular_output = root / "regular-result"
            regular_output.write_bytes(b"also survives")
            with (
                mock.patch.object(worker, "INPUT_PATH", str(input_path)),
                mock.patch.object(
                    worker, "FRAME_OUTPUT_PATH", str(directory_output)
                ),
                mock.patch.object(
                    worker, "RESULT_OUTPUT_PATH", str(regular_output)
                ),
            ):
                with self.assertRaises(worker.WorkerError):
                    with worker.open_worker_files(limits):
                        pass
            self.assertEqual(regular_output.read_bytes(), b"also survives")

    def test_os_stderr_suppresses_native_text_and_reports_one_fixed_line(self):
        helper_path = PROJECT_ROOT / "helpers" / "kukni-media-worker.py"
        script = f"""
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("worker_child", {str(helper_path)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def fail_with_native_log(_limits):
    os.write(2, b"decoder-controlled secret text\\n")
    raise RuntimeError("decoder-controlled exception text")

module.run = fail_with_native_log
raise SystemExit(module.main({valid_argv()!r}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, worker.FAILURE_BYTES)
        self.assertNotIn(b"secret", completed.stderr)


class OutputTransactionTests(unittest.TestCase):
    def setUp(self):
        self.limits = worker.parse_cli(valid_argv())
        self.media = worker.DecodedMedia(
            "video", True, False, 0, 1, 1, b"RGBA"
        )

    def test_write_all_retries_short_writes(self):
        written = bytearray()

        def short_write(descriptor, remaining):
            self.assertEqual(descriptor, 19)
            count = min(2, len(remaining))
            written.extend(remaining[:count])
            return count

        with mock.patch.object(worker.os, "write", side_effect=short_write) as write:
            worker._write_all(19, b"abcdefg")

        self.assertEqual(written, b"abcdefg")
        self.assertEqual(write.call_count, 4)

    def test_run_writes_frame_first_and_result_last(self):
        @contextmanager
        def opened_files(_limits):
            yield worker.WorkerFiles(input_fd=11, frame_fd=12, result_fd=13)

        writes = []
        with (
            mock.patch.object(worker, "open_worker_files", opened_files),
            mock.patch.object(worker, "decode_media", return_value=self.media) as decode,
            mock.patch.object(
                worker, "encode_result", return_value=(b"frame", b"result")
            ),
            mock.patch.object(
                worker, "_write_all", side_effect=lambda fd, data: writes.append((fd, data))
            ),
        ):
            worker.run(self.limits)

        decode.assert_called_once_with(11, self.limits)
        self.assertEqual(writes, [(12, b"frame"), (13, b"result")])

    def test_frame_write_failure_leaves_result_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "media"
            frame_path = root / "frame.rgba"
            result_path = root / "result.json"
            input_path.write_bytes(b"media")
            frame_path.write_bytes(b"stale frame")
            result_path.write_bytes(b"stale result")
            writes = []

            def fail_first_write(descriptor, payload):
                writes.append((descriptor, payload))
                raise worker.WorkerError("synthetic frame write failure")

            with (
                mock.patch.object(worker, "INPUT_PATH", str(input_path)),
                mock.patch.object(worker, "FRAME_OUTPUT_PATH", str(frame_path)),
                mock.patch.object(worker, "RESULT_OUTPUT_PATH", str(result_path)),
                mock.patch.object(worker, "decode_media", return_value=self.media),
                mock.patch.object(
                    worker, "encode_result", return_value=(b"frame", b"result")
                ),
                mock.patch.object(
                    worker, "_write_all", side_effect=fail_first_write
                ),
            ):
                with self.assertRaises(worker.WorkerError):
                    worker.run(self.limits)

            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][1], b"frame")
            self.assertEqual(result_path.read_bytes(), b"")


class DecodeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.limits = worker.parse_cli(valid_argv())
        self.paused = object()
        self.null = object()
        self.success = object()
        self.failure = object()
        self.gst = SimpleNamespace(
            State=SimpleNamespace(PAUSED=self.paused, NULL=self.null),
            StateChangeReturn=SimpleNamespace(
                SUCCESS=self.success,
                FAILURE=self.failure,
            ),
            SECOND=1,
        )

    def _pipeline_patches(self, pipeline, audio_sink, video_sink, app_sink):
        def require_element(_gst, name):
            return {"playbin": pipeline, "fakesink": audio_sink}[name]

        return (
            mock.patch.object(
                worker, "_load_gstreamer", return_value=(self.gst, object())
            ),
            mock.patch.object(worker, "_require_element", side_effect=require_element),
            mock.patch.object(
                worker, "_build_video_sink", return_value=(video_sink, app_sink)
            ),
            mock.patch.object(worker, "_duration_usec", return_value=123),
        )

    def test_video_success_pulls_one_preroll_and_tears_pipeline_down(self):
        states = []
        pipeline = mock.Mock()
        pipeline.set_state.side_effect = lambda state: (
            states.append(state) or self.success
        )
        pipeline.get_state.return_value = (self.success, self.paused, None)
        pipeline.get_property.side_effect = lambda name: {
            "n-video": 1,
            "n-audio": 0,
        }[name]
        audio_sink = mock.Mock()
        video_sink = mock.Mock()
        app_sink = mock.Mock()
        sample = object()
        app_sink.emit.return_value = sample

        patches = self._pipeline_patches(
            pipeline, audio_sink, video_sink, app_sink
        )
        with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
            worker, "_sample_to_frame", return_value=(1, 1, b"RGBA")
        ) as sample_to_frame:
            media = worker.decode_media(7, self.limits)

        self.assertEqual(media.frame, b"RGBA")
        self.assertEqual(states, [self.paused, self.null])
        pipeline.set_property.assert_any_call("flags", 3)
        pipeline.set_property.assert_any_call("uri", "fd://7")
        app_sink.emit.assert_called_once_with("pull-preroll")
        sample_to_frame.assert_called_once()

    def test_decode_error_still_tears_pipeline_down(self):
        states = []
        pipeline = mock.Mock()
        pipeline.set_state.side_effect = lambda state: (
            states.append(state) or self.success
        )
        pipeline.get_state.return_value = (self.success, self.paused, None)
        pipeline.get_property.side_effect = lambda name: {
            "n-video": 1,
            "n-audio": 0,
        }[name]
        audio_sink = mock.Mock()
        video_sink = mock.Mock()
        app_sink = mock.Mock()
        app_sink.emit.return_value = object()
        patches = self._pipeline_patches(
            pipeline, audio_sink, video_sink, app_sink
        )

        with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
            worker,
            "_sample_to_frame",
            side_effect=RuntimeError("decoder-controlled text"),
        ):
            with self.assertRaises(worker.WorkerError):
                worker.decode_media(7, self.limits)

        self.assertEqual(states, [self.paused, self.null])


if __name__ == "__main__":
    unittest.main()
