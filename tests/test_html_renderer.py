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
    LOAD_TIMEOUT_SECONDS,
    LOCKED_DOWN_SETTINGS,
    MAX_HTML_BYTES,
    HtmlPreviewError,
    HtmlRenderer,
    WebKit,
    apply_locked_down_settings,
    build_error_document,
    build_safe_document,
    prepare_static_preview,
    read_bounded_local_file,
    probe_bwrap_user_namespace,
    user_namespace_policy_allows_sandbox,
    webkit_sandbox_available,
    stop_and_terminate_web_view,
)


class SafeDocumentTests(unittest.TestCase):
    def test_preserves_source_after_early_csp(self):
        source = b'<!doctype html><h1 id="title">Hello</h1>'

        document = build_safe_document(source)

        self.assertTrue(document.endswith(source))
        self.assertTrue(document.lower().startswith(b"<!doctype html>"))
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


class StaticPreparationTests(unittest.TestCase):
    def test_preserves_meaningful_static_markup_and_inline_style(self):
        preview = prepare_static_preview(
            "<!doctype html><html><head><style>h1 { color: teal }</style></head>"
            '<body><h1 id="title">Safe &amp; static</h1></body></html>'
        )

        self.assertFalse(preview.requires_active_content)
        self.assertFalse(preview.blocked_active_content)
        self.assertFalse(preview.blocked_external_resources)
        self.assertIn(b"h1 { color: teal }", preview.document)
        self.assertIn(b"Safe &amp; static", preview.document)

    def test_removes_external_resources_and_active_elements(self):
        preview = prepare_static_preview(
            '<link rel="stylesheet" href="https://invalid.example/site.css">'
            '<style>.hero{background:url(https://invalid.example/image.png)}</style>'
            '<body onload="run()"><h1>Readable article text</h1><p>'
            + ("This paragraph remains useful without the optional application. " * 3)
            + "</p>"
            '<img src="https://invalid.example/pixel.png">'
            '<script src="https://invalid.example/app.js">run()</script>'
            '<iframe src="https://invalid.example/frame"></iframe></body>'
        )

        self.assertTrue(preview.blocked_active_content)
        self.assertTrue(preview.blocked_external_resources)
        self.assertFalse(preview.requires_active_content)
        self.assertIn(b"Readable article text", preview.document)
        self.assertNotIn(b"invalid.example", preview.document)
        self.assertNotIn(b"<script", preview.document)
        self.assertNotIn(b"<iframe", preview.document)
        self.assertNotIn(b"onload", preview.document)

    def test_js_only_app_shell_gets_an_honest_static_explanation(self):
        preview = prepare_static_preview(
            '<div id="root"></div><script>'
            'fetch("https://invalid.example/app").then(render);'
            "</script>"
        )

        self.assertTrue(preview.requires_active_content)
        self.assertIn(b"Interactive page not run", preview.document)
        self.assertIn(b"no meaningful static page", preview.document)
        self.assertNotIn(b"invalid.example", preview.document)
        self.assertNotIn(b'id="root"', preview.document)

    def test_malformed_unclosed_script_cannot_escape_sanitization(self):
        preview = prepare_static_preview(
            '<h1>Before</h1><script>document.write("<img src=x>")'
        )

        self.assertTrue(preview.blocked_active_content)
        self.assertFalse(preview.requires_active_content)
        self.assertIn(b"Before", preview.document)
        self.assertNotIn(b"document.write", preview.document)
        self.assertNotIn(b"<script", preview.document)

    def test_self_closing_active_tag_does_not_hide_following_static_content(self):
        preview = prepare_static_preview(
            '<iframe src="https://invalid.example/frame"/><h1>After</h1>'
        )

        self.assertIn(b"After", preview.document)
        self.assertNotIn(b"iframe", preview.document)

    def test_void_embed_does_not_hide_following_static_content(self):
        preview = prepare_static_preview(
            '<embed src="https://invalid.example/plugin"><h1>Report</h1>'
        )

        self.assertFalse(preview.requires_active_content)
        self.assertIn(b"Report", preview.document)
        self.assertNotIn(b"embed", preview.document)

    def test_nested_void_active_element_does_not_escape_dropped_subtree(self):
        preview = prepare_static_preview(
            '<iframe><embed src="https://invalid.example/plugin"></iframe>'
            "<h1>Following report</h1>"
        )

        self.assertIn(b"Following report", preview.document)
        self.assertNotIn(b"embed", preview.document)
        self.assertNotIn(b"iframe", preview.document)

    def test_iframe_only_shell_gets_native_fallback_classification(self):
        preview = prepare_static_preview(
            '<iframe src="https://invalid.example/application"></iframe>'
        )

        self.assertTrue(preview.requires_active_content)

    def test_ordinary_layout_remains_in_standards_mode(self):
        preview = prepare_static_preview(
            "<!doctype html><html><body><section><h1>Report</h1>"
            "<p>Static layout</p></section></body></html>"
        )

        self.assertTrue(preview.document.lower().startswith(b"<!doctype html>"))
        self.assertIn(b"<section><h1>Report</h1>", preview.document)

    def test_pathological_unclosed_css_urls_have_bounded_linear_output(self):
        source = "<style>" + ("url(http://invalid.example/" * 20_000) + "</style>"

        preview = prepare_static_preview(source)

        self.assertLess(len(preview.document), len(source.encode("utf-8")))
        self.assertNotIn(b"invalid.example", preview.document)

    def test_sanitization_has_a_fixed_wall_deadline(self):
        with mock.patch(
            "kukni.renderers.html.time.monotonic",
            side_effect=(0.0, 0.0, 3.0),
        ):
            with self.assertRaisesRegex(HtmlPreviewError, "sanitization timed out"):
                prepare_static_preview("<p>Static</p>")

    def test_embedded_data_image_remains_available_without_network(self):
        preview = prepare_static_preview(
            '<img alt="dot" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
        )

        self.assertFalse(preview.blocked_external_resources)
        self.assertIn(b"data:image/gif;base64", preview.document)

    def test_rejects_an_unknown_source_type(self):
        with self.assertRaises(TypeError):
            prepare_static_preview(object())

    def test_honours_cancellation_during_sanitization(self):
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 4

        with self.assertRaises(Exception) as caught:
            prepare_static_preview(
                "".join(f"<p>section {index}</p>" for index in range(100)),
                is_cancelled=cancelled,
            )

        self.assertEqual(type(caught.exception).__name__, "_PreviewCancelled")


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

    def test_capability_selection_never_runs_the_active_sandbox_probe(self):
        from gi.repository import Gio

        renderer = HtmlRenderer()
        with mock.patch(
            "kukni.renderers.html.webkit_runtime_available"
        ) as runtime_probe:
            self.assertTrue(
                renderer.supports(
                    Gio.File.new_for_path("/tmp/page.html"),
                    self._file_info("text/html"),
                )
            )

        runtime_probe.assert_not_called()

    def test_runtime_probe_starts_in_the_reader_worker(self):
        from gi.repository import Gio

        renderer = HtmlRenderer()
        cancellable = Gio.Cancellable()
        with (
            mock.patch(
                "kukni.renderers.html.webkit_runtime_available",
                return_value=False,
            ) as runtime_probe,
            mock.patch(
                "kukni.renderers.html.read_bounded_local_file",
                return_value=b"<h1>Static page</h1>",
            ),
            mock.patch("kukni.renderers.html.threading.Thread") as thread,
            mock.patch("kukni.renderers.html.GLib.idle_add") as idle_add,
        ):
            renderer.render(
                Gio.File.new_for_path("/tmp/page.html"),
                self._file_info("text/html"),
                cancellable,
                mock.Mock(),
                mock.Mock(),
            )
            runtime_probe.assert_not_called()
            thread.return_value.start.assert_called_once_with()

            worker = thread.call_args.kwargs["target"]
            worker()

        runtime_probe.assert_called_once_with()
        idle_add.assert_called_once()

    def test_js_only_shell_returns_notice_without_engine_probe(self):
        from gi.repository import Gio

        renderer = HtmlRenderer()
        with (
            mock.patch(
                "kukni.renderers.html.webkit_runtime_available"
            ) as runtime_probe,
            mock.patch(
                "kukni.renderers.html.read_bounded_local_file",
                return_value=b'<iframe src="https://invalid.example/app"></iframe>',
            ),
            mock.patch("kukni.renderers.html.threading.Thread") as thread,
            mock.patch("kukni.renderers.html.GLib.idle_add") as idle_add,
        ):
            renderer.render(
                Gio.File.new_for_path("/tmp/page.html"),
                self._file_info("text/html"),
                Gio.Cancellable(),
                mock.Mock(),
                mock.Mock(),
            )
            thread.call_args.kwargs["target"]()

        runtime_probe.assert_not_called()
        self.assertEqual(
            idle_add.call_args.args[0],
            renderer._deliver_active_notice,
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

    def test_applies_every_declared_lockdown_setting_headlessly(self):
        class FakeSettings:
            def __init__(self):
                self.values = {}

            def find_property(self, _name):
                return object()

            def set_property(self, name, value):
                self.values[name] = value

        settings = FakeSettings()

        apply_locked_down_settings(settings)

        self.assertEqual(settings.values, LOCKED_DOWN_SETTINGS)

    def test_default_size_limit_is_eight_mib(self):
        self.assertEqual(MAX_HTML_BYTES, 8 * 1024 * 1024)


class WebViewTerminationTests(unittest.TestCase):
    def test_static_load_deadline_is_five_seconds(self):
        self.assertEqual(LOAD_TIMEOUT_SECONDS, 5)

    def test_timeout_and_cancellation_teardown_stops_then_terminates(self):
        view = mock.Mock()

        stop_and_terminate_web_view(view)

        self.assertEqual(
            view.method_calls,
            [mock.call.stop_loading(), mock.call.terminate_web_process()],
        )

    def test_already_exited_web_process_does_not_break_teardown(self):
        view = mock.Mock()
        view.terminate_web_process.side_effect = RuntimeError("already exited")

        stop_and_terminate_web_view(view)

        view.stop_loading.assert_called_once_with()
        view.terminate_web_process.assert_called_once_with()


class RendererWorkerAdmissionTests(unittest.TestCase):
    def test_rapid_cancelled_selections_keep_only_one_latest_pending_worker(self):
        from gi.repository import Gio

        started = []

        class CapturedThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self):
                started.append(self)

        renderer = HtmlRenderer()
        info = RendererCapabilityTests._file_info("text/html")
        cancellations = [Gio.Cancellable() for _ in range(3)]
        with (
            mock.patch(
                "kukni.renderers.html.threading.Thread",
                CapturedThread,
            ),
            mock.patch(
                "kukni.renderers.html.read_bounded_local_file",
                return_value=b"<h1>Static page</h1>",
            ) as read_file,
            mock.patch(
                "kukni.renderers.html.webkit_runtime_available",
                return_value=False,
            ),
            mock.patch("kukni.renderers.html.GLib.idle_add"),
        ):
            for index, cancellable in enumerate(cancellations):
                if index:
                    cancellations[index - 1].cancel()
                renderer.render(
                    Gio.File.new_for_path(f"/tmp/page-{index}.html"),
                    info,
                    cancellable,
                    mock.Mock(),
                    mock.Mock(),
                )

            self.assertEqual(len(started), 1)
            started[0].target()
            self.assertEqual(len(started), 2)
            started[1].target()

        # The replaced middle request never consumes a worker or opens a path.
        self.assertEqual(read_file.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in read_file.call_args_list],
            ["/tmp/page-0.html", "/tmp/page-2.html"],
        )


if __name__ == "__main__":
    unittest.main()
