# Plan: taming build_shot.py (2.7k lines)

Status: **PARKED — do not start during production.** Written 2026-08-05 after the
cache-update debugging session (stale matrix restore + swap not adopting archive
transforms, commit `7a2ddfe`). Execute in a quiet window between deliveries.

## Why it grew

`build_shot.py` is the integration point of every pipeline concern — caches,
looks, animation, dressing, placement, light links all meet in the build loop,
and every bug fix lands compensation logic here. The complexity is partly
essential (it IS the integrator); the problem is four abstraction levels in one
file and a 575-line `execute()`.

## Call-graph facts (measured 2026-08-05)

63 top-level defs. Clusters are ALREADY clean — almost no cross-references:

| Cluster | ~Lines | Contents | External consumers |
|---|---|---|---|
| Animation round trip | 750 | `_snapshot_poses`, NLA/slot capture + rebind, `_apply_element_animation`, `_collect_element_animation`, `_element_anim_hashes`, diag | `operators.py`, `cache_shot.py` |
| Operators + build loop | 950 | `FLUMEN_OT_build_shot` (575 lines), `FLUMEN_OT_load_animation`, `FLUMEN_OT_reapply_cache_looks`, dialog plumbing | `operators.py` (CLASSES) |
| Cache import/swap/vis | 290 | `_import_alembic_cache`, `_swap_alembic_cache`, `_apply_cache_visibility`, `_clear_hide_keys` | — |
| Linking/holders/placement | 360 | holders, `_link_collection_override`, matrix snapshot/restore, camera rig | `dressing_ops.py`, `review_camera.py`, `cache_shot.py` |
| Dialog-row status | 280 | `_element_update_notes`, `_look_materials_broken`, version probing | — |
| Dressing apply | 85 | `_apply_dressing_props` | — |

Only two cross-cluster edges: `_element_loaded_file` (status → used by
placement) and `_action_fcurves` (anim → used by vis sidecar). Dead code:
`_stale_content_filter` (unused since `bc2e437`).

Exact per-consumer imports (recheck before moving; grep confirms):
- `cache_shot.py`: ELEMENT_HOLDER_PREFIX, _ELEMENT_LOADERS, _action_fcurves,
  _apply_build_frame_range, _apply_dressing_props, _apply_element_animation,
  _is_environment
- `operators.py`: ELEMENT_HOLDER_PREFIX, FLUMEN_AssemblyItem, FLUMEN_AnimItem,
  the 3 operator classes, _snapshot_poses, _collect_element_animation,
  _element_anim_hashes, _element_loaded_file, _project_rel
- `dressing_ops.py`: _fetch_publish_path, _link_collection_override,
  _named_holder, _project_rel
- `review_camera.py`: _named_holder

## Phase 1 — mechanical split (pure moves, no logic edits, one commit each)

1. `build_anim.py` (~750) — the whole animation round-trip cluster.
2. `build_cache.py` (~290) — alembic import/swap/sidecar. Smallest blame
   surface for the area where all recent bugs lived.
3. `build_status.py` (~280) — dialog-row/update-notes logic.
4. `build_shot.py` keeps holders/linking/placement/dressing/camera + operators
   (~1.3k). Delete `_stale_content_filter` along the way.

Rules: update the 4 consumers' imports in the same commit (no re-export shims);
run the full pytest suite + a headless `blender -b --factory-startup` import
smoke of the addon after each commit. Resolve the two cross-edges by importing
across the new modules (anim ← vis sidecar needs `_action_fcurves`; placement
needs `_element_loaded_file` from status — or move that one helper).

## Phase 2 — decompose `FLUMEN_OT_build_shot.execute()` (575 lines)

Extract behavior-preserving phase functions sharing a small build-state dict:
`_resolve_and_heal`, `_build_one_element` (swap/rebuild/load → place → dress →
look → anim), `_finalize_build` (light links, frame range, report). Riskier
than Phase 1 — do it separately, after Phase 1 has survived a few production
builds.

## Phase 3 — the safety net (what actually makes the file un-scary)

Promote the 2026-08-05 headless A/B debug scripts into a permanent
`scripts/diagnose_shot.py`: open a lighting file headless on the Windows box,
exercise swap / re-import / look / anim against the real caches, print `DBG|`
fact lines and render probe frames. One command, minutes, catches placement/
material/visibility regressions against real production data — the build loop
has no unit-test coverage and this is the practical substitute. (The session's
throwaway versions lived in `C:\Users\marco\flumen_dbg\` — `ab_gatto_update.py`,
`verify_skel3.py`, `diag_skel_xforms.py` — recover the pattern from git history
of this doc's session if deleted.)

Suggested order when the window opens: Phase 3 first (safety net), then 1, then 2.
