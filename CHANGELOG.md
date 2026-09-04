# Changelog

All notable changes to this project will be documented here.

## Unreleased

- Add Canon CR2 preview support for GNOME Sushi 46.
- Extract embedded display JPEGs without external RAW libraries.
- Bound container reads, parser work, encoded preview size, source dimensions, rendered dimensions, and helper runtime.
- Add safe per-user install and uninstall scripts.
- Add synthetic tests plus an optional private camera-corpus test.
- Add a source-only standalone GTK preview shell with a persistent session, Nautilus D-Bus navigation, stable sizing, universal fallback, cancellation, stale-result protection, and a global preparation timeout.
- Add bounded text/source and GPX previews that never execute selected files.
- Add a bounded XLSX parser and native spreadsheet view without macros, external links, network access, or a LibreOffice dependency.
- Add sandbox-gated HTML previews and resource-limited, fit-page PDF rendering.
- Keep the experimental in-process audio/video renderer available for isolated tests, but remove it from automatic routing.
- Add a strict disposable-media protocol, a PAUSED-state GStreamer worker that emits one bounded raw RGBA frame or audio metadata, and a fail-closed parent supervisor. Media remains disabled by default pending aggregate process-tree containment and packaged sandbox tests.
