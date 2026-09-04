# Changelog

All notable changes to this project will be documented here.

## Unreleased

### Standalone application

- Establish Kukni as an independent GTK4/libadwaita file previewer with its own
  application identity and persistent window.
- Implement the Nautilus previewer D-Bus contract for Space-key activation,
  close behavior, parent-window association, and directional selection events.
- Preserve one session across navigation with cancellation, stale-result
  protection, stable sizing, a global preparation timeout, and in-window error
  states.
- Add a deterministic capability registry and universal metadata/text/hex
  fallback for local regular files without a rich renderer.
- Add bounded, read-only text/source previews that expose deceptive controls and
  never launch executable content.
- Add a bounded XLSX parser and native spreadsheet view without formula
  evaluation, macros, external links, network access, or an office-suite
  dependency.
- Add sandbox-gated HTML previews with active features disabled and first-page
  PDF rendering in a resource-limited disposable worker.
- Add an isolated media-worker protocol and strict parent supervisor for one
  bounded video frame or inert audio metadata. Keep automatic media routing
  disabled pending aggregate process-tree containment and packaged sandbox
  tests.

### Installation and compatibility

- Add a conflict-checked standalone per-user installer with desktop and D-Bus
  activation metadata, an ownership manifest, transactional replacement, and a
  self-contained uninstaller.
- Make the normal installation independent of GNOME Sushi.
- Remove the original GNOME Sushi viewer adapter and its plugin-specific
  installation path.
- Continue to provide bounded Canon CR2 embedded-JPEG extraction with synthetic
  tests and optional private camera-corpus coverage; the extractor is not yet
  connected to the standalone renderer registry.
- Add CI coverage for the standalone runtime and installer behavior on Ubuntu
  24.04.
