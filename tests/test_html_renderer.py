# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kukni.renderers.html import (
    CONTENT_SECURITY_POLICY,
    LOCKED_DOWN_SETTINGS,
    MAX_HTML_BYTES,
    HtmlPreviewError,
    HtmlRenderer,
    WebKit,
    apply_locked_down_settings,
    build_error_document,
    build_safe_document,
    read_bounded_local_file,
    probe_bwrap_user_namespace,
    user_namespace_policy_allows_sandbox,
    webkit_sandbox_available,
)


class SafeDocumentTests(unittest.TestCase):
    def test_preserves_source_after_early_csp(self):
        source = b'<!doctype html><h1 id="title">Hello</h1>'

        document = build_safe_document(source)

        self.assertTrue(document.endswith(source))
        self.assertLess(document.index(b"Content-Security-Policy"), 128)

    def test_policy_denies_active_and_external_content(self):
        directives = set(CONTENT_SECURITY_POLICY.split("; "))

        self.assertIn("default-src 'none'", directives)
        self.assertIn("connect-src 'none'", directives)
        self.assertIn("script-src 'none'", directives)
        self.assertIn("frame-src 'none'", directives)
        self.assertIn("object-src 'none'", directives)
        self.assertIn("base-uri 'none'", directives)
        self.assertIn("form-action 'none'", directives)
        self.assertIn("sandbox", directives)
        self.assertNotIn("http:", CONTENT_SECURITY_POLICY)
        self.assertNotIn("https:", CONTENT_SECURITY_POLICY)
        self.assertNotIn("file:", CONTENT_SECURITY_POLICY)

    def test_error_page_escapes_untrusted_text(self):
        document = build_error_document('<img src=x onerror="alert(1)">&')

        self.assertNotIn(b'<img src=x onerror="alert(1)">', document)
        self.assertIn(b"&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;", document)

    def test_rejects_an_unknown_source_type(self):
        with self.assertRaises(TypeError):
            build_safe_document(object())


class BoundedReadTests(unittest.TestCase):
    def test_reads_a_regular_file_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "preview.html")
            source = b"<p>Kukni</p>\x00tail"
            path.write_bytes(source)

            self.assertEqual(read_bounded_local_file(os.fspath(path)), source)

    def test_rejects_a_file_larger_than_the_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "large.html")
            path.write_bytes(b"12345")

            with self.assertRaisesRegex(HtmlPreviewError, "limited"):
                read_bounded_local_file(os.fspath(path), limit=4)

    def test_detects_growth_past_the_initial_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "growing.html")
            path.write_bytes(b"12345")

            real_fstat = os.fstat

            def report_initial_empty_size(descriptor):
                result = real_fstat(descriptor)
                values = list(result)
                values[6] = 0
                return os.stat_result(values)

            with mock.patch("kukni.renderers.html.os.fstat", report_initial_empty_size):
                with self.assertRaisesRegex(HtmlPreviewError, "limited"):
                    read_bounded_local_file(os.fspath(path), limit=4)

    def test_rejects_non_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(HtmlPreviewError, "regular local file"):
                read_bounded_local_file(directory)

    def test_honours_cancellation_before_opening(self):
        with self.assertRaises(Exception) as caught:
            read_bounded_local_file("not-opened", is_cancelled=lambda: True)

        self.assertEqual(type(caught.exception).__name__, "_PreviewCancelled")

    def test_rejects_negative_limits(self):
        with self.assertRaises(ValueError):
            read_bounded_local_file("unused", limit=-1)


