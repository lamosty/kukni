# Architecture

Kukni is being built as a preview system with replaceable renderers and desktop integrations. The first implementation is deliberately small: a reusable, dependency-free RAW preview extractor and one GNOME Sushi integration.

## Current data flow

```text
Nautilus
  └─ GNOME Sushi 46 MIME dispatch
       └─ viewers/kukni.js
            ├─ starts a fixed per-user helper with an argv vector
            ├─ enforces a five-second child lifetime
            └─ incrementally decodes a bounded JPEG with GdkPixbuf
                 ▲
                 │ stdout only
       helpers/kukni-extract-preview.py
            ├─ opens one regular-file descriptor read-only
            ├─ applies container and parser-work budgets
            ├─ rejects RAW sensor JPEG frames and unsafe dimensions
            └─ emits one original embedded display JPEG
```

The extractor never develops RAW sensor data. Cameras normally store one or more ordinary JPEGs inside a RAW container for on-camera review and desktop thumbnails; using that image makes previews fast and color-correct according to the camera's own processing.

## Trust boundaries

- The selected path and all file bytes are untrusted input.
- The helper runs as the desktop user, with no network access requested and no file writes in its code path.
- The stdout byte stream crosses into GdkPixbuf, a native decoder in the Sushi process. Kukni limits its source and rendered dimensions, but this is not a security sandbox.
- Files under the user's Sushi data directory are executable plugin code and must be protected by normal user-account permissions.

## Why two processes?

Scanning the container in a child keeps parser CPU and memory separate from Sushi and lets the viewer terminate stalled work. The child emits a standard JPEG, so the desktop integration remains small and does not need a full RAW library.

This is containment, not isolation: the child has the same user identity as Sushi. A future standalone viewer should use the desktop's sandboxed image-loading facilities where available.

## Extension boundaries

Future work should preserve three layers:

1. **Extractors** accept a local file and return a bounded, typed preview plus non-sensitive metadata.
2. **Viewer core** owns zoom, navigation, metadata presentation, timeouts, cancellation, and sandbox policy.
3. **Desktop adapters** connect the viewer to Sushi, Nautilus, Nemo, Dolphin, or a standalone launcher without duplicating format parsing.

New RAW MIME types should be enabled only after a representative local corpus passes the same malformed-input and resource-limit tests. Format-specific parsing belongs in an extractor, not in a desktop adapter.

## Standalone viewer design

The standalone app will route each request through a capability registry:

```text
selected URI
  └─ type and capability probe
       ├─ native renderer: image, text/source, PDF, audio/video
       ├─ constrained web renderer: HTML
       ├─ bounded converter: RAW and Office documents
       └─ universal fallback: metadata plus safe text/hex inspection
```

Renderers produce content for one persistent preview window. A renderer may resolve to `ready`, `fallback`, or `error`; it may not close the session. The session controller owns the current URI, cancels stale asynchronous work, and ignores late results by generation number.

For Nautilus integration, Kukni can implement the user-session `org.gnome.NautilusPreviewer2` D-Bus contract. Arrow-key actions emit `SelectionEvent`; Nautilus remains responsible for choosing the adjacent item and responds with the next `ShowFile`. Kukni changes its current URI only on that call and keeps the window alive across unsupported files.

The `windowHandle` supplied by Nautilus is also the placement contract. On Wayland, Kukni should use the foreign parent handle so the compositor can center the preview over the originating Files window; applications must not attempt unsupported absolute positioning. The outer window chooses a stable viewport from the active monitor's work area and renderers fit inside it without resizing the top level.

HTML rendering requires JavaScript off, network requests blocked, and local resource access constrained. Office documents—including XLSX, DOCX, and PPTX—use a time- and output-bounded conversion worker with macros and external links disabled, then enter an existing PDF/image renderer. Unknown formats always reach the universal fallback.

## Compatibility policy

The current JavaScript integration subclasses private methods from Sushi 46. It is tested on Ubuntu 24.04 only and should fail honestly on incompatible versions rather than claiming broad support. Sushi 51 introduced a different plugin API; that integration should live beside, not silently replace, the legacy adapter.
