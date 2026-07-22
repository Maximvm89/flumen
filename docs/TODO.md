# Flumen Pipeline — TODO

Running backlog of things to build/fix. Newest context at the top of each section.

## UX / app

- [ ] **Empty-task open warning.** When opening a task in Blender that has **no
  published version *and* no local work file**, show a clear prompt
  ("No published version or local work file for `<entity>` — open a new empty
  Blender scene to start this task?") instead of silently launching Blender with
  its default cube. Today the status bar says "new scene" but it's easy to miss
  and reads as "the model disappeared." (`workspace_app/gui.py:_open_task_in_blender`)
- [ ] **Background the sign-in connect.** `_sign_in` runs the SFTP connect +
  config download synchronously on the UI thread, so a bad host/root freezes the
  window until it errors. Move it onto a `Job` thread like the other SFTP ops.

## Review

- [x] **Surface/look review step.** A surface look publish now auto-generates, in
  the background: a **shaded turntable** (model + applied look, neutral-studio or
  HDRI lookdev + grey/chrome balls) and a **texture/UV sheet** (each UDIM tile per
  map, labeled + a UV-wireframe panel). Both attach to the look's publish record and
  show in the Dailies tab (with a "Texture sheet" button) under the same
  to_review/reviewed/approved flow. HDRIs come from `05_library/hdri` (project
  default + per-look override at publish). `flumen look-review` regenerates.
  Follow-ups: per-channel AOV breakdown; render under multiple HDRIs; better
  framing of asset-vs-balls.

## Assets / textures

- [ ] **Texture delivery across machines.** Model files reference external/packed
  textures (e.g. Frankenstein's v007 UDIMs) that may be incomplete-packed or not
  synced to other machines → magenta/purple on open elsewhere. Need a proper way
  for textures to travel with a published asset (pack-complete-on-publish, or sync
  a per-asset `textures/` area), so opening on Windows shows the real shading.

## Caching (Alembic) — PAUSED, revisit

- [ ] **IK/constraint-driven parts frozen in the cache.** Baking a rigged character
  to Alembic (`cache_shot.py::_cache_shot_elements`, the `bpy.ops.wm.alembic_export`
  at ~line 164) loses the parts driven by IK/constraints rather than direct FK weight
  deform: **orso's arms are frozen** and the **teeth sit in the wrong place**, while
  the FK-deformed body bakes correctly. Established by testing:
  - The *source* animation file and a GUI **Build shot** both animate the arms fine —
    so the rig + the rebuild-from-published are correct. The loss is in the **Alembic
    export** step itself.
  - A pre-export pose bake (`nla.bake`) was tried and **reverted** (commit `9620f4e`):
    it ran under headless `-b`, where IK can't solve, so it baked a frozen rest pose
    onto the *whole* rig (broke everything).
  - Switching the cache job to a **GUI Blender** (not `-b`) so rigs evaluate — done
    (commit `29f6c40`, `launch(wait=True)`) — did **not** fix it alone: the export
    operator still doesn't solve constraints while sampling frames.
  - **Next step to try:** now that caching runs in GUI (IK *does* solve there), re-add
    a pose bake to plain keyframes BEFORE export — armature poses *and* bone-parented/
    constrained objects (teeth) — the operation that failed under `-b` should work in
    GUI. Verify with the standalone export/import snippet before shipping.
- [ ] **Cache file size.** orso baked to a **~1 GB** `.abc` (single character), making
  upload/download very slow. Likely the render-level Subdivision Surface baked per
  frame (`evaluation_mode="RENDER"`). Levers: cap/apply subdiv before bake or export
  at viewport eval; drop per-frame `normals` (lighting recomputes). Deferred until the
  animation-correctness bug above is fixed (don't optimise a broken cache).

## Build shot (lighting)

- [ ] **Duplicate instances share one linked datablock → broken + perpetual
  "rebuild".** Placing several copies of the same asset (`skeleton`, `skeleton_1..4`;
  `fantasma_1..10`) logs `WARNING Append: ID 'GRskeleton' is already linked` per copy,
  and the duplicates' geometry falls apart (exploding vertebrae, mesh named
  `Skeleton_Vertebrae02_GEO_004`). Suspected root of the "4 skeletons always show
  needs-rebuild" behaviour too. Investigate `build_shot.py::_link_collection_override`
  for the multi-copy case — each instance needs its own independent override, not a
  shared linked group. (Diagnose before editing; this is a tricky corner of Blender's
  override system.)

## Rendering

- [ ] **Turntable "shadow buffer full" error.** EEVEE runs out of shadow buffer
  during the turntable render. Tune the turntable's shadow settings (shadow
  pool/cube size, soft-shadow steps, or per-light shadow buffer) in
  `flumen/blender_turntable.py` and/or expose them in the `turntable` block of
  `project_settings.json`, so the render doesn't overflow.

## Release / distribution

- [ ] **First release tag (`v0.1.0`).** Installer is ready: `python build.py
  --installer` builds the per-user Windows `Flumen-Setup-<version>.exe` via Inno
  Setup. Follow [docs/RELEASING.md](RELEASING.md) — tag on `main`, build on Windows,
  publish via GitHub Releases. Then automate with CI.
- [ ] **Remove dead code?** `scripts/dist_sync.py` (the old SFTP source-sync) is
  unused now that the workflow is git + tagged releases. Decide: delete or keep.
- [ ] **Process:** re-run `flumen publish-config` whenever project settings or
  the folder schema change, so artists pick it up on next sign-in.

## Roadmap (from README)

- [ ] Maya port of the project-init add-on.
- [ ] Review/dailies loop in the workspace app (view turntables, approve/reject).
- [ ] Shot/animation playblasts (extend the turntable pipeline beyond models).
