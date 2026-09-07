# Contributing

Thanks for helping build a faster, calmer, and safer Quick Look experience for
Linux.

## Edit/run loop

Use `make dev` (or `make dev FILE=/path/to/image.png`) from the checkout. Each
launch uses the current source, identifies itself as Kukni Development, and
cannot register Nautilus's preview service or forward to the installed app.
Close and relaunch after edits; no copied installation or package rebuild is
needed for this loop. `./install.sh` is a deprecated compatibility path, not a
development setup command.

Use `make check` for the installed package and `make package` only to prepare an
actual upgrade. Package versions require clean commits and complete Git history.
Source PDF/HTML may fall back under Ubuntu namespace restrictions; do not alter
system security to hide that difference or borrow another app's profile.
Installed-package CI must render a real PNG, PDF, and fixed static HTML page.
The explicit `/usr/bin/kukni --check-html` requires a display and briefly opens a
synthetic test window; `make test-installed` runs it on an isolated display with
an outer hard timeout. Default `--check` remains headless. See the README for
one-time development dependencies.

## Before opening a pull request

1. Read [Architecture](docs/ARCHITECTURE.md) and preserve the separation between
   desktop integration, session state, renderer selection, and bounded parsing.
2. Run `make test`; run `make test-ui` in an isolated display when changing GTK
   behavior.
3. Test malformed inputs as well as normal files whenever parser or renderer
   behavior changes. Prefer synthetic fixtures.
4. Keep input, output, work, process, and time limits explicit. A compatibility
   improvement must not silently remove a safety boundary.
5. Make renderer dependency and sandbox failure fall back honestly instead of
   weakening the required boundary.
6. Explain which desktop, distribution, and renderer versions were tested.
7. Update user-facing format and shortcut claims in the same change that alters
   them.

## Test files and privacy

Do not commit personal photographs, document contents, camera serial numbers,
GPS metadata, proprietary sample corpora, crash dumps, or minimized files that
still contain private metadata. RAW extensions are ignored by Git on purpose.

The unit suite creates synthetic byte streams and container fixtures. For local
CR2 integration coverage, point `CR2_SAMPLE_DIR` at a private directory:

```sh
CR2_SAMPLE_DIR=/path/to/private/samples make test-corpus
```

Only test results—not the files—should be shared in an issue or pull request. If
a reproducer is essential, create the smallest synthetic file possible and
confirm its metadata before attaching it.

## Style

- Python targets Python 3.10 and later and uses four-space indentation.
- Shell scripts are POSIX `sh`, use `set -eu`, and must pass `sh -n`.
- Keep comments close to the constraint or decision they explain.
- Keep commits focused and include regression tests for bug fixes.

Security problems should follow [SECURITY.md](SECURITY.md), not a public issue.
