# Changelog

All notable changes to this project will be documented here.

## Unreleased

### Standalone application

- Keep the latest photograph pending while a cancelled decoder finishes;
  rapid navigation no longer turns a temporarily busy worker into a failure.

- Establish Kukni as an independent GTK4/libadwaita file previewer with its own
  application identity and persistent window.
- Implement the Nautilus previewer D-Bus contract for Space-key activation,
  close behavior, parent-window association, and directional selection events.
- Preserve one session across navigation with cancellation, stale-result
  protection, stable sizing, a global preparation timeout, and in-window error
  states.
- Add a deterministic capability registry and a calm metadata-only fallback.
  Remove binary/hex inspection, duplicate filenames, and repeated failure toasts
  from the normal preview experience.
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
- Add automatic standalone Canon CR2 previews by extracting, orienting, and
  decoding the embedded display JPEG in a disposable worker, then accepting
  only strictly validated bounded raw RGBA in the GTK process.
- Bound each CR2 worker to one global preparation, a 128 MiB input, 64 MiB
  encoded and raw outputs, 4,096-pixel/16.8-megapixel retained frames, an
  eight-second wall deadline, 768 MiB address space, six CPU seconds, 64 file
  descriptors, `NPROC=0`, and verified `no_new_privs`.
- Add synthetic CR2 renderer and worker tests plus optional private
  camera-corpus coverage.
- Add CI coverage for the standalone runtime and installer behavior on Ubuntu
  24.04.
