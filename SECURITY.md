# Security policy

## Supported versions

Kukni has not published a tagged release yet. Security fixes are applied to the
latest `main` revision on a best-effort basis during this alpha period. The
current desktop validation target is Ubuntu 24.04 with Nautilus 46; other
combinations have not yet earned a compatibility claim.

## Reporting a vulnerability

Please use a [private GitHub security advisory](https://github.com/lamosty/kukni/security/advisories/new).
Do not open a public issue for a vulnerability before a fix is available.

Include the Kukni revision, operating system, Nautilus version, affected renderer,
sandbox availability, impact, and the smallest safe reproduction you can
provide. Do not upload personal photos, documents, or media: they can contain
private content, timestamps, author identities, device identifiers, location
metadata, and embedded thumbnails. Prefer a synthetic or metadata-scrubbed
reproducer.

## Security model

Kukni runs as the logged-in desktop user. Every selected path, metadata value,
container structure, and file byte is untrusted. Kukni must not execute a
selected file, pass it to a shell, launch its default application, evaluate a
document formula or macro, or fetch a remote resource as a side effect of
previewing.

The standalone application currently applies these boundaries:

- only native local regular files reach content renderers;
- text, fallback, XLSX, PDF, and HTML paths have explicit input and output
  ceilings plus cancellation or deadlines;
- the XLSX parser ignores active parts and external relationships and exposes
  cached formula values only;
- CR2 extraction, JPEG decode, embedded orientation, and downscaling run in one
  killable worker; the UI accepts only strictly validated bounded raw RGBA and
  creates a memory texture without decoding worker-selected encoded content;
- HTML requires WebKitGTK's process sandbox and disables JavaScript, networking,
  media, forms, broad local-file access, and other active features;
- PDF rendering occurs in a short-lived bubblewrap namespace behind process
  resource limits and a wall deadline;
- optional sandbox-gated routes fall back when the required boundary cannot be
  established;
- asynchronous results are generation-checked so canceled work cannot replace a
  newer preview.

These are defense-in-depth measures, not a claim that the long-lived GTK process
is fully isolated from untrusted content. Native toolkit and image-decoding code
still handles bounded renderer output in some paths, and Python parsers run with
the user's identity. Keep the operating system and rendering libraries updated.

The CR2 worker admits one preparation at a time and enforces a 128 MiB input
limit, 64 MiB limits for both the embedded JPEG and raw RGBA output, source
limits of 32,768 pixels per edge and 100 megapixels, and retained-frame limits
of 4,096 pixels per edge and 16.8 megapixels. It also has an eight-second wall
deadline, 768 MiB address-space limit, six CPU seconds, 64 open descriptors,
`NPROC=0`, and verified `PR_SET_NO_NEW_PRIVS` before it reads untrusted CR2
bytes.

That worker is process-contained but not fully sandboxed in the source install.
It has no filesystem or network namespace, so compromised native decoder code
would retain ordinary same-user filesystem, network, IPC, and signalling access
during the worker's short lifetime. Passing fixed inherited descriptors is the
protocol's intended access pattern; it is not a filesystem or network security
boundary.

## Disabled paths and current gaps

Automatic audio/video routing remains disabled. The disposable media worker has
network and PID namespaces, per-process resource limits, fixed descriptors, a
hard parent deadline, and strict output validation, but it lacks a proven
aggregate process-tree/task boundary for compromised decoder code.

Common JPEG, PNG, and other image formats plus non-CR2 camera RAW formats do not
yet have rich standalone routes. They use the universal fallback. No GNOME
Sushi plugin integration ships with Kukni.

## Installation boundary

The source installer is designed for an unprivileged user prefix. Do not run it
with `sudo`. It uses a manifest to avoid overwriting or removing unexpected
files. A source installation cannot add the narrowly scoped system AppArmor
profile that some sandboxed renderers may require on Ubuntu; those renderers
must remain unavailable rather than bypass policy.
