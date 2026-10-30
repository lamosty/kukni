# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gi.repository import Gio, GLib
from kukni.renderers.media_still import MediaStillRenderer, _SLOT
from kukni.media_types import supports_media


class MediaStillTests(unittest.TestCase):
    def test_supported_containers_and_playlist_rejection(self):
        for suffix in ("wmv", "wma", "asf", "mp4", "mkv", "mov", "avi", "webm", "flv", "ts"):
            self.assertTrue(supports_media("sample." + suffix, "application/octet-stream"))
        self.assertTrue(supports_media("sample.bin", "video/x-ms-wmv"))
        self.assertFalse(supports_media("sample.wmv", "image/png"))
        self.assertFalse(supports_media("sample.wmv", "video/x-ms-wvx"))

    def test_cancelled_result_is_discarded_and_releases_admission(self):
        cancellation = Gio.Cancellable()
        returned = []
        def prepare(*args, **kwargs):
            cancellation.cancel()
            return object()
        with mock.patch("kukni.renderers.media_still.run_media_worker", side_effect=prepare):
            MediaStillRenderer().render(Gio.File.new_for_path("/tmp/sample.wmv"), None,
                                        cancellation, returned.append, returned.append)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                while GLib.MainContext.default().pending():
                    GLib.MainContext.default().iteration(False)
                if _SLOT.acquire(blocking=False):
                    _SLOT.release()
                    break
                time.sleep(0.01)
            else:
                self.fail("cancelled media retained admission")
        self.assertEqual(returned, [])

    @unittest.skipUnless(platform.machine() in ("x86_64", "aarch64"), "policy ABI unsupported")
    def test_real_policy_denies_processes_and_exec_but_allows_threads(self):
        # Never install seccomp in the test runner. All attempts are confined to
        # this disposable child; an unexpectedly allowed fork exits immediately.
        code = '''
import ctypes, errno, os, runpy, threading
worker = runpy.run_path("helpers/kukni-media-worker.py")
worker["restrict_worker"]()
try:
    pid = os.fork()
except OSError as error:
    assert error.errno == errno.EPERM
else:
    if pid == 0: os._exit(1)
    os.waitpid(pid, 0)
    raise AssertionError("fork permitted")
lib = ctypes.CDLL("libseccomp.so.2")
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
libc = ctypes.CDLL(None, use_errno=True)
for name in (b"clone", b"clone3", b"unshare"):
    number = lib.seccomp_syscall_resolve_name(name)
    result = libc.syscall(number, 0, 0, 0, 0, 0)
    if result == 0 and name == b"clone": os._exit(1)
    if result > 0 and name == b"clone": os.waitpid(result, 0)
    assert result == -1, name
    assert ctypes.get_errno() == (errno.ENOSYS if name == b"clone3" else errno.EPERM), name
try:
    os.execv("/usr/bin/true", ["true"])
except OSError as error:
    assert error.errno == errno.EPERM
else:
    raise AssertionError("exec permitted")
result = []
thread = threading.Thread(target=lambda: result.append(True))
thread.start(); thread.join()
assert result == [True]
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
