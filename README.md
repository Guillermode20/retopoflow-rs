# Retopoflow RS

[![CI](https://github.com/Guillermode20/retopoflow-rs/actions/workflows/ci.yml/badge.svg)](https://github.com/Guillermode20/retopoflow-rs/actions/workflows/ci.yml)

Surface retopology for Blender: a native Rust geometry engine driven by a single in-viewport session, using [RetopoFlow 3](https://github.com/CGCookie/retopoflow) as a behaviour reference. It is **not** a 1:1 RetopoFlow port; the differences are listed below. No upstream artwork, branding or UI assets are bundled.

Supports Blender **4.2 LTS and newer** (CI runs 4.2 LTS and 5.2).

## Install

Download the zip for your platform from [Releases](https://github.com/Guillermode20/retopoflow-rs/releases) (or build it, below) and install it through *Preferences → Add-ons → Install from Disk*. Native code is per OS and architecture. **Fully restart Blender after updating.** The native library loads from a private runtime copy, so reinstalling never overwrites code that is executing (see `docs/NATIVE_LOADING.md`).

## Use

1. Select your high-poly mesh in Object Mode and press **Retopo RS** in the 3D View header. A target mesh is created, or the scene's previous target is continued. To keep working on an existing retopology mesh, make it active instead. **+** always starts a new target.
2. Retopologize with the tools in the palette (top left). Options for the current tool are top right, and a hint at the bottom says what a click will do. **F1** shows every shortcut.
3. **Tab** (or Esc, or *Finish*) ends the session and keeps the work. **Shift-Esc** (or *Discard* twice) throws the session away.

The **Source** field in the sidebar picks the surface explicitly; when it's empty, every visible mesh except retopology targets is used. Source modifiers are evaluated.

| Key | Tool | What it does |
| --- | --- | --- |
| 1 | Contours | Drag a line across a form to add a ring. With a boundary selected it bridges to it. |
| 2 | PolyStrips | Drag along the surface to paint a strip of quads; width is in screen pixels. |
| 3 | Strokes | Draw an edge chain (ending near the start closes it). Drawing beside a selected boundary bridges it with quads, in either direction. |
| 4 | Patches | Click four corners for a grid. **F** fills any selected boundary loop with automatic corner detection. |
| 5 | PolyPen | Click to place vertices; hovering snaps to vertices and edges. With an edge selected, a click makes a triangle, and the next click completes it to a quad (Tri → Quad), so strips build quickly. Ctrl-click starts a new chain. |
| 6 | Knife | Drag a line across faces to cut only the faces it crosses. |
| 7 | Loops | Hover an edge to preview the loop, then click to insert it where you point (or at the centre). |
| 8 | Tweak | Brush-drag vertices along the surface with falloff. |
| 9 | Relax | Brush to even out spacing; boundaries stay fixed. |
| 0 / W | Select | Click a vertex or edge, drag a box. Shift adds/toggles, Ctrl removes. |

In every tool: **Shift-click** selects, **A / Alt-A** select all/none, **Ctrl-I** inverts, **Ctrl-L** selects linked, **M** merges, **X** deletes, **Ctrl-wheel** adjusts the tool's main setting, and **Ctrl-Z / Ctrl-Shift-Z** undo/redo each action. Navigation (middle mouse, wheel, numpad, Alt-drag) works as usual. Errors appear as toasts and never end the session.

The target is drawn by the session's own overlay. Faces are tinted and selection is orange, with a faint x-ray pass so geometry that dips under a curved surface stays readable. Blender's header, toolbar, sidebar, gizmos and overlay text are hidden during the session and restored afterwards.

### Data safety

Coordinates cross the native boundary in world space. Selection and coordinate-only edits (tweak, relax) preserve all mesh data. Topology edits refuse meshes with UVs, color attributes, custom attributes or normals, or vertex groups, rather than silently dropping them. Default flat shading is fine. Shape-key meshes are rejected for edits. Every action is transactional: a rejected operation leaves the mesh untouched. Save outside the session (Ctrl-S is blocked inside it). Keep backups of production assets.

## Build and test

Requires Rust 1.88+, Python 3 and Blender.

```sh
./scripts/check.sh            # fmt, clippy, cargo test, ruff, version sync, build, all Blender suites
./scripts/check.sh --no-blender
python3 scripts/build_addon.py   # → dist/retopoflow_rs-<os>-<arch>.zip
python3 scripts/run_blender_tests.py --blender /path/to/blender
blender --factory-startup --python tests/blender_gui.py -- shots/   # screenshots of a live session
```

Rust tests cover construction, sections, knife intersections, quad rings, PolyPen inference, fills, selection, malformed input and ABI ownership. The Blender suites drive the real session through `Session.modal` with synthetic events, projected through a real viewport: picking, box select, PolyPen strips, strokes/bridges, contours, knife, patches, fill, loops, brushes, HUD clicks, undo/redo, lifecycle and restoration. GPU drawing needs a display, so `tests/blender_gui.py` captures screenshots instead.

## Architecture

```
src/                     Rust engine (cdylib): JSON C ABI, one operation per call
blender/retopoflow_rs/
  engine.py              ctypes bridge, numpy mesh I/O, in-place mesh rebuilds
  target.py              cached target topology, projection, occlusion-aware picking
  session.py             the modal session (plain Session class + thin operator)
  hud.py, gfx.py         HUD layout (pure, shared by drawing and hit testing) and GPU helpers
  legacy.py              standalone one-shot operators for scripting
```

`rf_execute(const char *json)` returns allocated NUL-terminated JSON; free it once with `rf_free`. Requests carry `mesh` (`vertices`, `edges`, `faces`, `selected`), triangulated `source` and `operation`. `rf_source_create` / `rf_execute_cached` / `rf_source_free` cache a source BVH for a session. See `src/lib.rs` and `tests/native_operations.rs`.

## Parity with RetopoFlow 3

Reference: CGCookie/retopoflow at `ac2570c5292c1dd90190fd3641b4dbc42cf4bd63` (see `docs/REFERENCE.md`).

| Tool | Here | Missing |
| --- | --- | --- |
| Select | Click/box, linked, invert, path (standalone) | Paint selection, face/edge smart select |
| PolyPen | Vertex/edge snapping, Tri → Quad strips, edge split | Edge-slide insertion, RF's full insertion state machine |
| Strokes | Chains, auto-close, aligned boundary bridges | Span/extrude modes, endpoint joins |
| Contours | Plane sections, bridges to selected boundary | Interactive ring count/rotation editing |
| PolyStrips | Ribbons along a stroke | Bézier handles, strip editing, crossings |
| Patches | Four-corner grids, auto-corner loop fill | L/C-shaped fills |
| Loops | Ring/strip insertion with live preview | Interactive slide after insertion |
| Knife | Finite cut across crossed faces | Multi-segment freeform knife |
| Tweak / Relax | Falloff brushes projected to the surface | RF's solver details, merge-on-drop |

Symmetry, per-tool preferences and topology-aware data transfer are not implemented.

## License

GPL-3.0-or-later (`LICENSE`). Upstream code licensing and provenance are recorded in `REFERENCE_LICENSE.txt`. Upstream non-code assets are separately restricted and are not copied. This independently named project is not endorsed by the upstream authors.
