# Contributing

Thanks for helping make Linux file previews safer, faster, and more consistent.

## Before opening a pull request

1. Keep the project dependency-free unless a dependency has a clear security and maintenance benefit.
2. Run `make test`; run `make test-ui` in an isolated display when changing GTK behavior.
3. Test malformed inputs as well as normal files whenever parser or renderer behavior changes. Prefer synthetic fixtures.
4. Keep input and decoding limits explicit. A compatibility improvement must not silently remove a safety boundary.
5. Explain which desktop, distribution, and relevant renderer versions were tested.

## Test files and privacy

Do not commit personal photographs, camera serial numbers, GPS metadata, proprietary sample corpora, crash dumps, or minimized files that still contain private metadata. RAW extensions are ignored by Git on purpose.

The unit suite creates synthetic JPEG-like byte streams. For local integration coverage, point `CR2_SAMPLE_DIR` at a private directory:

```sh
CR2_SAMPLE_DIR=/path/to/private/samples make test-corpus
```

Only test results—not the files—should be shared in an issue or pull request. If a reproducer is essential, create the smallest synthetic file possible and confirm its metadata before attaching it.

## Style

- Python targets Python 3.10 and later and uses four-space indentation.
- JavaScript follows the style of Sushi 46's GJS viewers.
- Shell scripts are POSIX `sh`, use `set -eu`, and must pass `sh -n`.
- Keep commits focused and write tests for bug fixes.

Security problems should follow [SECURITY.md](SECURITY.md), not a public issue.
