# Roadmap

Kukni's destination is simple: press Space on a file, understand it, and keep
moving. This roadmap is a direction rather than a release promise. Each stage
must stay useful, testable, and honest about its containment boundaries.

## Current alpha foundation

The `main` branch already provides:

- a standalone GTK4/libadwaita application;
- per-user Nautilus D-Bus activation and continuous arrow-key navigation;
- one persistent preview window with cancellation and stale-result protection;
- bounded text/source previews and a universal metadata/text/hex fallback;
- a bounded native XLSX view that does not execute formulas or active content;
- sandbox-gated HTML and first-page PDF routes;
- a conflict-checked per-user installer and ownership-aware uninstaller;
- synthetic parser, renderer, integration, and install tests.

This is enough to test Kukni as an independent previewer, but it is not yet a
broad-format daily-driver release. Images, camera RAW, audio, and video currently
use the fallback view.

## 0.1 — Standalone preview release

Before the first tagged release:

- complete clean-install and migration testing on Ubuntu 24.04/Nautilus 46;
- verify Space, close, activation, and directional navigation in a real user
  session after reboot or sign-in;
- add application icons and AppStream metadata;
- make missing core dependencies fail with a clear, actionable message;
- publish an explicit compatibility and renderer matrix;
- close alpha defects without weakening fallback or sandbox policy.

## 0.2 — Images and camera RAW

Make Kukni immediately useful for the most common visual files:

- add bounded JPEG, PNG, WebP, GIF, TIFF, and SVG preview paths;
- connect the existing CR2 embedded-JPEG extractor to the standalone app;
- keep native image decoders outside the long-lived GTK process where practical;
- pass bounded pixel payloads—not worker-selected encoded content—back to the UI;
- add fit, 100%, zoom, and pan without changing the outer window size;
- validate orientation, transparency, and color behavior;
- enable additional RAW families such as NEF, ARW, RAF, DNG, ORF, RW2, and PEF
  only after representative private corpora and malformed-input tests pass.

## 0.3 — Media containment

The media worker protocol and supervisor exist, but automatic routing stays off
until the remaining process-tree boundary is real and testable:

- place each worker in a disposable cgroup or equivalent aggregate task/memory
  boundary;
- prove the sandbox PID namespace is quiescent before accepting output;
- ship the required package-level sandbox policy;
- enable a bounded static video frame and inert audio metadata first;
- treat interactive playback, seeking, and waveforms as a separate IPC design.

## 0.4 — Documents and richer navigation

- add bounded DOCX and PPTX conversion without macros, remote resources, or a
  heavyweight office suite in the UI process;
- add XLSX sheet navigation and richer inert formatting;
- add a source-view toggle to HTML;
- add an optional information panel with privacy-aware metadata and GPS hidden by
  default;
- refine fullscreen, zoom, and keyboard discoverability;
- distinguish unsupported, malformed, dependency-limited, and resource-limited
  previews without terminating the session.

## 0.5 — Packages and updates

- publish signed source tags and checksummed release archives;
- produce a conventional `.deb` with dependency metadata and narrowly scoped
  AppArmor policy where needed;
- publish the verified package through a signed Launchpad PPA;
- add RPM and downstream packaging guidance from the same stable layout;
- evaluate Flatpak host integration;
- revisit Snap only after strict confinement can support the D-Bus and
  selected-file access model without classic confinement.

See [Packaging](PACKAGING.md) for channel-specific constraints.

## Later desktop integrations

Nautilus is first. Once its behavior is stable, define narrow adapters for other
file managers such as Nemo, Caja, and Dolphin without copying parser logic into
the adapter. Each integration needs its own verified lifecycle, selection, and
window-parenting contract.

## Possible later previews

- Fonts, archives, e-books, Markdown presentation, and 3D assets where a bounded
  preview adds real value.
- Photography overlays such as a histogram, clipping warnings, and checkerboard
  transparency.
- A documented renderer protocol for third-party format support.

Kukni should not chase a format-count headline. A renderer belongs in the
default route only when it keeps navigation continuous, fails safely, and is
pleasant enough to replace opening the full application for a quick look.
