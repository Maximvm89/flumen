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

- [x] **RESOLVED (v0.18.0–v0.18.2): "arms frozen / teeth wrong / lost visibility on
  rebuild" was NEVER an Alembic export bug — it was the animation publish→rebuild
  round trip.** The cache just inherited a broken rebuild. Three real root causes,
  all now fixed in `build_shot.py`:
  1. **Rig-control custom properties not captured** (`f7cf295`). `_snapshot_poses`
     keyed transforms but not bone custom props, so an unkeyed Rigify `IK_FK` switch
     reset to the rig default on rebuild → arms posed in FK flipped to IK → T-pose.
     Fix: `_snapshot_poses` now also keys each pose bone's numeric custom properties.
  2. **Global-name matching broke duplicate instances** (`280bc59`). Animation was
     keyed to objects by collision-suffixed global names; a 2nd instance (orso_1)
     rebuilt under different suffixes → no match. Fix: match by stable override-
     reference (source) name via `_stable_obj_name`.
  3. **`_stale_content_filter` deleted per-object keys** (`bc2e437`) — the actual
     visibility-loss culprit, proved by `_ANIM_DEBUG_LOG`. It dropped every non-
     armature key on a (mis-fired) content mismatch, eating a 2nd instance's
     `hide_viewport` keys. Now not applied — reference-name matching already prevents
     wrong-object landing, so the filter was obsolete and destructive.
  Confirmed working on the real orso 2-instance shot. `_stale_content_filter` is now
  defined-but-unused; `_ANIM_DEBUG_LOG` diagnostic left in (silent, cleared per build).
- [ ] **Cache file size.** orso baked to a **~1 GB** `.abc` (single character), making
  upload/download very slow. Likely the render-level Subdivision Surface baked per
  frame (`evaluation_mode="RENDER"`). Levers: cap/apply subdiv before bake or export
  at viewport eval; drop per-frame `normals` (lighting recomputes). Still open, now
  worth doing since the cache is correct.
- [ ] **Reminder: animated visibility must use the "Disable in Viewports" (monitor)
  toggle keyed, NOT the eye icon.** The eye icon (`hide_set`) is temporary/per-viewlayer
  and never captured. Also note: animated `hide_viewport` does NOT survive Alembic
  export (documented Blender limitation) — so for the LIGHTING/cache build, a visibility
  sidecar is still needed if shows/hides must reach the render. Not yet built.

## Build shot (multiple instances)

- [ ] **Verify the skeletons.** The `WARNING Append: ID 'GRskeleton' is already linked`
  + exploding vertebrae on `skeleton_1..4` was hypothesised to be a separate override-
  independence bug, but it may have been the same animation round-trip bugs now fixed
  above (wrong action → wrong instance scrambled the deform). **Test:** rebuild a shot
  with the multiple skeletons; if they come back clean, there is no separate bug. If
  they still explode, investigate `build_shot.py::_link_collection_override` for the
  multi-copy override case. (User to check when convenient.)

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
