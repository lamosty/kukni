# Security policy

## Supported versions

Kukni has not published a tagged release yet. Security fixes are applied to the latest `main` revision on a best-effort basis during this pre-release period. The legacy CR2 plugin is currently tested only with GNOME Sushi 46 on Ubuntu 24.04; the standalone GTK app remains a source-only prototype.

## Reporting a vulnerability

Please use a [private GitHub security advisory](https://github.com/lamosty/kukni/security/advisories/new). Do not open a public issue for a vulnerability before a fix is available.

Include the affected version, operating system, Sushi version, impact, and the smallest safe reproduction you can provide. Do not upload personal CR2 files: they can contain full-resolution photos, timestamps, camera identifiers, and GPS metadata. Prefer a synthetic or metadata-scrubbed reproducer.

## Security boundaries

Kukni runs as the logged-in desktop user. Every selected path and file byte is treated as untrusted data; Kukni never executes the selected file or hands it to a shell or default application.

The legacy CR2 adapter does not isolate GdkPixbuf from the host. Its extraction and decoding limits are defense in depth, and it should be used with the same caution as other desktop image viewers.

The standalone prototype uses bounded native parsing for simple formats and requires sandbox availability for HTML and PDF rendering. Its media worker has a network/PID namespace, fixed descriptor access, per-process resource limits, a hard parent deadline, and strict raw-frame validation. Automatic media routing remains disabled until an aggregate process-tree/no-fork limit and packaged sandbox integration tests are complete. When a required boundary is unavailable, the renderer must fall back without weakening or disabling the sandbox.
