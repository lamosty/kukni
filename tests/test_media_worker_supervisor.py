# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import kukni.media_worker as media_worker
from kukni.media_worker import (
    MediaWorkerCancelled,
    MediaWorkerError,
    MediaWorkerLimits,
    run_media_worker,
)


def video_result_payload(*, frame_bytes: int = 4) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "kind": "video",
            "has_video": True,
            "has_audio": False,
            "duration_usec": 1_000_000,
            "width": 1,
            "height": 1,
            "frame_format": "rgba8",
            "frame_bytes": frame_bytes,
        },
        separators=(",", ":"),
    ).encode("utf-8")


class FinishedProcess:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.pid = 41_001

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class HangingProcess:
    def __init__(self):
        self.returncode = None
        self.pid = 41_002
        self.waited = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited = True
        raise subprocess.TimeoutExpired(("worker",), timeout)


class RecordingSlots:
    def __init__(self):
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, timeout):
        self.acquire_calls += 1
        return True

    def release(self):
        self.release_calls += 1


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.input_path = self.root / "sample.mkv"
        self.input_path.write_bytes(b"synthetic media input")
        self.helper_path = self.root / "kukni-media-worker.py"
        self.helper_path.write_text("# trusted test helper\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def runtime_arguments(self):
        executable = os.path.realpath("/bin/true")
        return {
            "bwrap_path": executable,
            "prlimit_path": executable,
            "python_path": executable,
            "true_path": executable,
            "worker_path": self.helper_path,
        }

    def test_launches_with_fd_only_boundary_and_returns_validated_bytes(self):
        captured = {}
        probe_calls = []
        input_open_calls = 0
        real_open = os.open

        def tracking_open(path, flags, *args):
            nonlocal input_open_calls
            if os.fspath(path) == os.fspath(self.input_path):
                input_open_calls += 1
            return real_open(path, flags, *args)

        def process_factory(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            input_fd, frame_fd, result_fd = kwargs["pass_fds"]

            input_status = fcntl.fcntl(input_fd, fcntl.F_GETFL)
            input_descriptor = fcntl.fcntl(input_fd, fcntl.F_GETFD)
            self.assertEqual(input_status & os.O_ACCMODE, os.O_RDONLY)
            self.assertTrue(input_status & os.O_NONBLOCK)
            self.assertTrue(input_descriptor & fcntl.FD_CLOEXEC)
            self.assertTrue(stat.S_ISREG(os.fstat(input_fd).st_mode))

            output_paths = []
            for output_fd in (frame_fd, result_fd):
                metadata = os.fstat(output_fd)
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                self.assertEqual(metadata.st_size, 0)
                self.assertEqual(os.lseek(output_fd, 0, os.SEEK_CUR), 0)
                self.assertTrue(
                    fcntl.fcntl(output_fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
                )
                output_paths.append(Path(os.readlink(f"/proc/self/fd/{output_fd}")))
            self.assertEqual(stat.S_IMODE(output_paths[0].parent.stat().st_mode), 0o700)
            self.assertEqual(output_paths[0].parent, output_paths[1].parent)

            os.write(frame_fd, b"RGBA")
            os.write(result_fd, video_result_payload())
            return FinishedProcess()

        def runtime_probe(bwrap_path, true_path):
            probe_calls.append((bwrap_path, true_path))
            return True

        with (
            mock.patch("kukni.media_worker.os.open", side_effect=tracking_open),
            mock.patch("kukni.media_worker.terminate_process_group") as terminate,
        ):
            output = run_media_worker(
                self.input_path,
                process_factory=process_factory,
                runtime_probe=runtime_probe,
                **self.runtime_arguments(),
            )

        self.assertEqual(input_open_calls, 1)
        self.assertEqual(output.frame, b"RGBA")
        self.assertIsInstance(output.frame, bytes)
        self.assertTrue(output.result.has_frame)
        self.assertEqual(output.result.frame_bytes, 4)
        self.assertEqual(len(probe_calls), 1)
        self.assertEqual(
            set(captured["kwargs"]),
            {
                "stdin",
                "stdout",
                "stderr",
                "close_fds",
                "pass_fds",
                "start_new_session",
                "env",
            },
        )
        self.assertEqual(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"]["stdout"], subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"]["stderr"], subprocess.DEVNULL)
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertEqual(
            captured["kwargs"]["env"],
            {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
        self.assertIsInstance(captured["command"], tuple)
        terminate.assert_called_once()
        for descriptor in captured["kwargs"]["pass_fds"]:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_fails_closed_when_runtime_or_namespace_is_unavailable(self):
        factory = mock.Mock()
        with self.assertRaisesRegex(MediaWorkerError, "sandbox is unavailable"):
            run_media_worker(
                self.input_path,
                process_factory=factory,
                runtime_probe=lambda _bwrap, _true: False,
                **self.runtime_arguments(),
            )
        factory.assert_not_called()

        arguments = self.runtime_arguments()
        arguments["bwrap_path"] = self.root / "missing-bwrap"
        with self.assertRaisesRegex(MediaWorkerError, "runtime tool"):
            run_media_worker(
                self.input_path,
                process_factory=factory,
                runtime_probe=lambda _bwrap, _true: True,
                **arguments,
            )
        factory.assert_not_called()

        arguments = self.runtime_arguments()
        arguments["worker_path"] = self.root / "missing-helper.py"
        with self.assertRaisesRegex(MediaWorkerError, "helper is unavailable"):
            run_media_worker(
                self.input_path,
                process_factory=factory,
                runtime_probe=lambda _bwrap, _true: True,
                **arguments,
            )
        factory.assert_not_called()

    def test_default_runtime_lookup_ignores_path_shadowing_and_resolves_links(self):
        trusted = self.root / "trusted"
        shadow = self.root / "shadow"
        trusted.mkdir()
        shadow.mkdir()
        expected = {}
        for command_name in ("bwrap", "prlimit", "python3", "true"):
            trusted_target = trusted / f"real-{command_name}"
            trusted_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            trusted_target.chmod(0o700)
            (trusted / command_name).symlink_to(trusted_target.name)
            shadow_command = shadow / command_name
            shadow_command.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            shadow_command.chmod(0o700)
            expected[command_name] = trusted_target.resolve()

        with (
            mock.patch.object(
                media_worker,
                "_TRUSTED_EXECUTABLE_DIRECTORIES",
                (trusted,),
            ),
            mock.patch.dict(os.environ, {"PATH": os.fspath(shadow)}),
        ):
            runtime = media_worker._resolve_media_worker_runtime(
                bwrap_path=None,
                prlimit_path=None,
                python_path=None,
                worker_path=self.helper_path,
                true_path=None,
            )
            explicit = media_worker._require_executable(
                shadow / "bwrap",
                "bwrap",
            )

        self.assertEqual(Path(runtime[0]), expected["bwrap"])
        self.assertEqual(Path(runtime[1]), expected["prlimit"])
        self.assertEqual(Path(runtime[2]), expected["python3"])
        self.assertEqual(Path(runtime[4]), expected["true"])
        self.assertEqual(Path(explicit), (shadow / "bwrap").resolve())

    def test_default_probe_must_fit_and_finish_inside_the_request_deadline(self):
        factory = mock.Mock()
        with mock.patch(
            "kukni.media_worker.probe_bwrap_user_namespace",
            return_value=True,
        ) as probe:
            with self.assertRaisesRegex(MediaWorkerError, "timed out"):
                run_media_worker(
                    self.input_path,
                    limits=MediaWorkerLimits(wall_timeout_seconds=2.9),
                    process_factory=factory,
                    clock=lambda: 0.0,
                    **self.runtime_arguments(),
                )
        probe.assert_not_called()
        factory.assert_not_called()

        expired = False

        def clock():
            return 5.0 if expired else 0.0

        def slow_probe(_bwrap, _true):
            nonlocal expired
            expired = True
            return True

        with mock.patch(
            "kukni.media_worker.probe_bwrap_user_namespace",
            side_effect=slow_probe,
        ) as probe:
            with self.assertRaisesRegex(MediaWorkerError, "timed out"):
                run_media_worker(
                    self.input_path,
                    limits=MediaWorkerLimits(wall_timeout_seconds=4.0),
                    process_factory=factory,
                    clock=clock,
                    **self.runtime_arguments(),
                )
        probe.assert_called_once()
        factory.assert_not_called()

    def test_rejects_invalid_input_without_blocking_or_spawning(self):
        factory = mock.Mock()
        for content, limits, message in (
            (b"", MediaWorkerLimits(), "empty"),
            (b"12345", MediaWorkerLimits(max_input_bytes=4), "size limit"),
        ):
            with self.subTest(message=message):
                self.input_path.write_bytes(content)
                with self.assertRaisesRegex(MediaWorkerError, message):
                    run_media_worker(
                        self.input_path,
                        limits=limits,
                        process_factory=factory,
                        runtime_probe=lambda _bwrap, _true: True,
                        **self.runtime_arguments(),
                    )
        with self.assertRaisesRegex(MediaWorkerError, "regular local file"):
            run_media_worker(
                self.root,
                process_factory=factory,
                runtime_probe=lambda _bwrap, _true: True,
                **self.runtime_arguments(),
            )
        factory.assert_not_called()

    def test_nonzero_or_crashed_worker_has_only_parent_controlled_error(self):
        for returncode in (7, -11):
            with self.subTest(returncode=returncode):
                def process_factory(_command, **kwargs):
                    os.write(kwargs["pass_fds"][2], b"attacker detail")
                    return FinishedProcess(returncode)

                with self.assertRaises(MediaWorkerError) as raised:
                    run_media_worker(
                        self.input_path,
                        process_factory=process_factory,
                        runtime_probe=lambda _bwrap, _true: True,
                        **self.runtime_arguments(),
                    )
                self.assertEqual(str(raised.exception), "the media worker failed")
                self.assertNotIn("attacker", str(raised.exception))

    def test_malformed_and_oversized_outputs_are_normalized(self):
        cases = (
            (b"RGBA", b'{"attacker":"do not surface me"}', MediaWorkerLimits()),
            (b"RGBA", b"x" * 33, MediaWorkerLimits(max_result_bytes=32)),
            (
                b"x" * 17,
                video_result_payload(),
                MediaWorkerLimits(max_edge_pixels=2, max_frame_bytes=16),
            ),
        )
        for frame, result, limits in cases:
            with self.subTest(frame_size=len(frame), result_size=len(result)):
                def process_factory(_command, **kwargs):
                    os.write(kwargs["pass_fds"][1], frame)
                    os.write(kwargs["pass_fds"][2], result)
                    return FinishedProcess()

                with self.assertRaises(MediaWorkerError) as raised:
                    run_media_worker(
                        self.input_path,
                        limits=limits,
                        process_factory=process_factory,
                        runtime_probe=lambda _bwrap, _true: True,
                        **self.runtime_arguments(),
                    )
                self.assertNotIn("attacker", str(raised.exception))

    def test_detects_input_mutation_before_accepting_output(self):
        def process_factory(_command, **kwargs):
            os.write(kwargs["pass_fds"][1], b"RGBA")
            os.write(kwargs["pass_fds"][2], video_result_payload())
            self.input_path.write_bytes(b"mutated while the decoder was running")
            return FinishedProcess()

        with self.assertRaisesRegex(MediaWorkerError, "changed during decoding"):
            run_media_worker(
                self.input_path,
                process_factory=process_factory,
                runtime_probe=lambda _bwrap, _true: True,
                **self.runtime_arguments(),
            )

    def test_cancellation_terminates_the_process_and_is_distinct(self):
        process = HangingProcess()

        def cancelled():
            return process.waited

        with mock.patch("kukni.media_worker.terminate_process_group") as terminate:
            with self.assertRaises(MediaWorkerCancelled):
                run_media_worker(
                    self.input_path,
                    cancelled=cancelled,
                    process_factory=lambda _command, **_kwargs: process,
                    runtime_probe=lambda _bwrap, _true: True,
                    clock=lambda: 0.0,
                    **self.runtime_arguments(),
                )
        terminate.assert_called_once_with(process)

    def test_monotonic_timeout_terminates_the_process(self):
        process = HangingProcess()
        ticks = iter((0.0, 0.01, 0.02, 0.03, 0.06, 0.1, 0.11))

        with mock.patch("kukni.media_worker.terminate_process_group") as terminate:
            with self.assertRaisesRegex(MediaWorkerError, "timed out"):
                run_media_worker(
                    self.input_path,
                    limits=MediaWorkerLimits(wall_timeout_seconds=0.1),
                    process_factory=lambda _command, **_kwargs: process,
                    runtime_probe=lambda _bwrap, _true: True,
                    clock=lambda: next(ticks),
                    **self.runtime_arguments(),
                )
        terminate.assert_called_once_with(process)

    def test_completed_poll_is_rejected_if_it_arrives_after_deadline(self):
        expired = False

        class LatePollProcess(FinishedProcess):
            def poll(self):
                nonlocal expired
                expired = True
                return 0

        process = LatePollProcess()
        with mock.patch("kukni.media_worker.terminate_process_group") as terminate:
            with self.assertRaisesRegex(MediaWorkerError, "timed out"):
                run_media_worker(
                    self.input_path,
                    limits=MediaWorkerLimits(wall_timeout_seconds=1.0),
                    process_factory=lambda _command, **_kwargs: process,
                    runtime_probe=lambda _bwrap, _true: True,
                    clock=lambda: 2.0 if expired else 0.0,
                    **self.runtime_arguments(),
                )
        terminate.assert_called_once_with(process)

    def test_completed_wait_is_rejected_if_it_returns_after_deadline(self):
        expired = False

        class LateWaitProcess(HangingProcess):
            def wait(self, timeout=None):
                nonlocal expired
                expired = True
                self.returncode = 0
                return 0

        process = LateWaitProcess()
        with mock.patch("kukni.media_worker.terminate_process_group") as terminate:
            with self.assertRaisesRegex(MediaWorkerError, "timed out"):
                run_media_worker(
                    self.input_path,
                    limits=MediaWorkerLimits(wall_timeout_seconds=1.0),
                    process_factory=lambda _command, **_kwargs: process,
                    runtime_probe=lambda _bwrap, _true: True,
                    clock=lambda: 2.0 if expired else 0.0,
                    **self.runtime_arguments(),
                )
        terminate.assert_called_once_with(process)

    def test_validated_output_is_not_accepted_after_deadline(self):
        expired = False
        original_check = media_worker._ensure_input_unchanged

        def process_factory(_command, **kwargs):
            os.write(kwargs["pass_fds"][1], b"RGBA")
            os.write(kwargs["pass_fds"][2], video_result_payload())
            return FinishedProcess()

        def finish_input_validation(descriptor, expected):
            nonlocal expired
            original_check(descriptor, expected)
            expired = True

        with (
            mock.patch(
                "kukni.media_worker._ensure_input_unchanged",
                side_effect=finish_input_validation,
            ),
            mock.patch("kukni.media_worker.terminate_process_group") as terminate,
        ):
            with self.assertRaisesRegex(MediaWorkerError, "timed out"):
                run_media_worker(
                    self.input_path,
                    limits=MediaWorkerLimits(wall_timeout_seconds=1.0),
                    process_factory=process_factory,
                    runtime_probe=lambda _bwrap, _true: True,
                    clock=lambda: 2.0 if expired else 0.0,
                    **self.runtime_arguments(),
                )
        terminate.assert_called_once()

    def test_terminator_failure_cannot_replace_error_or_skip_cleanup(self):
        slots = RecordingSlots()
        captured_fds = ()
        output_paths = ()

        def process_factory(_command, **kwargs):
            nonlocal captured_fds, output_paths
            captured_fds = kwargs["pass_fds"]
            output_paths = tuple(
                Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                for descriptor in captured_fds[1:]
            )
            return FinishedProcess(returncode=9)

        with (
            mock.patch.object(media_worker, "_WORKER_SLOTS", slots),
            mock.patch(
                "kukni.media_worker.terminate_process_group",
                side_effect=BaseException("termination failed"),
            ) as terminate,
        ):
            with self.assertRaises(MediaWorkerError) as raised:
                run_media_worker(
                    self.input_path,
                    process_factory=process_factory,
                    runtime_probe=lambda _bwrap, _true: True,
                    **self.runtime_arguments(),
                )

        self.assertEqual(str(raised.exception), "the media worker failed")
        terminate.assert_called_once()
        self.assertEqual(slots.release_calls, 1)
        for descriptor in captured_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        for output_path in output_paths:
            self.assertFalse(output_path.exists())

    def test_terminator_failure_cannot_replace_cancellation(self):
        process = HangingProcess()
        slots = RecordingSlots()

        with (
            mock.patch.object(media_worker, "_WORKER_SLOTS", slots),
            mock.patch(
                "kukni.media_worker.terminate_process_group",
                side_effect=RuntimeError("termination failed"),
            ) as terminate,
        ):
            with self.assertRaises(MediaWorkerCancelled):
                run_media_worker(
                    self.input_path,
                    cancelled=lambda: process.waited,
                    process_factory=lambda _command, **_kwargs: process,
                    runtime_probe=lambda _bwrap, _true: True,
                    clock=lambda: 0.0,
                    **self.runtime_arguments(),
                )

        terminate.assert_called_once_with(process)
        self.assertEqual(slots.release_calls, 1)

    def test_cancellation_is_polled_while_waiting_for_a_global_slot(self):
        class BusySlots:
            def __init__(self):
                self.acquire_calls = 0
                self.release_calls = 0

            def acquire(self, timeout):
                self.acquire_calls += 1
                return False

            def release(self):
                self.release_calls += 1

        slots = BusySlots()
        with mock.patch.object(media_worker, "_WORKER_SLOTS", slots):
            with self.assertRaises(MediaWorkerCancelled):
                run_media_worker(
                    self.input_path,
                    cancelled=lambda: slots.acquire_calls > 0,
                    process_factory=mock.Mock(),
                    runtime_probe=lambda _bwrap, _true: True,
                    clock=lambda: 0.0,
                    **self.runtime_arguments(),
                )
        self.assertEqual(slots.acquire_calls, 1)
        self.assertEqual(slots.release_calls, 0)


if __name__ == "__main__":
    unittest.main()
