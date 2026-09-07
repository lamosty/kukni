# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

from pathlib import Path
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from gi.repository import Gio

from kukni.renderers.folder import (
    FolderPreviewCancelled,
    FolderPreviewError,
    FolderPreviewView,
    FolderRenderer,
    FolderSummary,
    scan_folder,
)


class FolderScanTests(unittest.TestCase):
    def test_empty_folder_has_a_complete_zero_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = scan_folder(Gio.File.new_for_path(temporary))

        self.assertEqual(summary.total_count, 0)
        self.assertEqual(summary.items, ())
        self.assertFalse(summary.truncated)

    def test_counts_immediate_mixed_children_and_only_direct_file_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "child-folder").mkdir()
            (folder / "child-folder" / "not-counted.bin").write_bytes(b"x" * 999)
            (folder / "plain.txt").write_bytes(b"12345")
            (folder / ".hidden").write_bytes(b"abc")
            (folder / "file-link").symlink_to(folder / "plain.txt")
            if hasattr(os, "mkfifo"):
                os.mkfifo(folder / "special")

            summary = scan_folder(Gio.File.new_for_path(temporary))

        self.assertEqual(summary.folder_count, 1)
        self.assertEqual(summary.file_count, 2)
        self.assertEqual(summary.hidden_count, 1)
        self.assertEqual(summary.link_count, 1)
        self.assertEqual(summary.other_count, int(hasattr(os, "mkfifo")))
        self.assertEqual(summary.direct_file_size, 8)
        self.assertEqual(
            [item.name for item in summary.items],
            sorted(
                (item.name for item in summary.items),
                key=lambda name: (name.casefold(), name),
            ),
        )
        link = next(item for item in summary.items if item.name == "file-link")
        self.assertEqual(link.kind, "link")
        self.assertIsNone(link.size)

    def test_sample_is_bounded_and_entry_limit_is_reported_as_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for index in range(30):
                (folder / f"item-{index:02}.txt").write_bytes(b"x")

            summary = scan_folder(
                Gio.File.new_for_path(temporary),
                max_entries=12,
                sample_limit=5,
            )

        self.assertTrue(summary.truncated)
        self.assertEqual(summary.total_count, 12)
        self.assertEqual(summary.file_count, 12)
        self.assertEqual(len(summary.items), 5)

    def test_time_limit_produces_an_honest_partial_summary(self):
        ticks = iter((0.0, 0.0, 1.0))
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "one").write_bytes(b"1")
            (Path(temporary) / "two").write_bytes(b"2")
            summary = scan_folder(
                Gio.File.new_for_path(temporary),
                time_limit_seconds=0.5,
                clock=lambda: next(ticks),
            )

        self.assertTrue(summary.truncated)
        self.assertEqual(summary.total_count, 1)

    def test_timeout_before_first_item_is_not_described_as_empty(self):
        ticks = iter((0.0, 1.0))
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "present").write_bytes(b"x")
            summary = scan_folder(
                Gio.File.new_for_path(temporary),
                time_limit_seconds=0.5,
                clock=lambda: next(ticks),
            )

        self.assertTrue(summary.truncated)
        self.assertEqual(summary.total_count, 0)
        self.assertNotIn("Empty", FolderPreviewView._heading(summary))
        self.assertIn("Partial", FolderPreviewView._heading(summary))
        self.assertNotIn("No items", FolderPreviewView._empty_message(summary))

    def test_partial_or_unknown_direct_size_is_always_a_lower_bound(self):
        complete = FolderSummary((), 1, 1, 0, 0, 0, 0, 5, 0)
        partial = FolderSummary((), 1, 1, 0, 0, 0, 0, 5, 0, truncated=True)
        unknown = FolderSummary((), 1, 1, 0, 0, 0, 0, 5, 1)

        self.assertEqual(
            FolderPreviewView._direct_size_text(complete),
            "Files directly in this folder: 5 bytes",
        )
        self.assertIn(
            "at least 5 bytes known",
            FolderPreviewView._direct_size_text(partial),
        )
        self.assertIn(
            "at least 5 bytes known",
            FolderPreviewView._direct_size_text(unknown),
        )

    def test_missing_child_stat_makes_direct_size_a_lower_bound(self):
        class VanishedEntry:
            name = "vanished"

            @staticmethod
            def stat(*, follow_symlinks):
                self.assertFalse(follow_symlinks)
                raise FileNotFoundError

        class OneEntryScan:
            def __enter__(self):
                return iter((VanishedEntry(),))

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "kukni.renderers.folder.os.scandir",
            return_value=OneEntryScan(),
        ):
            summary = scan_folder(Gio.File.new_for_path(temporary))

        self.assertEqual(summary.other_count, 1)
        self.assertEqual(summary.unknown_file_size_count, 1)
        self.assertIn(
            "at least 0 bytes known",
            FolderPreviewView._direct_size_text(summary),
        )

    def test_cancellation_stops_between_immediate_entries(self):
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 4

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for index in range(10):
                (folder / str(index)).write_bytes(b"x")
            with self.assertRaises(FolderPreviewCancelled):
                scan_folder(
                    Gio.File.new_for_path(temporary),
                    cancelled=cancelled,
                )

    def test_permission_failure_has_a_readable_bounded_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "private"
            folder.mkdir()
            (folder / "secret").write_bytes(b"x")
            folder.chmod(0)
            try:
                with self.assertRaisesRegex(FolderPreviewError, "Permission was denied"):
                    scan_folder(Gio.File.new_for_path(str(folder)))
            finally:
                folder.chmod(stat.S_IRWXU)

    def test_selected_folder_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            target = folder / "target"
            target.mkdir()
            (target / "inside").write_bytes(b"content")
            link = folder / "linked-folder"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(FolderPreviewError, "Symbolic-link"):
                scan_folder(Gio.File.new_for_path(str(link)))

    def test_open_descriptor_survives_root_replacement_without_following_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected"
            moved = root / "moved"
            outside = root / "outside"
            selected.mkdir()
            outside.mkdir()
            (selected / "original-item").write_bytes(b"safe")
            (outside / "outside-item").write_bytes(b"not selected")
            real_scandir = os.scandir

            def replace_after_open(descriptor):
                selected.rename(moved)
                selected.symlink_to(outside, target_is_directory=True)
                return real_scandir(descriptor)

            with mock.patch(
                "kukni.renderers.folder.os.scandir",
                side_effect=replace_after_open,
            ):
                summary = scan_folder(Gio.File.new_for_path(str(selected)))

        self.assertEqual([item.name for item in summary.items], ["original-item"])

    def test_limits_must_be_positive(self):
        file = Gio.File.new_for_path("/unused")
        for keyword in ("max_entries", "time_limit_seconds", "sample_limit"):
            with self.subTest(keyword=keyword), self.assertRaises(ValueError):
                scan_folder(file, **{keyword: 0})


