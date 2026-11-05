# Rust retopology for Blender

A native Rust geometry engine and a thin Python/ctypes Blender add-on, using RetopoFlow 3 as a behavior reference. **Not yet a 1:1 RetopoFlow port.** The tools below perform real geometry operations; their remaining differences are explicitly listed. No upstream artwork, branding assets, or UI assets are bundled.

## Build and install

Requires Rust (edition 2024), Python 3, and Blender. The add-on declares Blender 3.6+ support; automated integration has currently only been run on the locally installed Blender 5.2.2.

```sh
cargo test
cargo clippy --all-targets -- -D warnings
python3 scripts/build_addon.py
python3 tests/test_native_reload.py
```

Install `dist/retopoflow_rs-linux.zip` (or the ZIP built on your platform) through Blender Preferences → Add-ons → Install from Disk. **Fully quit and reopen Blender after updating**, particularly from versions before 0.2.2. Native code now loads from a private runtime copy so ZIP reinstall cannot overwrite executing library pages (see `docs/NATIVE_LOADING.md`). Binaries are OS/architecture-specific; build on each target platform.

## Use

Use **Retopo RS** in the **3D View header** to enter one persistent viewport session with the active target, or the adjacent **+** to create a new target. The sidebar provides the same session entry; old standalone commands are hidden under **Legacy standalone commands**.

When the active object is explicitly chosen in **Source**, or is the only polygon mesh visible in Object Mode, startup creates a separate target automatically. An explicitly chosen source is included even if hidden. Zero-area evaluated source triangles are skipped in the temporary geometry cache without changing your source mesh; an entirely unusable source reports its name and the reason.

The session has an in-viewport tool palette and contextual options, shares selection across tools, and keeps native source geometry cached for the session. Numeric tool switching follows RF3: **1 Contours, 2 PolyStrips, 3 Strokes, 4 Patches, 5 PolyPen, 6 Knife, 7 Loops, 8 Tweak, 9 Relax, 0 Select**. Do not launch a new operator for each stroke.

**Ctrl-Z / Ctrl-Shift-Z** undo/redo committed actions inside the session. **Esc/right-click during drawing** discards that gesture and keeps the session open. **Tab or idle Esc** finishes the session; **Shift-Esc** cancels the whole session. Presentation settings are restored on exit. Save outside the session. See `docs/SESSION.md` for the integration contract and validation status.

- **Project / Relax / Tweak:** nearest-surface projection, boundary-preserving Laplacian smoothing, and drag-translation projected onto the source.
- **Stroke:** drag on the source to draw a resampled edge chain. Closed Stroke creates a loop.
- **Boundary Bridge:** select one unbranched boundary; draw a matching stroke with the same vertex count and closure. Produces quads.
- **Ribbon Strip:** drag a centerline; Width controls the full distance between projected rails.
- **Four-click Patch:** click four perimeter corners in winding order; Count and Rows control the quad-cell dimensions.
- **Fill Patch:** select a rectangular closed boundary with exactly `2*(Count + Rows)` edges. Reuses its vertices and fills it with a projected Coons grid. The first ordered boundary vertex is the first corner; opposite sides must match the configured dimensions.
- **Contour:** drag a line to define a view-perpendicular plane; creates the nearest source section. Contour Boundary Bridge joins it to a selected matching boundary, aligning start and direction automatically.
- **Insert Loop:** click within 24 pixels of a target edge. Traverses its quad ring/strip and inserts a loop at Loop Factor. Rejects non-quad and twisted paths.
- **Plane Cut:** drag a line defining an infinite cutting plane. Splits intersected target faces with shared intersections. This is a plane cut, not arbitrary finite multi-segment RetopoFlow Knife.
- **PolyPen:** project/snap a click; chain from a selected vertex, extrude a triangle from a selected boundary edge, or split a nearby target edge. Inside the integrated session, each insertion is independently undoable; tool switching does not end drawing mode. It does not implement full automatic triangle/quad inference.
- **Merge:** weld selected vertices at their projected center, remapping topology and rejecting invalid vertex fans.
- **Selection:** all/none/invert/connected and shortest topological path between two selected endpoints.

Draw previews show the input stroke/plane/corners, not a complete live reconstruction of the resulting mesh. Per-action session history handles undo/redo internally; the legacy standalone commands still use Blender operator undo.

### Data safety and limits

