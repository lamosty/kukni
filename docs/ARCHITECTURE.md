# Architecture

Kukni is a preview system with replaceable renderers and desktop integrations. It currently contains two deliberately separate paths: a usable, dependency-free CR2 adapter for legacy GNOME Sushi and a source-only standalone GTK prototype.

## Legacy CR2 data flow

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

## Standalone viewer prototype

The standalone app routes each request through a capability registry:

```text
selected URI
  └─ type and capability probe
       ├─ native renderer: text/source and XLSX
       ├─ disposable PDF worker
       ├─ constrained web renderer: HTML, when its sandbox is available
       ├─ planned native/sandboxed image and RAW renderer
       ├─ gated disposable media worker
       └─ universal fallback: metadata plus safe text/hex inspection
```

Renderers produce content for one persistent preview window. A renderer may resolve to `ready`, `fallback`, or `error`; it may not close the session. The session controller owns the current URI, cancels stale asynchronous work, and ignores late results by generation number.

The in-process GStreamer audio/video implementation remains available for isolated testing, but the default registry deliberately omits it. The newer worker path opens one parent-validated input descriptor, enters a bubblewrap network/PID namespace behind `prlimit`, pauses GStreamer long enough to obtain metadata and at most one bounded RGBA frame, and returns through two size-checked output descriptors. Raw pixels cross back instead of an encoded image, so the GTK process does not invoke another image decoder on worker-controlled output.

That worker is still not registered automatically. Per-process limits do not provide an aggregate memory/task budget if compromised decoder code forks, and Ubuntu's AppArmor user-namespace policy requires a packaged profile before bubblewrap works in an otherwise unconfined source session. Enabling the route requires either a tested no-fork policy or a delegated cgroup limit, plus an integration test proving bubblewrap's PID namespace is quiescent before output is accepted. Until then media files reach the universal fallback and navigation continues.

For Nautilus integration, Kukni can implement the user-session `org.gnome.NautilusPreviewer2` D-Bus contract. Arrow-key actions emit `SelectionEvent`; Nautilus remains responsible for choosing the adjacent item and responds with the next `ShowFile`. Kukni changes its current URI only on that call and keeps the window alive across unsupported files.

The `windowHandle` supplied by Nautilus is also the placement contract. On Wayland, Kukni should use the foreign parent handle so the compositor can center the preview over the originating Files window; applications must not attempt unsupported absolute positioning. The outer window chooses a stable viewport from the active monitor's work area and renderers fit inside it without resizing the top level.

HTML rendering requires JavaScript off, network requests blocked, and local resource access constrained. XLSX currently uses a bounded ZIP/XML parser and native table that ignores macro code, formula source, and external relationships. DOCX and PPTX conversion remain planned. Unknown formats always reach the universal fallback.

## Compatibility policy

The current JavaScript integration subclasses private methods from Sushi 46. It is tested on Ubuntu 24.04 only and should fail honestly on incompatible versions rather than claiming broad support. Sushi 51 introduced a different plugin API; that integration should live beside, not silently replace, the legacy adapter.
