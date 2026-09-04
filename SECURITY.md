# Security policy

## Supported versions

Security fixes are provided for the latest tagged release on a best-effort basis. The plugin is currently tested only with GNOME Sushi 46 on Ubuntu 24.04.

## Reporting a vulnerability

Please use a [private GitHub security advisory](https://github.com/lamosty/kukni/security/advisories/new). Do not open a public issue for a vulnerability before a fix is available.

Include the affected version, operating system, Sushi version, impact, and the smallest safe reproduction you can provide. Do not upload personal CR2 files: they can contain full-resolution photos, timestamps, camera identifiers, and GPS metadata. Prefer a synthetic or metadata-scrubbed reproducer.

## Security boundaries

Kukni runs as the logged-in desktop user. It does not provide isolation from the host or from GdkPixbuf; its limits are defense in depth against malformed local files. The helper is designed to read only the selected regular file, write the extracted preview only to stdout, make no network connections, and terminate when Sushi closes the preview.
