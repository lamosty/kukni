# Roadmap

This is a direction, not a promise or release schedule. Each milestone should remain useful and releasable on its own.

## 0.1 — Safe CR2 previews

- Canon CR2 embedded-JPEG extraction.
- GNOME Sushi 46 integration on Ubuntu 24.04.
- Bounded reads, parsing, dimensions, rendering, and child lifetime.
- Synthetic regression tests, private camera-corpus testing, CI, and safe per-user installation.

## 0.2 — Persistent standalone preview

- Implement a GTK4/libadwaita window independent of Sushi's private widget API.
- Implement the Nautilus preview D-Bus contract and preserve one session across navigation.
- Add a capability-based renderer registry and universal metadata/text/hex fallback.
- Treat `ready`, `fallback`, and `error` as in-window states; only explicit close actions end the session.
- Add native image, text/source, and PDF renderers.
- Move audio/video decoding into a disposable, network-denied worker with CPU, memory, output, and wall-clock limits before enabling automatic media routing.
- Add locked-down rendered HTML with a source-view toggle.
- Add bounded Office conversion for XLSX, DOCX, and PPTX, with spreadsheet sheet navigation as a follow-up.
- Add smooth zoom and pan, fit, 100% view, fullscreen, and keyboard-first controls.

## 0.3 — Camera workflow

- Verify and enable formats such as NEF, ARW, RAF, DNG, ORF, RW2, and PEF one family at a time.
- Add a stable extractor probe protocol for preview dimensions and format capabilities.
- Show privacy-aware EXIF details: camera, lens, exposure, focal length, timestamp, and orientation; hide GPS unless explicitly requested.
- Add failure messages that distinguish an unsupported format from a malformed or resource-limited file.

## 0.4 — Photography tools

- Add checkerboard transparency and color-management validation.
- Add optional histogram and clipping overlays for photography workflows.

## 0.5 — Desktop integrations and containment

- Support Sushi's current plugin API and investigate Glycin-based sandboxed decoding.
- Add well-scoped adapters for Nautilus first, then evaluate Nemo, Caja, and Dolphin.
- Define a sandbox profile with read-only access to exactly the selected file and no network.
- Fuzz extractors and publish a supported format/version matrix.

## Distribution milestones

- Adopt one Meson install layout for development, distro packages, and sandboxed builds.
- Publish checksummed `.deb` and `.rpm` artifacts from tagged releases.
- Maintain a signed APT repository after the package format stabilizes.
- Submit a Flatpak to Flathub with a minimal audited host adapter for file-manager integration.
- Evaluate Snap confinement only after the D-Bus and selected-file access model is proven under strict confinement.
- Prepare downstream-friendly metadata for Debian/Ubuntu, Fedora, openSUSE, and an AUR package.

## Possible later previews

- Fonts, archives, Markdown, and 3D assets where the desktop lacks a good safe preview.
- Audio waveforms and richer video metadata.
- A documented plugin API for third-party extractors.

Kukni should not duplicate a mature desktop renderer merely to increase a format count. Its focus is a consistent preview experience, missing high-value formats, and safer integration.
