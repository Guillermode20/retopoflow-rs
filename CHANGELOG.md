# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). `Cargo.toml`, `bl_info` and this file must agree (`scripts/check_version.py`, enforced in CI and on release tags).

## [Unreleased]

## [0.3.0] - 2026-11-05

### Added
- In-viewport HUD: tool palette with key badges, per-tool options with steppers and toggles, contextual hints, toasts, F1 shortcut card. Respects header/toolbar overlap and UI scale.
- Session overlay renderer for the target: tinted faces, selection colours, hover highlights, x-ray ghost pass for geometry under curved surfaces, live previews (PolyPen rubber band, loop rings, patch corners, strokes, brush circle).
- PolyPen: vertex and edge snapping under the cursor, Tri → Quad strip building, Ctrl-click to start a new chain.
- Patches: **F** fills any selected boundary loop with automatic corner detection (`fill_auto`).
- Strokes: closing a stroke near its start makes a loop; boundary bridges align to the stroke direction.
- Knife cuts only the faces its line crosses (finite knife) without cracks in neighbours.
- Live Tweak and Relax brushes with falloff, projected onto the source.
- Click/box selection, Select All/None/Invert/Linked, merge and delete inside the session.
- Scene remembers its target and last tool; `Object.rf_rs_is_target` keeps targets out of sources.
- CI (Rust on Linux/macOS/Windows, MSRV, ruff, Blender 4.2 LTS and 5.2 headless suites), tag-driven release builds, `scripts/check.sh`.

### Changed
- Minimum Blender is 4.2 LTS.
- Add-on split into `engine`, `target`, `session`, `hud`, `gfx` and `legacy` modules; session undo uses bmesh snapshots instead of mesh datablocks.
- Topology edits rebuild the mesh in place (no more `Mesh RS RS` datablocks).
- New faces face outward relative to the source surface.
- Contours and Knife lines may start off the surface; the cutting plane follows the drawn line in perspective.
- Errors appear as toasts; the session continues. Blender-initiated cancellation (e.g. loading a file) keeps work.
- Navigation keys (numpad, Home, NDOF) pass through; a drag released outside the viewport still commits.
- Release zips are named `retopoflow_rs-<os>-<arch>.zip`.

### Fixed
- HUD overlapping the viewport header and Blender's view text.
- Clicks on overlapping header/toolbar regions acting as tool clicks.
- Per-event cost proportional to mesh size (selection stamps, picking loops).
- Right-click when idle ending the session.

## [0.2.2]

Initial import: Rust engine with JSON C ABI, native runtime-copy loading, first viewport session, standalone operators.

[Unreleased]: https://github.com/Guillermode20/retopoflow-rs/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Guillermode20/retopoflow-rs/releases/tag/v0.3.0
