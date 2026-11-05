# RetopoFlow 3 reference map

Upstream: https://github.com/CGCookie/retopoflow
Inspected commit: `ac2570c5292c1dd90190fd3641b4dbc42cf4bd63`.

These are reference entry points, not claims that the corresponding algorithms have been fully ported.

| Tool | Upstream implementation | Local implementation |
| --- | --- | --- |
| Loops | `retopoflow/rftool_loops/loops_insert.py`: `insert_edge_loop_strip`; `loops.py`: `prep_edit`, `slide` | `src/topology.rs`: `insert_loop` |
| Knife | `retopoflow/rftool_knife/knife_insert.py`: `_insert`, `_get_crosses` | `src/topology.rs`: `knife` (plane-only) |
| Patches | `retopoflow/rftool_patches/patches.py`: `_recompute`, `fill_patch` | `src/construction.rs`: `patch`; `src/fill.rs`: `fill_patch` |
| PolyStrips | `retopoflow/rftool_polystrips/polystrips_ops.py`: `new_brushstroke`; `polystrips_utils.py`: strip sampling/update | `src/construction.rs`: `polystrip` |
| Contours | `retopoflow/rftool_contours/contours_ops.py`: `new_cut`, `fill`, `change_count`; `contours_utils.py`: loop alignment | `src/surface.rs`: `sections`; `src/construction.rs`: `contour` |
| PolyPen | `retopoflow/rftool_polypen/polypen_insert.py`: `insert`, `_get_edge_quad_verts`, `_insert` | `src/pen.rs`: `insert`, `merge_selected` |

User-facing behavior reference: upstream `docs/{contours,knife,loops,patches,polypen,polystrips,relax,select,strokes,tweak}.md`, mirrored under its `help/` directory. In particular, Patches supports L/C/rectangular selected regions upstream; our boundary fill currently handles only the explicitly dimensioned rectangular case. Strokes upstream includes bridge/span inference beyond our explicit matched-boundary operation.

Session/UI lifecycle references and interaction contract: `docs/SESSION.md`. RF3 numeric tool keys are taken from upstream `config/keymaps.py`; the viewport session is an independently implemented shell rather than a copy of upstream artwork or HTML/CSS assets.

Upstream attribution and non-code asset restrictions: `REFERENCE_LICENSE.txt`. No upstream artwork/fonts/screenshots/icons/styles are bundled in this project.