class RendererCapabilityTests(unittest.TestCase):
    @staticmethod
    def _file_info(content_type: str):
        from gi.repository import Gio

        info = Gio.FileInfo()
        info.set_file_type(Gio.FileType.REGULAR)
        info.set_content_type(content_type)
        return info

    def test_supports_html_mime_type_and_common_suffixes(self):
        from gi.repository import Gio

        renderer = HtmlRenderer()

        with mock.patch(
            "kukni.renderers.html.webkit_runtime_available", return_value=True
        ):
            self.assertTrue(
                renderer.supports(
                    Gio.File.new_for_path("/tmp/page.bin"),
                    self._file_info("text/html"),
                )
            )
            self.assertTrue(
                renderer.supports(
                    Gio.File.new_for_path("/tmp/PAGE.XHTML"),
                    self._file_info("application/octet-stream"),
                )
            )
            self.assertFalse(
                renderer.supports(
                    Gio.File.new_for_path("/tmp/page.txt"),
                    self._file_info("text/plain"),
                )
            )
            self.assertFalse(
                renderer.supports(
                    Gio.File.new_for_path("/tmp/misleading.html"),
                    self._file_info("image/png"),
                )
            )

    def test_original_probe_import_path_remains_available(self):
        from kukni.worker import probe_bwrap_user_namespace as worker_probe

        self.assertIs(probe_bwrap_user_namespace, worker_probe)

    def test_apparmor_restriction_rejects_an_unconfined_process(self):
        self.assertFalse(
            user_namespace_policy_allows_sandbox(
                apparmor_restriction="1\n",
                apparmor_label="unconfined\n",
                unprivileged_userns_clone="1\n",
                max_user_namespaces="1024\n",
            )
        )

    def test_apparmor_profile_can_grant_the_required_permission(self):
        self.assertTrue(
            user_namespace_policy_allows_sandbox(
                apparmor_restriction="1\n",
                apparmor_label="kukni (enforce)\n",
                unprivileged_userns_clone="1\n",
                max_user_namespaces="1024\n",
            )
        )

    def test_named_profile_advances_to_the_active_probe(self):
        self.assertTrue(
            user_namespace_policy_allows_sandbox(
                apparmor_restriction="1",
                apparmor_label="epiphany (unconfined)",
                unprivileged_userns_clone="1",
                max_user_namespaces="1024",
            )
        )

    def test_global_user_namespace_controls_are_honoured(self):
        common = {
            "apparmor_restriction": None,
            "apparmor_label": None,
            "unprivileged_userns_clone": "1",
            "max_user_namespaces": "1024",
        }
        self.assertTrue(user_namespace_policy_allows_sandbox(**common))
        self.assertFalse(
            user_namespace_policy_allows_sandbox(
                **{**common, "unprivileged_userns_clone": "0"}
            )
        )
        self.assertFalse(
            user_namespace_policy_allows_sandbox(
                **{**common, "max_user_namespaces": "0"}
            )
        )

    def test_runtime_gate_requires_the_active_bwrap_probe(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("kukni.renderers.html.WebKit", object()),
            mock.patch(
                "kukni.renderers.html.shutil.which",
                side_effect=lambda name: f"/usr/bin/{name}",
            ),
            mock.patch(
                "kukni.renderers.html.user_namespace_policy_allows_sandbox",
                return_value=True,
            ),
            mock.patch(
                "kukni.renderers.html.probe_bwrap_user_namespace",
                return_value=False,
            ) as probe,
        ):
            webkit_sandbox_available.cache_clear()
            self.assertFalse(webkit_sandbox_available())
            probe.assert_called_once_with("/usr/bin/bwrap", "/usr/bin/true")
        webkit_sandbox_available.cache_clear()

    @unittest.skipIf(WebKit is None, "WebKitGTK 6 is not installed")
    def test_current_unconfined_ubuntu_session_is_gated_before_launch(self):
        restriction = Path(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
        )
        label = Path("/proc/self/attr/current")
        if not restriction.exists() or restriction.read_text().strip() != "1":
            self.skipTest("AppArmor user namespace restriction is not active")
        if not label.exists() or not label.read_text().strip().startswith("unconfined"):
            self.skipTest("test process is not unconfined")

        webkit_sandbox_available.cache_clear()
        self.assertFalse(webkit_sandbox_available())

    def test_refuses_an_environment_that_disables_webkit_sandbox(self):
        with (
            mock.patch("kukni.renderers.html.WebKit", object()),
            mock.patch(
                "kukni.renderers.html.shutil.which",
                return_value="/usr/bin/bwrap",
            ),
            mock.patch(
                "kukni.renderers.html.user_namespace_policy_allows_sandbox",
                return_value=True,
            ),
            mock.patch.dict(
                os.environ,
                {"WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS": "1"},
            ),
        ):
            webkit_sandbox_available.cache_clear()
            self.assertFalse(webkit_sandbox_available())
        webkit_sandbox_available.cache_clear()

    @unittest.skipIf(WebKit is None, "WebKitGTK 6 is not installed")
    def test_installed_webkit_accepts_every_lockdown_setting(self):
        settings = WebKit.Settings()

        apply_locked_down_settings(settings)

        for property_name, expected in LOCKED_DOWN_SETTINGS.items():
            if settings.find_property(property_name) is not None:
                if property_name == "enable-dns-prefetching":
                    # Deprecated WebKit versions already force this off, and
                    # warn merely for reading the compatibility property.
                    continue
                self.assertEqual(settings.get_property(property_name), expected)

    def test_default_size_limit_is_eight_mib(self):
        self.assertEqual(MAX_HTML_BYTES, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
