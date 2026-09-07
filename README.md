# Kukni

[![Tests](https://github.com/lamosty/kukni/actions/workflows/test.yml/badge.svg)](https://github.com/lamosty/kukni/actions/workflows/test.yml)
[![License: GPL-2.0-or-later](https://img.shields.io/badge/license-GPL--2.0--or--later-blue.svg)](LICENSE)

## Quick Look for Linux

**Press Space. See the file. Keep moving.**

Kukni is a free, keyboard-first file previewer for Linux. It gives GNOME Files
(Nautilus) one stable preview window, so you can move through a folder with the
arrow keys instead of opening a full application for every file. Files stay for
browsing; applications stay for editing.

Kukni is its own GTK4 application. It does **not** require GNOME Sushi.

_Kukni_ is colloquial Slovak for “take a look.”

> [!IMPORTANT]
> Kukni is an early alpha, targeting Ubuntu 24.04 with Nautilus 46. Tested
> `.deb` downloads are available from successful main-branch CI runs. There is
> not yet a stable release or automatic-update repository; see the limits below.

## What already works

- Press <kbd>Space</kbd> on a local file in Nautilus to open or close Kukni.
- Move with <kbd>←</kbd>, <kbd>→</kbd>, <kbd>↑</kbd>, and <kbd>↓</kbd> while the
  same preview window stays open.
- Preview Canon CR2 photographs from the camera-generated JPEG already embedded
  in the RAW container, without developing or altering the sensor data.
- See PNG, JPEG, WebP, GIF, TIFF, BMP, and ICO images as pictures, including
  transparency and automatic embedded orientation. Animated files show a still.
- Read bounded, read-only previews of text, source code, configuration, logs,
  JSON, XML, CSV, Markdown, and similar files.
- Inspect the first visible worksheet of an XLSX file without starting an
  office suite, evaluating formulas, running macros, or following external
  links.
- Browse PDF pages with fit, zoom, and page controls when the required sandbox
  is installed. Optional HTML previews keep active content disabled.
- Use content-shaped windows, zoom and pan, and an on-demand Info panel.
  Changing a file keeps the same window alive; manual resizing takes precedence.
- Keep browsing unsupported files with a simple file card and a clear
  explanation, never a hex dump or binary-content inspection panel.
- Open a file directly with `kukni FILE`, or choose one inside the app with
  <kbd>Ctrl</kbd>+<kbd>O</kbd>.

Kukni accepts local regular files only. It does not fetch remote locations.

### Current format limits

| File kind | Current preview |
| --- | --- |
| Text and source | Read-only, bounded to the first 1 MiB; hidden controls are made visible |
| XLSX | Bounded native table for the first visible worksheet; cached values only |
| HTML | Available only with WebKitGTK 6 and a working process sandbox; scripts, network access, and broad local-file access stay disabled |
| PDF | Lazy page navigation through the first 500 pages, fit/zoom; requires a working bubblewrap sandbox |
| Canon CR2 | Camera-generated embedded JPEG, automatically oriented and fit to the window |
| PNG, JPEG, WebP, GIF, TIFF, BMP, ICO | Bounded image preview; static frame only; WebP requires its GdkPixbuf loader |
| SVG, HEIC, other images and camera RAW | File details; dedicated renderers are not connected yet |
| Audio and video | File details; automatic media decoding is deliberately disabled |
| Other local files | A plain “Preview unavailable” state with type and size; no byte inspection |

If an optional renderer or its sandbox is unavailable, Kukni falls back rather
than silently weakening the safety boundary.

### Fast CR2 without RAW development

Canon cameras normally store an ordinary display JPEG inside each CR2 for
on-camera review. Kukni extracts that image in a disposable worker, decodes and
downscales it there, and gives the GTK process only validated raw RGBA pixels.
The original CR2 is opened read-only and never altered.

The default worker limits are explicit:

- 128 MiB maximum CR2 input;
- 64 MiB maximum embedded JPEG and 64 MiB maximum returned RGBA payload;
- 32,768 pixels per source edge and 100 megapixels total;
- 4,096 pixels per retained edge and 16.8 megapixels total;
- one preparation at a time, held through GTK delivery;
- an eight-second wall deadline, 768 MiB address-space limit, six CPU seconds,
  64 open descriptors, hard `NPROC=0`, and verified
  `PR_SET_NO_NEW_PRIVS`.

The worker is killable and the parent strictly validates its output, but the
source install cannot give it a filesystem or network namespace on the current
Ubuntu target. During its short lifetime it still has ordinary same-user
filesystem and network access. The descriptor-only protocol is an intended
access boundary, not a complete sandbox.

Ordinary raster images reuse this same process boundary, with a 64 MiB input
limit and the same pixel, CPU, memory, and deadline limits. Raster/CR2 workers
remain process-bounded rather than filesystem/network isolated in the current
Ubuntu package too; its namespace policy enables the separate PDF/HTML paths.

## Install on Ubuntu 24.04

**Use the Ubuntu package for everyday previews. Do not install a source copy
into `~/.local` to develop Kukni.** They are separate workflows:

| Purpose | Workflow |
| --- | --- |
| Preview files with Space in Nautilus | Install the tested `.deb` once; update it with APT when a newer package is downloaded |
| Edit and try the code | `make dev` from the checkout; no installation and no Space-key takeover |
| Verify the desktop installation | `make check` or `/usr/bin/kukni --check` |
| Build a reviewable package | `make package` from a clean checkout with full Git history |

### Download a tested alpha

1. Open [Tests](https://github.com/lamosty/kukni/actions/workflows/test.yml) and
   choose a successful **main-branch push** run. Older runs may have no artifact.
2. Download its `kukni-ubuntu-24.04-…` artifact and extract the ZIP into an empty
   directory. It contains the **same package that passed installed PNG/PDF
   self-tests**, plus `SHA256SUMS`.
3. In that directory, verify the checksum, then install the one downloaded
   package (replace `VERSION` with its actual filename):

```sh
sha256sum --check SHA256SUMS
sudo apt install ./kukni_VERSION_all.deb
/usr/bin/kukni --check
```

The package declares its runtime dependencies and includes the Kukni-specific
AppArmor namespace permission needed for the PDF sandbox on Ubuntu 24.04. It
conflicts with `gnome-sushi`, which provides the same Nautilus preview service;
APT shows that replacement before installation. No global security setting is
changed.

These are **expiring CI alpha downloads**, not signed public releases. GitHub
requires signing in to download workflow artifacts. Checksums detect corruption;
they are not a publisher signature. A stable download page and signed APT updates
remain planned. [GitHub's artifact download guide](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)
explains the current download route.

### Already used `./install.sh`?

A newer package does **not** replace files inside your home directory. Old
per-user D-Bus files can still make Space launch an obsolete Kukni—even when the
new package's image and PDF renderers work correctly.

After the package has installed successfully:

1. Close any Kukni preview window.
2. Run the old copy's ownership-checking uninstaller **without sudo**:

   ```sh
   ~/.local/lib/kukni/uninstall.sh
   ```

3. Run `/usr/bin/kukni --check`, then test Space in Nautilus. If an old process
   still owns the preview service, close it; sign out and back in if necessary.

Do not use `--force` if the uninstaller reports modified or unexpected files;
review them first. For custom-prefix installs use that prefix's installed
uninstaller. A current checkout also offers `./uninstall.sh --dry-run` to inspect
the removal plan without changing installed files.

New package checks treat shadowing activation as a **failure**, not an optional
warning. They report whether a running preview owner belongs to the package;
they never start or stop desktop applications. No running owner is normal before
Space is pressed. Source checks do not certify the desktop installation.

### Build locally instead

```sh
git clone https://github.com/lamosty/kukni.git
cd kukni
make package
```

Install the exact output filename printed by the builder, not a wildcard over
old builds. Package building changes no system files and needs no root. See
[Packaging](docs/PACKAGING.md) for the layout, migration, and release policy.

## Controls

| Key | Action |
| --- | --- |
| <kbd>Space</kbd> or <kbd>Esc</kbd> | Close the preview |
| Arrow keys | Ask Nautilus for the adjacent selection |
| <kbd>F</kbd> or <kbd>F11</kbd> | Toggle fullscreen |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> | Choose a file directly |
| <kbd>+</kbd> / <kbd>−</kbd> | Zoom the image or PDF preview |
| <kbd>0</kbd> / <kbd>1</kbd> | Fit / 1:1 retained preview pixels |
| <kbd>Ctrl</kbd>+wheel / drag | Zoom / pan an enlarged preview |
| <kbd>Page Up</kbd> / <kbd>Page Down</kbd> | Previous / next PDF page |
| <kbd>Ctrl</kbd>+<kbd>I</kbd> | Show or hide file information |

Arrow-key folder navigation is available when Nautilus opened the preview.
1:1 refers to the retained preview, not full-source detail for downscaled images
or vector PDF pages; the control tooltip explains this limit.

## Remove Kukni

For the Ubuntu package:

```sh
sudo apt remove kukni
```

Use `sudo apt purge kukni` to also remove its package configuration. Removing
Kukni does not reinstall another previewer. The legacy per-user uninstaller
above removes only its own manifest-verified files and never removes the package.

## Development

Install the development dependencies once (the installed Kukni package already
provides the core runtime):

```sh
sudo apt install make git python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
  util-linux webp-pixbuf-loader xvfb xauth
```

Then edit the checkout and relaunch:

```sh
make dev
make dev FILE=/path/to/picture.png
```

This runs the current working tree as **Kukni Development**, with a separate
application identity and no Nautilus preview registration. It never copies code
into `~/.local`, changes activation files, or forwards your request to an old
installed instance. Use Ctrl+O to choose another file. Folder arrow navigation
belongs to the installed Nautilus integration, not this standalone dev window.

The development mode does **not** borrow the package launcher's AppArmor
permission. PDF/HTML may therefore be unavailable from source on Ubuntu even
when they work in the installed app. Test the full sandboxed runtime through the
package and installed-package CI; do not disable security to make a dev preview
pass. `./bin/kukni --version` identifies the checkout revision and dirty state;
`/usr/bin/kukni --version` identifies the installed package.

Plain `./install.sh` and `make install` now stop without installing anything.
The old copier is retained behind `--legacy-user-install` only for explicit
compatibility work; it is not the development workflow.

Run the parser, renderer, safety, and legacy-migration tests:

```sh
make test
```

Run GTK smoke tests in an isolated display when the required tools and optional
renderers are installed:

```sh
make test-ui
```

After installing a package, `make test-installed` separately checks its D-Bus
activation, process origin, ShowFile, and Close in a private display/session.
It never changes your desktop registration. Visibility is not treated as proof
of rendered pixels; the image/PDF render checks remain separate requirements.

To test the bounded CR2 extractor against a private camera corpus without
committing photographs:

```sh
CR2_SAMPLE_DIR=/path/to/samples make test-corpus
```

Sample RAW files are deliberately ignored by Git. Read
[CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change.

Design details live in [Architecture](docs/ARCHITECTURE.md), next work is in
the [Roadmap](docs/ROADMAP.md), and interaction invariants are in
[UX principles](docs/UX_PRINCIPLES.md).

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
