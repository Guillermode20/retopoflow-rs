# Contributing

## Workflow

- Branch from `main`; open a pull request. CI must pass (Rust on three OSes, MSRV, ruff, version sync, Blender 4.2 LTS and 5.2 suites).
- Run `./scripts/check.sh` before pushing. It runs everything CI does.
- Commits are small and self-contained, with an imperative subject prefixed by area: `engine:`, `addon:`, `ci:`, `build:`, `docs:`, `tests:`. Explain *why* in the body when it isn't obvious.
- User-visible changes get a line under `## [Unreleased]` in `CHANGELOG.md`.

## Tests

- Engine behaviour belongs in Rust unit tests (`src/*.rs`) or `tests/native_operations.rs`.
- Session behaviour belongs in `tests/blender_session.py`. Drive `Session.modal` with `Driver` events and project coordinates with `Driver.screen`, never hard-coded pixels.
- UI drawing changes: run `blender --factory-startup --python tests/blender_gui.py -- shots/` and look at the screenshots.

## Releasing

1. Move the `Unreleased` notes to a new `## [X.Y.Z] - YYYY-MM-DD` section.
2. Bump `version` in `Cargo.toml` and `bl_info['version']` in `blender/retopoflow_rs/__init__.py`; run `cargo build` to update `Cargo.lock`.
3. `python3 scripts/check_version.py`, commit as `release: vX.Y.Z`, then `git tag -a vX.Y.Z -m vX.Y.Z && git push --follow-tags`.
4. The Release workflow builds Linux, Windows and macOS (arm64 and x86_64) zips and publishes them with the changelog section as notes.
