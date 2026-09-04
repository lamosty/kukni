# UX principles

Kukni should feel like a temporary, continuous lens over the current file
selection—not a sequence of miniature applications.

## Shipped alpha behavior

The current standalone app provides these controls:

- Nautilus opens the selected file with Space.
- Space, Escape, or the window close control ends the preview.
- Left, Right, Up, and Down ask Nautilus to move the selection while the preview
  window remains open.
- `F` or `F11` toggles fullscreen.
- `Ctrl+O` opens a file chooser for direct-launch use.

Arrow-key folder navigation depends on a Nautilus-owned preview session. In a
direct `kukni FILE` launch, those keys explain that file-manager navigation is
not connected.

Zoom, fit, 100% view, panning, and an information-panel toggle are roadmap
features; they are not implemented controls today.

## Session continuity

These are non-negotiable invariants:

- Unsupported, malformed, slow, or failed files do not close the preview window.
- The displayed filename, metadata, content, and current URI change together
  only after the new selection is confirmed.
- Late work from the previous file is canceled and ignored; it cannot replace
  newer content.
- A loading, fallback, or error state leaves navigation available.

The required regression sequence is:

```text
supported file A
  → arrow to unsupported file B
  → B appears as a fallback without closing
  → arrow to supported file C
  → C appears in the same window
```

At no point may focus or selection snap back to A, and the user must not need to
press Space again.

## Universal usefulness

Every accessible local regular file gets a meaningful state:

- a rich preview when a safe renderer and its dependencies are available;
- a universal fallback with name, type, size, timestamp, and bounded text or hex
  content;
- an in-window explanation when access or policy prevents reading it.

“Unsupported” describes current renderer capability; it does not end the
session. Remote locations are not fetched until a narrow portal-based access
model exists.

## Keyboard first

- Primary actions stay reachable without moving to the pointer.
- Controls remain discoverable to pointer users and expose accessible names.
- Content views should not steal arrow-key navigation from the session.
- New shortcuts must avoid ordinary typing interactions inside any future
  focusable content.

Planned image controls are `+` and `-` for zoom, `0` for fit, and `1` for 100%.
They become part of the documented user contract only after implementation and
accessibility testing.

## Stable presentation

- The preview asks the compositor to associate it with the originating
  file-manager window using the supplied parent handle.
- The default viewport is stable instead of adopting every file's natural
  dimensions.
- Content changes use a restrained loading state rather than resizing the outer
  window.
- Rich content should preserve its natural proportions and should not upscale by
  default.
- Future 100% views may pan internally, but must not resize the outer window.
- Errors stay concise and visually subordinate to the ability to continue.
- System light/dark and reduced-motion preferences should be respected by every
  custom presentation.

## Safe previews

- Rendering must not execute document scripts, launchers, formulas, or macros by
  default.
- HTML must not make network requests or gain broad local-file access.
- Metadata panels must not reveal GPS by default.
- Converters and extractors need explicit input, output, time, work, and
  cancellation limits.
- A missing sandbox produces a fallback, never a request to disable security.

A preview is successful only if it is fast enough to stay in the browsing flow,
stable enough to navigate continuously, and bounded enough to handle an
untrusted file without optimistic shortcuts.
