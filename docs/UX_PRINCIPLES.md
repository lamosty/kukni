# UX principles

Kukni is a temporary, continuous lens over the current file selection—not a
sequence of miniature applications. The content matters more than its chrome.

## Implemented controls

- Nautilus opens the selected file with Space. Space, Escape, or Close ends it.
- Left, Right, Up, and Down ask the originating Nautilus session to move its
  selection. Navigation controls are hidden and disabled without a live owner;
  standalone opening does not invent a folder order or imply working arrows.
- `F` or `F11` toggles fullscreen. `Ctrl+O` opens a direct-launch file chooser.
- The header names the file once. **Info** (`Ctrl+I`) reveals type, file size,
  retained image dimensions, and preview details. It does not parse EXIF/GPS.
- Images and rendered PDF pages have native **Fit**, **−**, **+**, and **1:1**
  controls. Keyboard equivalents are `0`, `-`, `+` (or `=`), and `1`.
- Ctrl+wheel zooms. At enlarged scales, drag the canvas or use its scrollbars
  to pan. Ordinary wheel scrolling remains native scrolling.
- PDF page buttons and Page Up/Page Down move inside the document; arrow keys
  remain reserved for file-manager navigation.

**1:1 refers to retained preview pixels, not guaranteed original-file detail.**
One retained pixel maps to one logical display unit; HiDPI physical pixels may
be denser. If safety limits downscaled a photograph, the tooltip says so and
Info shows source and preview dimensions. A PDF page is a bounded raster, not
an unlimited vector zoom. Zoom does not re-decode or allocate a larger texture.
Fit preserves aspect ratio and does not upscale small images by default.

## Session continuity

- Unsupported, malformed, slow, or failed files do not close the window.
- Filename and metadata belong to the current selection, including during a
  loading state. A previous image never masquerades as the newly selected file.
- Late work and pending automatic resizes are cancelled/ignored; they cannot
  replace newer content. Direct empty activation also invalidates pending work.
- Loading, fallback, and error states retain an available file-manager session.
- Changing content reuses the same toplevel and originating parent handle.
  Switching to a direct open or losing the file manager clears that association.

The required regression sequence is:

```text
supported file A
  → arrow to unsupported file B
  → compact fallback, same window
  → arrow to supported file C
  → C appears, same window
```

Focus or selection must not snap back to A; Space is not required again.

## Adaptive, not restless

- Landscape, portrait, square, and panoramic images suggest appropriately
  proportioned windows. Text/documents get a readable page, audio a compact
  player, and unavailable states a small, calm explanation. A family preset is
  only applied when that renderer actually returns content; an unavailable
  renderer still gets the fallback size.
- Sizes are bounded by the current monitor's **logical** geometry, with desktop
  margins and absolute upper caps. Monitor scale is not multiplied twice.
- A completed current preview suggests size after 140 ms of coalescing. Loading
  never resizes the outer window. Differences below the larger of a 64-unit
  floor or 12% threshold are ignored; similar photographs do not bounce.
- An unsolicited resize, tiling, maximization, or fullscreen allocation gives
  control to the user for the rest of that window's lifetime. Later files fit
  within that chosen window. Zoom, pan, and PDF page changes never resize it.
- GTK does not identify who changed a Wayland allocation. A short 700 ms
  compositor-settling interval after our resize avoids mistaking our own request
  for manual resizing; a user resize inside that interval is indistinguishable.
- Keep the same toplevel and make a best-effort native parent association. The
  compositor controls placement and may adjust position during a resize. Kukni
  does **not** promise exact coordinates, active-monitor workareas, or pixel-
  identical macOS placement on Wayland.
- System light/dark styling and GTK motion preferences remain in effect; no
  custom outer-window resize animation or forced recentering is introduced.

## Universal usefulness

Every accessible local regular file gets a meaningful state:

- a rich preview when its safe renderer and dependencies are available;
- a calm unavailable state with type and size, without reading file bytes;
- an in-window explanation when access or policy prevents reading it.

“Unsupported” describes renderer capability, not the end of a session.
Technical failure details are on demand, not the main content. Binary
inspection is not a substitute for an image or page. Remote locations are not
fetched until a narrow portal-based access model exists.

## Keyboard and accessibility

Primary actions expose accessible names and explanatory tooltips. Content
cannot steal file-navigation arrow keys. Image controls remain keyboard
reachable without requiring pointer focus, and unavailable actions are disabled.
A newly chosen file resets to Fit rather than inheriting a surprising zoom.

## Safe previews

- Never execute document scripts, launchers, formulas, or macros by default.
- HTML cannot make network requests or gain broad local-file access.
- Metadata panels do not reveal GPS by default.
- Converters and extractors have input, output, time, work, and cancellation
  limits. Native decoding remains outside the UI process.
- A missing sandbox produces fallback, never a request to disable security.

Success means staying fast enough for browsing, continuous across failures, and
bounded enough for an untrusted file without optimistic shortcuts.
