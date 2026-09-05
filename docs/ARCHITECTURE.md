# Architecture

Kukni is a standalone GTK4 file previewer with a persistent session, a
capability-based renderer registry, and narrow desktop-integration boundaries.
GNOME Files (Nautilus) is the first supported file manager, but it is not the
owner of Kukni's UI or rendering code.

## Current data flow

```text
Nautilus Space key                         `kukni FILE`
        │                                      │
        └─ org.gnome.NautilusPreviewer D-Bus ──┤
                                               ▼
                                      Kukni application
                                               │
                                      persistent PreviewWindow
                                               │
                                  local-file and capability probe
                                               │
                  ├─ Canon CR2 → disposable decode worker → raw RGBA
                  ├─ raster images → same bounded pixel contract → raw RGBA
                  ├─ native XLSX table
                  ├─ gated PDF worker
                  ├─ gated HTML renderer
                  ├─ bounded text/source view
                  └─ metadata-only unavailable state
```

Kukni owns the user-session previewer D-Bus name and implements both the legacy
and current Nautilus method shapes needed by the present compatibility target.
The normal installer creates a user-level activation file, so Nautilus can start
Kukni on demand. GNOME Sushi is not involved and is not a runtime dependency.

Nautilus remains responsible for folder selection. When an arrow key is pressed,
Kukni emits a `SelectionEvent`; Nautilus selects the adjacent item and answers
with another `ShowFile` call. Kukni updates its current URI only after that call,
keeping the same top-level window alive through rich previews, fallbacks, and
errors.

## Session model

Every request gets a monotonically increasing generation. New navigation
cancels work for the old generation, and late results are ignored. A renderer
can resolve to a rich preview, a fallback, or an error, but it does not decide
when the session closes. Space, Escape, or the window close control ends the
session.

The outer window uses a stable viewport instead of resizing to each file. On
Wayland, Kukni accepts Nautilus's foreign parent handle so the compositor can
place the preview relative to the Files window without unsupported absolute
positioning.

Kukni accepts native local files only. Remote URIs receive an in-window
explanation and are not fetched.

## Renderer contract

The registry probes renderers in deterministic order. A renderer receives one
`Gio.File`, its already-queried metadata, a cancellation object, and success and
error callbacks. If no rich renderer accepts the file—or a renderer cannot run
within its required boundary—the fallback shows a plain explanation, file type,
and size without reading the file. Technical details are disclosed on demand.

Current automatic routes are:

1. **Canon CR2** — the parent opens one bounded regular file and passes its
   read-only descriptor to a disposable worker. That worker finds the
   camera-generated embedded JPEG, decodes it with GdkPixbuf, applies embedded
   orientation, downsizes it, and emits only tightly packed raw RGBA plus small
   metadata. The parent rejects source mutation and validates every field and
   byte count before creating a `Gdk.MemoryTexture`; it never decodes
   worker-selected encoded image content.
2. **XLSX** — a bounded ZIP/XML parser produces an inert model for a native GTK
   table. It reads only the first visible worksheet, shows cached formula values,
   and ignores macros, active content, and external relationships.
3. **PDF** — Poppler renders page one in a short-lived bubblewrap namespace
   behind `prlimit`. Input and output sizes, CPU, address space, descriptors,
   dimensions, and wall time are bounded. If the sandbox probe fails, PDF uses
   the fallback.
4. **HTML** — WebKitGTK 6 may render a bounded local document only when its
   process sandbox is usable. JavaScript, networking, forms, media, broad local
   access, and other active features remain disabled. If that boundary is not
   available, HTML uses the fallback.
5. **Text/source** — a worker thread reads at most 1 MiB from a verified regular
   file. Decoding is strict, deceptive controls are exposed, and executable or
   launcher-like files are displayed rather than run.

CR2 work is serialized globally until GTK has consumed or discarded the
payload. Defaults allow a 128 MiB CR2, a 64 MiB embedded JPEG, source dimensions
up to 32,768 pixels per edge and 100 megapixels, and a retained RGBA frame up to
4,096 pixels per edge, 16.8 megapixels, and 64 MiB. The worker also receives an
eight-second wall deadline, 768 MiB address-space ceiling, six CPU seconds,
64-descriptor ceiling, 64 MiB file-size ceiling, hard `NPROC=0`, and verified
`PR_SET_NO_NEW_PRIVS` before untrusted CR2 bytes are read.

This is a killable, tightly checked process boundary, not a complete sandbox.
The source install cannot create the mount and network namespaces needed here
on the current Ubuntu/AppArmor target. A compromised CR2 decoder would still
have the worker's ordinary same-user filesystem, network, IPC, and signalling
access during its short lifetime. Fixed inherited descriptors describe the
intended protocol; they do not enforce filesystem or network isolation.

The fallback is part of the product contract, not an error page. It makes
navigation continuous even before a dedicated renderer exists for a format.

## Trust boundaries

- Selected paths, metadata, container structures, and every file byte are
  untrusted input.
- Kukni never invokes a selected file as a command and never hands it to a shell
  or default application.
- All parsing and rendering paths have explicit input, work, output, or time
  limits appropriate to the format.
- A sandbox-gated renderer falls back when its boundary is unavailable; it must
  not disable or weaken the sandbox to improve compatibility.
- Renderer failures and stale work remain inside the current preview session so
  navigation can continue.

Ubuntu's AppArmor policy can deny unprivileged user namespaces to an unconfined
source installation. That means bubblewrap-backed PDF and HTML rendering may
correctly remain unavailable even when their packages are installed. A future
`.deb` can ship a narrowly scoped AppArmor profile; the source installer does
not alter system security policy.

## Experimental media work that is not routed

An isolated media worker and parent supervisor can produce one bounded video
frame or audio metadata through a size-checked protocol. It is deliberately
absent from the default registry: per-process limits do not yet provide the
aggregate process-tree and task containment required for automatic decoding of
untrusted media. Enabling it requires a tested cgroup or equivalent no-fork
boundary plus packaged sandbox integration tests.

PNG, JPEG, WebP, GIF, TIFF, BMP, and ICO have an automatic raster route. It
reuses the existing CR2 supervisor, descriptor contract, decoder validation,
and global admission slot rather than introducing another worker framework.
SVG and other unrouted images remain metadata-only, never source-code views.
No GNOME Sushi plugin integration ships with Kukni.

## Extension boundaries

New work should preserve four layers:

1. **Desktop adapters** translate a narrow host contract into preview and
   navigation requests.
2. **Session core** owns URI state, cancellation, generation checks, timeouts,
   and close behavior.
3. **Renderer registry** selects an available capability without changing
   session policy.
4. **Parsers and workers** turn one validated local file into a bounded, inert
   model or pixel payload.

Format parsing does not belong in the Nautilus adapter. A new renderer must fail
honestly when its dependencies or containment are unavailable, and malformed
input tests must accompany happy-path coverage.

## Compatibility target

The currently validated desktop combination is Ubuntu 24.04 with Nautilus 46.
The previewer contract is private desktop integration rather than a freedesktop
standard, so support for newer Nautilus releases must be verified explicitly.
Other file managers are future adapters, not implied compatibility.
