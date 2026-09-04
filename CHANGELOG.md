# Changelog

All notable changes to this project will be documented here.

## Unreleased

- Add Canon CR2 preview support for GNOME Sushi 46.
- Extract embedded display JPEGs without external RAW libraries.
- Bound container reads, parser work, encoded preview size, source dimensions, rendered dimensions, and helper runtime.
- Add safe per-user install and uninstall scripts.
- Add synthetic tests plus an optional private camera-corpus test.
- Keep the experimental in-process audio/video renderer available for isolated tests, but disable automatic media routing until disposable worker isolation exists.