Coordinates cross the ABI in world space and return to target-local space. Selection and coordinate-only operations preserve mesh attributes; shape-key coordinate edits are rejected. Topology-changing operations reject UV/color/custom-attribute/vertex-group/custom-normal data rather than silently deleting it. Default flat shading and unmarked sharp-edge data are accepted; nondefault shading attributes remain protected. Multi-material and other complex data transfer is not a completed feature. Keep a backup of production assets.

Stroke sample counts are 2–4096 (closed minimum 3), patch dimensions are bounded, and malformed/nonfinite inputs are rejected. Geometry requests are transactional. Modal drawing caches an immutable native source BVH for the session and releases it on finish/cancel. Standalone button operations construct their source per request; source changes are not incorporated into an already-running drawing session. Source evaluation uses modifiers; target topology operates on the base mesh.

## Tests

```sh
blender --background --factory-startup --python-exit-code 1 --python tests/blender_smoke.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender_tools.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender_modal.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender_session.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender_sources.py
```

Rust tests cover construction, sections, shared knife intersections, open/closed quad rings, pen snapping, weld rollback, boundary filling, selection, malformed options and ABI ownership. Blender tests cover world/local transforms, tool dispatch, mesh reconstruction and attribute-preservation/rejection. Deterministic modal tests exercise repeated pen commits and cancelled partial patches. Session tests exercise Edit Mode entry/exit, multiple sources, per-action undo/redo, gesture cancellation, whole-session cancellation, native cache release, new-target finish and failed-start restoration. Headless tests do not verify actual mouse gestures, rendered previews, or Blender's live user undo workflows.

## Native API

`rf_execute(const char *json)` returns allocated NUL-terminated JSON; free it exactly once with `rf_free(pointer)`. Use ctypes `c_void_p`, not a `c_char_p` return type, to preserve allocation ownership.

Requests contain `mesh` (`vertices`, `edges`, `faces`, `selected`), triangulated `source`, and `operation`; results contain `ok` and either `mesh` or `error`. For repeated operations, `rf_source_create(Source JSON)` returns a process-local `uint64_t` handle (0 on error); `rf_execute_cached(request JSON, handle)` permits omitting `source`; release the handle with `rf_source_free(handle)`. Unknown or released handles return errors, not dangling-pointer access. Options: `points`, `count`, `closed`, `rows`, `width`, `strength`, `iterations`, `origin`, `normal`, `edge`, `factor`. See `src/lib.rs` and `tests/native_operations.rs` for examples.

## Upstream reference and parity

Reference: https://github.com/CGCookie/retopoflow at commit `ac2570c5292c1dd90190fd3641b4dbc42cf4bd63`. Relevant upstream entry points are recorded in `docs/REFERENCE.md`.

| Tool | Implemented here | Remaining differences |
| --- | --- | --- |
| Select | Viewport vertex picking, selection sets, connected/path | RF edge/face smart selection and paint selection |
| Relax | Projected smoothing with fixed boundaries | Interactive brush falloff and RF solver behavior |
| Tweak | Projected selected-vertex drag | RF brush behavior and snapping/merge heuristics |
| Strokes | Edge chains and matching boundary extrusion | Span modes, endpoint joins, automatic face inference |
| PolyPen | Snapping, chain/triangle insertion, edge split | Automatic quads and RF insertion state machine |
| Contours | Open/closed plane sections and aligned bridges | Interactive count edits, loop manipulation, multi-source behavior |
| PolyStrips | Surface-normal ribbons | Bézier handles, crossing detection, strip editing |
| Patches | Four-corner grids and rectangular boundary Coons fill | L/C-shaped regions, automatic corner detection |
| Loops | Open/closed quad-ring insertion with factor | Interactive slide and RF preview/state machine |
| Knife | Shared-vertex infinite-plane cutting | Finite/freeform multi-segment screen-space knife |

The session shell recreates the persistent viewport interaction model, but does not reproduce RF's original HTML-backed UI, custom target renderer, every keymap/preference, symmetry system or full topology-aware data transfer. Visual and tool-workflow parity remain incomplete. No tool is claimed to have complete upstream parity.

## License

GPL-3.0-or-later (`LICENSE`). Upstream code licensing/provenance is in `REFERENCE_LICENSE.txt`; upstream non-code assets are separately restricted and are not copied. This independently named project is not endorsed by the upstream authors.
