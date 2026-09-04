# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.worker import (
    TERMINATION_GRACE_SECONDS,
    probe_bwrap_user_namespace,
    terminate_process_group,
)


class BubblewrapProbeTests(unittest.TestCase):
    def test_requires_a_successful_isolated_child(self):
        with mock.patch("kukni.worker.subprocess.run") as run:
            run.return_value.returncode = 0

            self.assertTrue(probe_bwrap_user_namespace("/usr/bin/bwrap", "/bin/true"))

            command = run.call_args.args[0]
            self.assertIn("--unshare-all", command)
            self.assertIn("--die-with-parent", command)
            self.assertEqual(command[-1], "/bin/true")

            run.return_value.returncode = 1
            self.assertFalse(
                probe_bwrap_user_namespace("/usr/bin/bwrap", "/bin/true")
            )

    def test_fails_closed_when_the_probe_times_out(self):
        with mock.patch(
            "kukni.worker.subprocess.run",
            side_effect=subprocess.TimeoutExpired("bwrap", 3),
        ):
            self.assertFalse(
                probe_bwrap_user_namespace("/usr/bin/bwrap", "/bin/true")
            )

    def test_fails_closed_when_the_probe_cannot_start(self):
        with mock.patch(
            "kukni.worker.subprocess.run",
            side_effect=OSError("unavailable"),
        ):
            self.assertFalse(
                probe_bwrap_user_namespace("/usr/bin/bwrap", "/bin/true")
            )


class ProcessGroupTerminationTests(unittest.TestCase):
    @staticmethod
    def _running_process() -> mock.Mock:
        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = None
        return process

    def test_returns_without_signalling_an_exited_process(self):
        process = self._running_process()
        process.poll.return_value = 0

        with mock.patch("kukni.worker.os.killpg") as killpg:
            terminate_process_group(process)

        killpg.assert_not_called()
        process.wait.assert_not_called()

    def test_terminates_the_process_group_and_waits(self):
        process = self._running_process()

        with mock.patch("kukni.worker.os.killpg") as killpg:
            terminate_process_group(process)

        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=TERMINATION_GRACE_SECONDS)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_escalates_a_stalled_process_group_to_sigkill(self):
        process = self._running_process()
        process.wait.side_effect = (
            subprocess.TimeoutExpired("worker", TERMINATION_GRACE_SECONDS),
            None,
        )

        with mock.patch("kukni.worker.os.killpg") as killpg:
            terminate_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGKILL),
            ],
        )
        self.assertEqual(
            process.wait.call_args_list,
            [
                mock.call(timeout=TERMINATION_GRACE_SECONDS),
                mock.call(timeout=TERMINATION_GRACE_SECONDS),
            ],
        )

    def test_falls_back_to_direct_process_signals(self):
        process = self._running_process()
        process.wait.side_effect = (
            subprocess.TimeoutExpired("worker", TERMINATION_GRACE_SECONDS),
            None,
        )

        with mock.patch("kukni.worker.os.killpg", side_effect=ProcessLookupError):
            terminate_process_group(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
