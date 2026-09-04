# Contributing

Thanks for helping build a faster, calmer, and safer Quick Look experience for
Linux.

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
