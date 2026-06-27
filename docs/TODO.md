# Legami Pipeline — TODO

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
  default + per-look override at publish). `animpipe look-review` regenerates.
  Follow-ups: per-channel AOV breakdown; render under multiple HDRIs; better
  framing of asset-vs-balls.

## Assets / textures

- [ ] **Texture delivery across machines.** Model files reference external/packed
  textures (e.g. Frankenstein's v007 UDIMs) that may be incomplete-packed or not
  synced to other machines → magenta/purple on open elsewhere. Need a proper way
  for textures to travel with a published asset (pack-complete-on-publish, or sync
  a per-asset `textures/` area), so opening on Windows shows the real shading.

## Rendering

- [ ] **Turntable "shadow buffer full" error.** EEVEE runs out of shadow buffer
  during the turntable render. Tune the turntable's shadow settings (shadow
  pool/cube size, soft-shadow steps, or per-light shadow buffer) in
  `animpipe/blender_turntable.py` and/or expose them in the `turntable` block of
  `project_settings.json`, so the render doesn't overflow.

## Release / distribution

- [ ] **First release tag (`v0.1.0`).** Installer is ready: `python build.py
  --installer` builds the per-user Windows `Legami-Setup-<version>.exe` via Inno
  Setup. Follow [docs/RELEASING.md](RELEASING.md) — tag on `main`, build on Windows,
  publish via GitHub Releases. Then automate with CI.
- [ ] **Remove dead code?** `scripts/dist_sync.py` (the old SFTP source-sync) is
  unused now that the workflow is git + tagged releases. Decide: delete or keep.
- [ ] **Process:** re-run `animpipe publish-config` whenever project settings or
  the folder schema change, so artists pick it up on next sign-in.

## Roadmap (from README)

- [ ] Maya port of the project-init add-on.
- [ ] Review/dailies loop in the workspace app (view turntables, approve/reject).
- [ ] Shot/animation playblasts (extend the turntable pipeline beyond models).
