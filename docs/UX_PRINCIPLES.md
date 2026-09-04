# UX principles

Kukni should feel like a temporary, continuous lens over the current file selection—not like opening and closing a sequence of miniature applications.

## Session continuity

- Space opens preview mode; Space, Escape, or the close control ends it.
- Unsupported, malformed, slow, or failed files never close the preview window.
- Left, Right, Up, and Down request navigation from the file manager while the window remains open.
- The displayed filename, metadata, content, and current URI change together only after the new selection is confirmed.
- Late work from the previous file is canceled and ignored; it cannot replace newer content.

The required regression sequence is:

```text
supported file A
  → arrow to unsupported file B
  → B appears as a fallback without closing
  → arrow to supported file C
  → C appears in the same window
```

At no point may focus or selection snap back to A, and the user must not need to press Space again.

## Universal usefulness

Every regular file gets a meaningful state:

- a rich preview when a safe renderer or converter is available;
- a universal fallback with name, type, size, timestamps, and bounded text or hex content;
- an in-window explanation when access or safety policy prevents reading it.

“Unsupported” is a capability description, not a terminal error.

## Keyboard first

- Arrow keys navigate the surrounding selection.
- `+` and `-` zoom; `0` fits; `1` shows 100%; `F` toggles fullscreen; `I` toggles information.
- Controls remain discoverable to pointer users and expose accessible names.
- Focus stays predictable; opening a toolbar or information panel does not steal navigation permanently.

## Stable presentation

- The preview opens against the active monitor and requests compositor placement centered over the originating file-manager window.
- The default viewport uses a consistent portion of the monitor's usable area instead of adopting the current file's natural dimensions.
- The outer window keeps that stable size while content changes.
- Loading uses the previous frame or a restrained placeholder rather than flashing or resizing.
- Content uses a neutral canvas, preserves aspect ratio, and never upscales by default.
- Fit mode is the default. A 100% view may overflow into panning, but it never changes the outer window size.
- Errors are concise, actionable, and visually subordinate to navigation.
- Reduced-motion and system light/dark preferences are respected.

## Safe previews

- Rendering must not execute document scripts or macros by default.
- HTML must not make network requests or gain broad local-file access.
- Metadata panels must not reveal GPS by default.
- Converters and extractors are bounded by input size, output size, time, and cancellation.
