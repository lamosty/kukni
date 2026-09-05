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
> Kukni is an early alpha with no published binary release yet. The current
> validation target is Ubuntu 24.04 with Nautilus 46. The repository includes a
> local Ubuntu package builder; read the installation and format limits below.

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

## Ubuntu package

The package declares its runtime dependencies and includes a Kukni-specific
AppArmor namespace permission so its mandatory PDF sandbox can start on Ubuntu
24.04. It does not disable system security or make PDF rendering unconfined.

There is no published binary release yet. To build from a clean reviewed checkout:

```sh
git clone https://github.com/lamosty/kukni.git
cd kukni
python3 packaging/build-deb.py
sudo apt install ./dist/kukni_*.deb
/usr/bin/kukni --check
```

`--check` must succeed for core images and PDF; it actually renders synthetic
content rather than accepting an unavailable-preview fallback. Optional HTML
prerequisites are reported separately.

**Already using the per-user installer?** Close the old preview and run
`~/.local/lib/kukni/uninstall.sh` **without sudo** before installing the package.
Otherwise its per-user launcher or activation file can shadow the new package.
The package conflicts with `gnome-sushi` because both provide Nautilus's preview
service; APT shows that replacement before installation. See
[Packaging](docs/PACKAGING.md) for migration and removal details.

## Per-user source installation (development)

The source installer remains useful for development, but cannot install the
system sandbox policy. PDF and HTML may remain unavailable even with their
runtime packages installed. On Ubuntu 24.04, install the source dependencies:

```sh
sudo apt install git python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 util-linux webp-pixbuf-loader
```

Then install Kukni without `sudo`:

```sh
git clone https://github.com/lamosty/kukni.git
cd kukni
./install.sh
```

The installer places the application under `~/.local`, adds a desktop entry,
and registers Kukni as your Nautilus preview service. It checks conflicts and
will not overwrite modified or unowned files unless you explicitly use
`--force` after reviewing them.

Select a local file in Nautilus and press <kbd>Space</kbd>. If another preview
service was already running during installation, sign out and back in once so
the new user-session activation takes effect.

You can also launch Kukni directly:

```sh
kukni /path/to/file
```

If `~/.local/bin` is not on your `PATH`, use `~/.local/bin/kukni` or add that
directory to your shell configuration.

### Optional renderers

On Ubuntu, install the optional PDF and HTML runtime packages with:

```sh
sudo apt install poppler-utils bubblewrap gir1.2-webkit-6.0
```

Installing those packages does not guarantee that the host's user-namespace
policy permits the sandbox; Kukni checks at runtime and falls back safely when
it does not.

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

## Uninstall the per-user source copy

Do not use `sudo`:

```sh
./uninstall.sh
```

You can also run the installed copy at
`~/.local/lib/kukni/uninstall.sh`. The uninstaller verifies Kukni's ownership
manifest and refuses to remove modified or unexpected files unless `--force`
is explicitly supplied.

### Migrating from GNOME Sushi

Sushi is neither installed nor used by the default Kukni setup. If it is still
installed from an earlier setup, verify that Kukni opens from Nautilus first.
Ubuntu users may then remove the old package with:

```sh
sudo apt remove gnome-sushi
```

Kukni does not ship or install a Sushi plugin.

## Packages and releases

There is no public Kukni APT repository or Snap release today. The local `.deb`
builder is the first packaging step; a signed Launchpad PPA and normal APT
updates come after installed-package validation. Snap is not the first target
because its confinement must support Nautilus's session D-Bus contract and
access to arbitrary selected files.

See [Packaging](docs/PACKAGING.md) for the release plan.

## Development

Run the parser, renderer, safety, and installer tests:

```sh
make test
```

Run GTK smoke tests in an isolated display when the required tools and optional
renderers are installed:

```sh
make test-ui
```

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
