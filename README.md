# Kukni

[![Tests](https://github.com/lamosty/kukni/actions/workflows/test.yml/badge.svg)](https://github.com/lamosty/kukni/actions/workflows/test.yml)
[![License: GPL-2.0-or-later](https://img.shields.io/badge/license-GPL--2.0--or--later-blue.svg)](LICENSE)

**Press Space. See the file.**

Kukni is an open-source project for fast, native-feeling file previews on Linux. Its first working backend adds instant Canon CR2 previews to GNOME Files (Nautilus) through Sushi; the project is growing into an independent preview app with broad format support.

Select a `.cr2` file in Files and press <kbd>Space</kbd>. Kukni finds the camera-generated JPEG already embedded in the CR2 and displays it without developing or altering the RAW image.

_Kukni_ is colloquial Slovak for “take a look.”

## Current status

- The working preview backend supports Canon CR2 files with an embedded display JPEG.
- The legacy desktop adapter is tested on Ubuntu 24.04 with GNOME Sushi 46.
- Uses Sushi's legacy per-user viewer interface. Sushi 51 and newer have a different plugin API and are not currently supported.
- Local files only; remote `Gio.File` locations fall back to Sushi's normal behavior.

The RAW extractor is distro-neutral plain Python. The current desktop adapter is version-sensitive because it integrates with Sushi; the planned standalone viewer and Flatpak distribution remove that limitation. Kukni is not a RAW editor or a replacement for [LibRaw](https://www.libraw.org/).

## Install

Install Sushi first. On Debian and Ubuntu:

```sh
sudo apt install gnome-sushi
```

On Fedora:

```sh
sudo dnf install sushi
```

On another distribution, install its GNOME Sushi package and verify that it provides the legacy viewer interface. Ubuntu 24.04/Sushi 46 is the currently verified combination.

Then install the plugin for your user—do not use `sudo`:

```sh
git clone https://github.com/lamosty/kukni.git
cd kukni
./install.sh
```

If Sushi was already running, close the preview and reload it once:

```sh
pkill -x sushi
```

Now select a CR2 file in Files and press <kbd>Space</kbd>.

The installer writes only these two files:

```text
~/.local/share/sushi/viewers/kukni.js
~/.local/share/sushi/helpers/kukni-extract-preview.py
```

It refuses to overwrite different files unless `--force` is explicitly supplied.

## How it works

```text
Nautilus → Sushi integration → bounded Kukni extractor → embedded JPEG → GdkPixbuf
```

The viewer passes the selected path as a literal subprocess argument—there is no shell command. The helper opens the file read-only, accepts only a regular file, validates JPEG marker boundaries, excludes the lossless JPEG that contains RAW sensor data, and emits the largest safe display frame. Sushi decodes the result incrementally and scales it to at most 4096 × 4096 pixels.

Safety limits include:

- 128 MiB CR2 container size;
- 64 MiB embedded JPEG size;
- shared scan-byte, candidate, and marker budgets;
- 32,768 pixels per source edge and 100 megapixels total;
- a five-second extraction timeout;
- child-process cleanup on error and preview closure.

The plugin never writes to the selected photo and performs no network access. GdkPixbuf decoding still happens in the user's Sushi process, so this is not a security sandbox. Keep the operating system updated and treat files from unknown sources with appropriate caution.

## Uninstall

From the cloned repository:

```sh
./uninstall.sh
```

The uninstaller refuses to remove files that differ from this checkout unless `--force` is supplied.

## Development

Run the synthetic parser, safety, installer, and JavaScript syntax tests:

```sh
make test
```

To test a private local camera corpus without committing photos:

```sh
CR2_SAMPLE_DIR=/path/to/samples make test-corpus
```

Sample RAW files are deliberately ignored by Git. See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change.

The current design and extension boundaries are documented in [Architecture](docs/ARCHITECTURE.md). Planned work is kept in the [Roadmap](docs/ROADMAP.md), interaction invariants live in [UX principles](docs/UX_PRINCIPLES.md), and the distribution plan is in [Packaging](docs/PACKAGING.md).

## Why a plugin?

GdkPixbuf's historical RAW loader is not shipped by Ubuntu, so Sushi 46 cannot preview CR2 files out of the box. Modern GNOME is moving image loading toward sandboxed [Glycin](https://gitlab.gnome.org/GNOME/glycin); contributing RAW support there is the better long-term direction. Kukni fills the practical gap today while leaving room for additional desktop integrations.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