class FolderRendererTests(unittest.TestCase):
    def test_renderer_requires_a_native_directory(self):
        renderer = FolderRenderer()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.DIRECTORY)

        self.assertTrue(renderer.supports(Gio.File.new_for_path("/tmp/folder"), info))
        self.assertFalse(
            renderer.supports(Gio.File.new_for_uri("https://invalid.example/folder"), info)
        )
        info.set_file_type(Gio.FileType.REGULAR)
        self.assertFalse(renderer.supports(Gio.File.new_for_path("/tmp/file"), info))

    def test_cancelled_selection_cannot_deliver_a_stale_view(self):
        renderer = FolderRenderer()
        cancellable = Gio.Cancellable()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.DIRECTORY)
        entered = threading.Event()
        release = threading.Event()
        idle_delivery = threading.Event()
        ready: list[object] = []

        def delayed_scan(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return FolderSummary((), 0, 0, 0, 0, 0, 0, 0, 0)

        def immediate_idle(callback, *args):
            try:
                return callback(*args)
            finally:
                idle_delivery.set()

        with mock.patch("kukni.renderers.folder.scan_folder", side_effect=delayed_scan), mock.patch(
            "kukni.renderers.folder.GLib.idle_add", side_effect=immediate_idle
        ):
            renderer.render(
                Gio.File.new_for_path("/tmp/folder"),
                info,
                cancellable,
                lambda *args: ready.append(args),
                lambda _message: self.fail("folder errors should use a native state"),
            )
            self.assertTrue(entered.wait(1))
            cancellable.cancel()
            release.set()
            self.assertTrue(idle_delivery.wait(1))

        self.assertEqual(ready, [])

    def test_two_busy_workers_keep_only_the_newest_pending_selection(self):
        renderer = FolderRenderer()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.DIRECTORY)
        old_cancellables = (Gio.Cancellable(), Gio.Cancellable())
        displaced = Gio.Cancellable()
        newest = Gio.Cancellable()
        entered = threading.Event()
        release = threading.Event()
        old_idle = threading.Event()
        newest_ready = threading.Event()
        scan_names: list[str] = []
        ready_names: list[str] = []
        timeout_calls: list[tuple[object, tuple[object, ...]]] = []
        removed: list[int] = []
        counts = {"entered": 0, "old_idle": 0}
        lock = threading.Lock()

        def delayed_scan(file, **_kwargs):
            name = file.get_basename()
            scan_names.append(name)
            if name.startswith("old"):
                with lock:
                    counts["entered"] += 1
                    if counts["entered"] == 2:
                        entered.set()
                release.wait(2)
            return FolderSummary((), 0, 0, 0, 0, 0, 0, 0, 0)

        def record_timeout(_delay, callback, *args):
            timeout_calls.append((callback, args))
            return len(timeout_calls)

        def immediate_idle(callback, *args):
            result = callback(*args)
            with lock:
                if args[2] in old_cancellables:
                    counts["old_idle"] += 1
                    if counts["old_idle"] == 2:
                        old_idle.set()
            return result

        def on_ready(_view, subtitle):
            ready_names.append(subtitle)
            newest_ready.set()

        with mock.patch(
            "kukni.renderers.folder.scan_folder", side_effect=delayed_scan
        ), mock.patch(
            "kukni.renderers.folder.FolderPreviewView",
            side_effect=lambda summary, _icon: summary,
        ), mock.patch(
            "kukni.renderers.folder.GLib.timeout_add", side_effect=record_timeout
        ), mock.patch(
            "kukni.renderers.folder.GLib.source_remove",
            side_effect=lambda source_id: removed.append(source_id),
        ), mock.patch(
            "kukni.renderers.folder.GLib.idle_add", side_effect=immediate_idle
        ):
            renderer.render(
                Gio.File.new_for_path("/tmp/old-one"),
                info,
                old_cancellables[0],
                on_ready,
                self.fail,
            )
            renderer.render(
                Gio.File.new_for_path("/tmp/old-two"),
                info,
                old_cancellables[1],
                on_ready,
                self.fail,
            )
            self.assertTrue(entered.wait(1))
            renderer.render(
                Gio.File.new_for_path("/tmp/displaced"),
                info,
                displaced,
                on_ready,
                self.fail,
            )
            renderer.render(
                Gio.File.new_for_path("/tmp/newest"),
                info,
                newest,
                on_ready,
                self.fail,
            )
            self.assertEqual(removed, [1])
            self.assertEqual(len(timeout_calls), 2)

            for cancellable in (*old_cancellables, displaced):
                cancellable.cancel()
            release.set()
            self.assertTrue(old_idle.wait(1))
            callback, args = timeout_calls[-1]
            callback(*args)
            self.assertTrue(newest_ready.wait(1))

        self.assertCountEqual(scan_names, ["old-one", "old-two", "newest"])
        self.assertEqual(ready_names, ["Folder · 0 items"])

    def test_read_failure_is_delivered_as_a_native_folder_state(self):
        renderer = FolderRenderer()
        cancellable = Gio.Cancellable()
        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.DIRECTORY)
        delivered = threading.Event()
        ready: list[object] = []
        errors: list[str] = []
        sentinel = object()

        def immediate_idle(callback, *args):
            try:
                return callback(*args)
            finally:
                delivered.set()

        with mock.patch(
            "kukni.renderers.folder.scan_folder",
            side_effect=FolderPreviewError("Permission was denied."),
        ), mock.patch(
            "kukni.renderers.folder.FolderPreviewView", return_value=sentinel
        ), mock.patch(
            "kukni.renderers.folder.GLib.idle_add", side_effect=immediate_idle
        ):
            renderer.render(
                Gio.File.new_for_path("/tmp/private-folder"),
                info,
                cancellable,
                lambda *args: ready.append(args),
                errors.append,
            )
            self.assertTrue(delivered.wait(1))

        self.assertEqual(errors, [])
        self.assertEqual(len(ready), 1)
        self.assertIs(ready[0][0], sentinel)
        self.assertEqual(ready[0][1], "Folder · contents unavailable")


if __name__ == "__main__":
    unittest.main()
