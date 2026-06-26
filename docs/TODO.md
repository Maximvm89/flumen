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

## Assets / textures

- [ ] **Texture delivery across machines.** Model files reference external/packed
  textures (e.g. Frankenstein's v007 UDIMs) that may be incomplete-packed or not
  synced to other machines → magenta/purple on open elsewhere. Need a proper way
  for textures to travel with a published asset (pack-complete-on-publish, or sync
  a per-asset `textures/` area), so opening on Windows shows the real shading.

## Release / distribution

- [ ] **First release tag.** Once the Windows build + sign-in flow is verified,
  cut `v0.1.0` on `main` and publish the bundle via GitHub Releases.
- [ ] **Remove dead code?** `scripts/dist_sync.py` (the old SFTP source-sync) is
  unused now that the workflow is git + tagged releases. Decide: delete or keep.
- [ ] **Process:** re-run `animpipe publish-config` whenever project settings or
  the folder schema change, so artists pick it up on next sign-in.

## Roadmap (from README)

- [ ] Maya port of the project-init add-on.
- [ ] Review/dailies loop in the workspace app (view turntables, approve/reject).
- [ ] Shot/animation playblasts (extend the turntable pipeline beyond models).
