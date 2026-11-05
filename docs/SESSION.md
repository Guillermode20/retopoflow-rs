# Viewport session integration contract

The integration must be a single retopology session, not a sequence of independent sidebar operators. Upstream RetopoFlow 3 achieves its workspace-like behavior by temporarily taking over an existing 3D View; it does not duplicate the workspace during normal startup.

## Reference behavior

- Entry from the 3D View header, creating a target or continuing an existing one.
- Fixed target and source roles throughout the session. Visible mesh sources exclude the target; evaluated source geometry includes modifiers. An explicitly chosen Source is included even if hidden. Choosing the active object as Source creates a separate target rather than excluding the only source. Cache preparation skips zero-area triangles without editing source mesh data.
- One shared selection and target representation across tool switches.
- In-viewport tool palette, contextual options, counts and status/help.
- UI input has priority over tools; navigation is passed to Blender only when not competing with an active gesture.
- Gesture confirmation/cancellation is distinct from session termination.
- Undo/redo applies to individual committed actions, not the entire series of PolyPen clicks.
- Exit and partial-start failure restore presentation settings and release draw handlers/native handles.

## RF3 numeric tool mapping

| Key | Tool |
| --- | --- |
| 1 | Contours |
| 2 | PolyStrips |
| 3 | Strokes |
| 4 | Patches |
| 5 | PolyPen |
| 6 | Knife |
| 7 | Loops |
| 8 | Tweak |
| 9 | Relax |
| 0 / W | Select |

Upstream: `config/keymaps.py`. Matching these keys does not imply the underlying tools have complete RF3 behavior.

## Validation checklist

Checked items below are covered by `tests/blender_session.py` using real Blender data and the actual session methods with deterministic event delivery. Only RNA operator construction / live window-manager event registration is bypassed. They are not a graphical mouse test.

- [x] Enter from Edit Mode and return to Edit Mode with the target intact.
- [x] Enter with a new target and evaluated source meshes.
- [x] Switch tools without exiting the operator or resetting selection.
- [x] Perform two edits; undo and redo each independently.
- [x] Cancel a partial drawing without leaving the session.
- [x] Finish session, retaining geometry and restoring viewport settings.
- [x] Cancel the entire session, restoring entry geometry and viewport settings.
- [x] Restore UI/cache/mesh resources after an operation error or partial startup.
- [ ] Confirm actual palette hit testing and GPU overlay drawing in a graphical viewport.

Headless state/history tests can validate lifecycle logic, but cannot establish the final checkbox or claim visual parity.

## Upstream architecture references

`retopoflow/retopoflow.py`: startup/loading/end lifecycle.
`retopoflow/blenderregister.py`: header entry and target creation.
`retopoflow/rf/rf_ui.py`: viewport UI and Blender settings restoration.
`retopoflow/rf/rf_tools.py`: persistent shared tool instances.
`retopoflow/rf/rf_fsm.py`: event ordering, tool switching and global commands.
`retopoflow/rf/rf_undo.py`: per-action snapshots and tool restoration.
`retopoflow/rf/rf_sources.py`: multiple source wrappers/visibility.
`retopoflow/rf/rf_target.py`: shared topology, selection and custom rendering.

The Rust engine remains a geometry service. A native algorithm with a familiar name is not a replacement for the upstream workflow around it.
