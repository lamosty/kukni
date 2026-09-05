# Changelog

All notable changes to this project will be documented here.

## Unreleased

### Standalone application

- Add real PNG, JPEG, WebP, GIF, TIFF, BMP, and ICO previews using the bounded
  CR2 pixel-worker contract, with orientation, transparency, and dimension limits.
- Require real image-worker success and run isolated GTK smoke tests in CI;
  visual XML formats no longer masquerade as successful source previews.

- Keep the latest photograph pending while a cancelled decoder finishes;
  rapid navigation no longer turns a temporarily busy worker into a failure.
- Add content-aware, monitor-bounded window sizing, shared image/PDF zoom and
  pan, and file details on demand; preserve manual resizing and the same window.
- Bind navigation to the originating file-manager session and detach it on
  standalone launch, file choice, caller disappearance, or session closure.
- Cache recent validated image pixels in memory with byte/entry/age limits and
  file-change invalidation; no persistent image cache is written.

- Establish Kukni as an independent GTK4/libadwaita file previewer with its own
  application identity and persistent window.
- Implement the Nautilus previewer D-Bus contract for Space-key activation,
  close behavior, parent-window association, and directional selection events.
- Preserve one session across navigation with cancellation, stale-result
  protection, a global preparation timeout, and in-window error
  states.
- Add a deterministic capability registry and a calm metadata-only fallback.
  Remove binary/hex inspection, duplicate filenames, and repeated failure toasts
  from the normal preview experience.
- Add bounded, read-only text/source previews that expose deceptive controls and
  never launch executable content.
- Add a bounded XLSX parser and native spreadsheet view without formula
  evaluation, macros, external links, network access, or an office-suite
  dependency.
- Add sandbox-gated HTML previews and bounded lazy PDF page navigation, with
  shared fit/zoom controls and cancellation of obsolete page requests.
- Add an isolated media-worker protocol and strict parent supervisor for one
  bounded video frame or inert audio metadata. Keep automatic media routing
  disabled pending aggregate process-tree containment and packaged sandbox
  tests.

### Installation and compatibility

- Add a rootless Ubuntu `.deb` builder with runtime dependencies and app-scoped
  namespace permission for its root-owned launcher. Add headless `--check`
  diagnostics that require actual core image/PDF rendering and an installed
  package CI gate; document source-install migration and capability limits.

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
