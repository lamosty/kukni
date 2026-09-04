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
> Kukni is an early alpha with no packaged release yet. The current validation
> target is Ubuntu 24.04 with Nautilus 46. Install from a reviewed source
> checkout, expect rough edges, and read the format limits below.

## What already works

- Press <kbd>Space</kbd> on a local file in Nautilus to open or close Kukni.
- Move with <kbd>←</kbd>, <kbd>→</kbd>, <kbd>↑</kbd>, and <kbd>↓</kbd> while the
  same preview window stays open.
- Read bounded, read-only previews of text, source code, configuration, logs,
  JSON, XML, CSV, Markdown, and similar files.
- Inspect the first visible worksheet of an XLSX file without starting an
  office suite, evaluating formulas, running macros, or following external
  links.
- Render locked-down HTML and the first page of a PDF when their optional
  engines and required sandbox are available.
- Get a useful metadata plus text-or-hex inspection view for every other local
  regular file instead of losing the preview session.
- Open a file directly with `kukni FILE`, or choose one inside the app with
  <kbd>Ctrl</kbd>+<kbd>O</kbd>.

Kukni accepts local regular files only. It does not fetch remote locations.

### Current format limits

| File kind | Current preview |
| --- | --- |
| Text and source | Read-only, bounded to the first 1 MiB; hidden controls are made visible |
| XLSX | Bounded native table for the first visible worksheet; cached values only |
| HTML | Available only with WebKitGTK 6 and a working process sandbox; scripts, network access, and broad local-file access stay disabled |
| PDF | First page only, using Poppler inside a working bubblewrap sandbox |
| Images and camera RAW, including CR2 | Metadata plus bounded inspection fallback; a standalone image/RAW renderer is not connected yet |
| Audio and video | Metadata plus bounded inspection fallback; automatic media decoding is deliberately disabled |
| Other local files | Metadata plus a bounded text or hex sample |

If an optional renderer or its sandbox is unavailable, Kukni falls back rather
than silently weakening the safety boundary.

## Install for your user

Kukni currently installs from source. On Ubuntu 24.04, install the core runtime
dependencies:

```sh
sudo apt install git python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
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
sudo apt install poppler-utils bubblewrap util-linux gir1.2-webkit-6.0
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

Arrow-key folder navigation is available when Nautilus opened the preview.

## Uninstall

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

There is no Kukni APT repository, `.deb`, or Snap release today. The planned
Ubuntu path is a reviewable `.deb` followed by a signed Launchpad PPA, so a
future install can use normal `apt` updates. Snap is not the first packaging
target because its confinement must be reconciled with Nautilus's session
D-Bus contract and access to arbitrary selected files.

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
