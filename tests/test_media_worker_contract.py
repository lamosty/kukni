# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.media_worker import (
    FRAME_FORMAT_NONE,
    FRAME_FORMAT_RGBA8,
    MediaWorkerError,
    MediaWorkerLimits,
    build_media_worker_launch,
    parse_worker_result,
    validate_frame_bytes,
    validate_worker_descriptors,
)


def result_payload(**overrides) -> bytes:
    value = {
        "version": 1,
        "kind": "video",
        "has_video": True,
        "has_audio": True,
        "duration_usec": 2_000_000,
        "width": 2,
        "height": 3,
        "frame_format": FRAME_FORMAT_RGBA8,
        "frame_bytes": 24,
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class MediaWorkerLimitsTests(unittest.TestCase):
    def test_requires_positive_finite_limits(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            MediaWorkerLimits(max_cpu_seconds=0)
        with self.assertRaisesRegex(ValueError, "finite"):
            MediaWorkerLimits(wall_timeout_seconds=float("inf"))
        with self.assertRaisesRegex(ValueError, "maximum-width row"):
            MediaWorkerLimits(max_edge_pixels=10, max_frame_bytes=39)


class MediaWorkerResultTests(unittest.TestCase):
    def test_accepts_exact_video_and_audio_results(self):
        video = parse_worker_result(result_payload())
        audio = parse_worker_result(
            result_payload(
                kind="audio",
                has_video=False,
                has_audio=True,
                width=0,
                height=0,
                frame_format=FRAME_FORMAT_NONE,
                frame_bytes=0,
            )
        )

        self.assertTrue(video.has_frame)
        self.assertFalse(audio.has_frame)

    def test_rejects_non_bytes_empty_oversized_and_invalid_json(self):
        with self.assertRaises(TypeError):
            parse_worker_result("{}")
        with self.assertRaisesRegex(MediaWorkerError, "no result"):
            parse_worker_result(b"")
        with self.assertRaisesRegex(MediaWorkerError, "size limit"):
            parse_worker_result(b" " * 33, limits=MediaWorkerLimits(max_result_bytes=32))
        with self.assertRaisesRegex(MediaWorkerError, "valid JSON"):
            parse_worker_result(b"not-json")
        with self.assertRaises(MediaWorkerError):
            parse_worker_result(b"[" * 2_000 + b"]" * 2_000)
        with self.assertRaisesRegex(MediaWorkerError, "valid JSON"):
            parse_worker_result(
                result_payload().replace(
                    b'"duration_usec":2000000',
                    b'"duration_usec":' + b"9" * 5_000,
                )
            )

    def test_rejects_missing_unknown_and_duplicate_fields(self):
        missing = json.loads(result_payload())
        del missing["kind"]
        with self.assertRaisesRegex(MediaWorkerError, "fields are invalid"):
            parse_worker_result(json.dumps(missing).encode())

        with self.assertRaisesRegex(MediaWorkerError, "fields are invalid"):
            parse_worker_result(result_payload(note="untrusted"))

        duplicate = result_payload().decode().replace(
            '"version":1',
            '"version":1,"version":1',
        )
        with self.assertRaisesRegex(MediaWorkerError, "repeats a field"):
            parse_worker_result(duplicate.encode())

    def test_rejects_wrong_scalar_types_and_non_finite_constants(self):
        with self.assertRaisesRegex(MediaWorkerError, "has_video.*boolean"):
            parse_worker_result(result_payload(has_video=1))
        with self.assertRaisesRegex(MediaWorkerError, "width.*non-negative integer"):
            parse_worker_result(result_payload(width=True))
        with self.assertRaisesRegex(MediaWorkerError, "invalid value"):
            parse_worker_result(
                result_payload().replace(
                    b'"duration_usec":2000000',
                    b'"duration_usec":NaN',
                )
            )
        with self.assertRaisesRegex(MediaWorkerError, "version"):
            parse_worker_result(result_payload(version=1.0))

    def test_rejects_inconsistent_stream_and_frame_metadata(self):
        invalid_values = (
            ({"kind": "archive"}, "kind"),
            ({"has_video": False, "has_audio": False}, "no previewable stream"),
            ({"kind": "video", "has_video": False}, "no video stream"),
            ({"kind": "audio", "has_video": True}, "inconsistent"),
            ({"frame_format": FRAME_FORMAT_NONE}, "frame dimensions"),
            ({"frame_bytes": 20}, "byte count"),
            ({"width": 0, "frame_bytes": 0}, "positive dimensions"),
        )
        for overrides, message in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(MediaWorkerError, message):
                    parse_worker_result(result_payload(**overrides))

    def test_rejects_dimension_duration_and_frame_limits(self):
        limits = MediaWorkerLimits(
            max_edge_pixels=8,
            max_frame_bytes=256,
            max_duration_usec=10,
        )
        with self.assertRaisesRegex(MediaWorkerError, "duration"):
            parse_worker_result(result_payload(duration_usec=11), limits=limits)
        with self.assertRaisesRegex(MediaWorkerError, "edge"):
            parse_worker_result(
                result_payload(
                    duration_usec=0,
                    width=9,
                    height=1,
                    frame_bytes=36,
                ),
                limits=limits,
            )
        with self.assertRaisesRegex(MediaWorkerError, "byte count"):
            parse_worker_result(
                result_payload(
                    duration_usec=0,
                    width=8,
                    height=8,
                    frame_bytes=257,
                ),
                limits=limits,
            )

    def test_frame_bytes_must_match_validated_metadata(self):
        result = parse_worker_result(result_payload())
        validate_frame_bytes(b"x" * 24, result)
        with self.assertRaisesRegex(MediaWorkerError, "declared size"):
            validate_frame_bytes(b"x" * 23, result)

        audio = parse_worker_result(
            result_payload(
                kind="audio",
                has_video=False,
                width=0,
                height=0,
                frame_format=FRAME_FORMAT_NONE,
                frame_bytes=0,
            )
        )
        validate_frame_bytes(b"", audio)
        with self.assertRaisesRegex(MediaWorkerError, "declared size"):
            validate_frame_bytes(b"x", audio)


class MediaWorkerCommandTests(unittest.TestCase):
    def test_builds_fixed_network_denied_fd_only_sandbox(self):
        with self._descriptors() as descriptors:
            launch = build_media_worker_launch(
                bwrap_path="/usr/bin/bwrap",
                prlimit_path="/usr/bin/prlimit",
                python_path="/usr/bin/python3",
                worker_path="/opt/kukni/media-worker.py",
                input_fd=descriptors[0],
                frame_fd=descriptors[1],
                result_fd=descriptors[2],
            )
        command = launch.argv

        self.assertEqual(command[0], "/usr/bin/prlimit")
        self.assertIn("--unshare-all", command)
        self.assertIn("--unshare-user", command)
        self.assertIn("--disable-userns", command)
        self.assertLess(command.index("--unshare-all"), command.index("--unshare-user"))
        self.assertLess(command.index("--unshare-user"), command.index("--disable-userns"))
        self.assertNotIn("--share-net", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("--clearenv", command)
        self.assertIn("--remount-ro", command)
        bwrap_index = command.index("/usr/bin/bwrap")
        sandbox_separator = command.index("--", bwrap_index)
        self.assertLess(command.index("--remount-ro"), sandbox_separator)
        self.assertIn("--die-with-parent", command)
        self.assertNotIn("/run/user", command)
        self.assertNotIn("/home/lamosty", command)
        self.assertIn("/input/media", command)
        self.assertIn("/output/frame.rgba", command)
        self.assertIn("/output/result.json", command)

        triples = set(zip(command, command[1:], command[2:]))
        self.assertIn(
            ("--ro-bind-fd", str(launch.pass_fds[0]), "/input/media"),
            triples,
        )
        self.assertIn(
            ("--bind-fd", str(launch.pass_fds[1]), "/output/frame.rgba"),
            triples,
        )
        self.assertIn(
            ("--bind-fd", str(launch.pass_fds[2]), "/output/result.json"),
            triples,
        )
        self.assertNotIn(("--ro-bind", "/", "/"), triples)
        self.assertEqual(launch.pass_fds, descriptors)
        self.assertNotIn("HOME", dict(launch.environment))

    def test_rejects_unsafe_paths_and_descriptors(self):
        with self._descriptors() as descriptors:
            arguments = {
                "bwrap_path": "/usr/bin/bwrap",
                "prlimit_path": "/usr/bin/prlimit",
                "python_path": "/usr/bin/python3",
                "worker_path": "/opt/kukni/media-worker.py",
                "input_fd": descriptors[0],
                "frame_fd": descriptors[1],
                "result_fd": descriptors[2],
            }
            for key, value in (
                ("bwrap_path", "bwrap"),
                ("worker_path", "worker.py"),
                ("input_fd", 2),
                ("frame_fd", True),
            ):
                with self.subTest(key=key):
                    with self.assertRaises(ValueError):
                        build_media_worker_launch(**{**arguments, key: value})
            with self.assertRaisesRegex(ValueError, "distinct"):
                build_media_worker_launch(
                    **{**arguments, "frame_fd": descriptors[0]}
                )

    def test_descriptor_validation_requires_read_only_input_and_fresh_outputs(self):
        with self._descriptors() as descriptors:
            validate_worker_descriptors(
                input_fd=descriptors[0],
                frame_fd=descriptors[1],
                result_fd=descriptors[2],
            )
            os.write(descriptors[1], b"already used")
            with self.assertRaisesRegex(ValueError, "empty and fresh"):
                validate_worker_descriptors(
                    input_fd=descriptors[0],
                    frame_fd=descriptors[1],
                    result_fd=descriptors[2],
                )

    def test_descriptor_validation_enforces_input_size_and_close_on_exec(self):
        with self._descriptors() as descriptors:
            with self.assertRaisesRegex(ValueError, "input exceeds"):
                validate_worker_descriptors(
                    input_fd=descriptors[0],
                    frame_fd=descriptors[1],
                    result_fd=descriptors[2],
                    limits=MediaWorkerLimits(max_input_bytes=4),
                )

            original_flags = fcntl.fcntl(descriptors[2], fcntl.F_GETFD)
            fcntl.fcntl(
                descriptors[2],
                fcntl.F_SETFD,
                original_flags & ~fcntl.FD_CLOEXEC,
            )
            with self.assertRaisesRegex(ValueError, "close-on-exec"):
                validate_worker_descriptors(
                    input_fd=descriptors[0],
                    frame_fd=descriptors[1],
                    result_fd=descriptors[2],
                )

    @staticmethod
    def _descriptors():
        class DescriptorContext:
            def __enter__(self):
                self.directory = tempfile.TemporaryDirectory()
                path = Path(self.directory.name, "input.mkv")
                path.write_bytes(b"synthetic media")
                self.input_fd = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                )
                self.frame = tempfile.TemporaryFile()
                self.result = tempfile.TemporaryFile()
                self.value = (
                    self.input_fd,
                    self.frame.fileno(),
                    self.result.fileno(),
                )
                return self.value

            def __exit__(self, *_args):
                os.close(self.input_fd)
                self.frame.close()
                self.result.close()
                self.directory.cleanup()

        return DescriptorContext()


if __name__ == "__main__":
    unittest.main()
