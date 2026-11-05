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

`tests/blender_session.py` constructs the real `Session` and delivers synthetic
events through `Session.modal`, with every coordinate projected through a real
viewport. Only the window manager's live event registration is bypassed.
`tests/blender_gui.py` runs a live session in a window and captures screenshots
for visual review (not run in CI, since it needs a display).

- [x] Enter from Edit Mode and return to Edit Mode with the target intact.
- [x] Enter from a lone source: create a target, then continue it next time.
- [x] Switch tools by key and by palette click without resetting selection.
- [x] Undo and redo individual actions (keyboard and buttons).
- [x] Cancel a gesture or close help with Esc without leaving the session.
- [x] Finish, retaining geometry and restoring viewport settings and visibility.
- [x] Discard the whole session, restoring entry geometry.
- [x] Blender-initiated cancel keeps work.
- [x] Restore UI, cache and mesh resources after an operation error or a failed start.
- [x] Navigation events pass through; Ctrl-S is blocked.
- [x] Picking, box selection, PolyPen strips and snapping, strokes and bridges, contours, knife, patches, fill, loops, Tweak and Relax brushes, HUD steppers and wheel.
- [x] HUD placement and GPU overlay drawing in a graphical viewport (screenshot check on Blender 4.2 LTS and 5.2).

## Module responsibilities

- `session.py`: event routing (HUD → gestures → shortcuts → navigation), the tools, presentation changes paired with restore callbacks, bmesh-snapshot history, and the overlay renderer.
- `target.py`: one numpy snapshot of the target per change; screen projection and occlusion are cached per view.
- `hud.py`: `layout(View) → Frame` is pure, and the same frame is used for drawing and hit testing.

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
