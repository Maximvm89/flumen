# Legami Blender Pipeline — Tool 1: Project Init

Gives every artist an identical Blender setup (color management + render/units/
output) pulled from one file on the FTP, and guarantees the project OCIO config
is loaded via a cross-platform launcher.

## The pieces

| Piece | Where | Job |
|---|---|---|
| `project_settings.json` | FTP: `/shared/Legami/02_pipeline/` | Single source of truth for all Blender settings. TD edits this. |
| OCIO config | FTP: `/shared/Legami/02_pipeline/ocio/config.ocio` | The pinned ACES color config. |
| Launcher | `animpipe launch` + `launcher/` wrappers | Syncs the above from FTP, sets `BLENDER_OCIO`, starts Blender. |
| Addon `legami_pipeline` | installed in Blender | Applies the settings to the scene; per-user FTP login for future tools. |

Flow: **artist double-clicks the launcher → it syncs config from FTP & sets OCIO
→ Blender opens → artist clicks "Apply Project Settings" in the Legami panel.**

## One-time, by the pipeline TD

1. Upload the settings + color config to the server (from the animpipe folder):
   ```bash
   ./color_pipeline/get_ocio_config.sh
   python3 -m animpipe put --local color_pipeline/cg-config-v2.2.0_aces-v1.3_ocio-v2.4.ocio --remote 02_pipeline/ocio/
   python3 -m animpipe put --local color_pipeline/config.ocio                                 --remote 02_pipeline/ocio/
   python3 -m animpipe put --local pipeline_config/project_settings.json                      --remote 02_pipeline/
   ```
2. To change a project default later: edit `project_settings.json`, re-`put` it.
   Every artist gets it on their next launch + Apply.

## One-time, per artist

1. **Install the launcher side:** they need the `animpipe` folder + its `.venv`
   (see the main README) and the `launcher/` wrapper for their OS.
2. **Install the addon:** Blender > Edit > Preferences > Add-ons > Install... >
   pick `legami_pipeline.zip` > enable "Legami Pipeline".
3. **Enter their FTP login:** in the addon preferences (per-user host/user/
   password). This is for the in-Blender "Pull Latest" button and future publish
   tools.
4. (Optional) If Blender isn't in a standard location, set `tools.blender_path`
   in `config.yaml` or the `LEGAMI_BLENDER` env var.

## Daily use

- **Launch Blender via the wrapper** for your OS in `launcher/`
  (`Legami-Launch-mac.command` / `-windows.bat` / `-linux.sh`), or run
  `python3 -m animpipe launch`. This syncs the latest config and sets OCIO.
- In Blender: open the 3D-view sidebar (**N**) > **Legami** tab >
  **Apply Project Settings**. Use **Verify Color Config** to confirm OCIO loaded.
- Save your startup file once after applying, if you want new scenes pre-set.

## Notes & limitations

- **Why a launcher?** Blender reads its OCIO config at startup; there's no
  reliable runtime API to swap it. The launcher sets `BLENDER_OCIO` before
  Blender starts — the only bulletproof way to guarantee correct color.
- **Color names** in `project_settings.json` (display/view/look) must match the
  active OCIO config's displays & views. The addon warns (doesn't crash) if a
  name is missing — check the system console for skipped settings.
- **Credentials** are stored in Blender's preferences unencrypted; on shared
  machines leave the password blank and enter it per session.
- **"Pull Latest From FTP"** needs paramiko inside Blender — click
  "Install Dependencies" in the addon preferences once (needs internet). The
  launcher path doesn't need this (it uses animpipe's own environment).

## Next tools (same pattern)

- **Publish** (asset/shot) from Blender into the right `publish/` folder with
  versioning, using the per-user login.
- A **Maya** version of this addon reading the same `project_settings.json`.
