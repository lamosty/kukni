# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Shared lifecycle primitives for short-lived, sandboxed worker processes."""

from __future__ import annotations

import os
import signal
import subprocess


TERMINATION_GRACE_SECONDS = 0.5


def probe_bwrap_user_namespace(bwrap_path: str, true_path: str) -> bool:
    """Return whether bubblewrap can create the user namespace workers need."""

    try:
        result = subprocess.run(
            (
                bwrap_path,
                "--unshare-all",
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--",
                true_path,
            ),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def terminate_process_group(process: subprocess.Popen) -> None:
    """Stop a worker launched in a new session, escalating TERM to KILL."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
