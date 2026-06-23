# Legami — Color Pipeline (OCIO / ACES)

This is the project color policy. Goal: consistent color from Blender through to
the Premiere edit, using ACES as the common foundation.

## The one constraint that shapes everything

**Premiere Pro cannot read OCIO config files.** As of the 2025 release Adobe
overhauled color management, but full OCIO/ACES config support is still
work-in-progress — only After Effects has true OCIO integration. So the rule is:

> Color is managed in **Blender** (with the ACES OCIO config). Blender bakes the
> ACES look into a standard **Rec.709** deliverable. Premiere only ever sees
> normal Rec.709 footage and never touches the OCIO file.

Because this project is **Premiere-final / simple** (no separate Resolve/Nuke
grade), we keep it lean: render display-ready Rec.709 for the edit, and keep a
scene-linear EXR master per shot in case a regrade is ever needed.

## Project color settings (the policy)

| Stage | Setting |
|---|---|
| Config | `cg-config-v2.2.0_aces-v1.3_ocio-v2.4.ocio` (ACES 1.3, CG, pinned) |
| Blender working/scene space | **ACEScg** (Linear) |
| View transform (SDR edit) | **ACES 1.3 — Rec.709** |
| Texture albedo/color maps | **sRGB - Texture** (or `sRGB`) |
| Texture data maps (normal, roughness, metal, masks) | **Non-Color / Raw** |
| Edit deliverable to Premiere | **Rec.709**, view transform baked in |
| Shot master (archive/regrade) | **OpenEXR, ACEScg** (scene-linear) |
| Premiere sequence | **Rec.709 (SDR)** |

We pin **one** config version for the whole project so every artist gets
identical color regardless of their Blender build.

---

## Part A — Blender 5.1.2 setup

Blender 5.x ships ACES support in its bundled config (ACEScg working space +
ACES 1.3/2.0 view transforms). You have two ways to run it; pick one and keep
the whole team on the same choice.

### Option 1 — Pinned project config (recommended)

Guarantees everyone uses the exact same color, even across Blender builds.

1. Get the config (run once):
   ```bash
   cd color_pipeline
   ./get_ocio_config.sh
   ```
   This downloads `cg-config-v2.2.0_aces-v1.3_ocio-v2.4.ocio` and makes a
   `config.ocio` symlink next to it.

2. Put it in the project so it ships with the show:
   upload the `.ocio` file (and the `config.ocio` link) to
   `/shared/Legami/02_pipeline/ocio/`.

3. Each artist points Blender at their local synced copy via an environment
   variable **before launching Blender**:
   ```bash
   # macOS / Linux (add to ~/.zshrc or a studio launch script)
   export BLENDER_OCIO="/path/to/Legami/02_pipeline/ocio/config.ocio"
   ```
   ```bat
   :: Windows (set as a System Environment Variable)
   setx BLENDER_OCIO "X:\Legami\02_pipeline\ocio\config.ocio"
   ```
   `BLENDER_OCIO` overrides the config for Blender only (won't disturb other
   apps). Restart Blender; it now loads the project config.

### Option 2 — Native built-in ACES (simplest, if everyone is on 5.1.2)

If the whole team is reliably on Blender 5.1.2, you can skip the download and
use the bundled config:

1. `Preferences > System > Color Management` → leave config source on **Blender
   OCIO** (the bundled config).
2. No env var needed. The trade-off: color is tied to each person's exact
   Blender version. Fine for a small team on one build; Option 1 is safer long-term.

### Per-scene settings (either option)

In each `.blend`, `Scene Properties > Color Management`:

1. **Working space / Scene Linear** → **ACEScg** (5.x exposes this as the blend
   file working color space; set it to ACEScg).
2. **View Transform** → **ACES 1.3 — Rec.709** (SDR).
3. **Look** → None (apply any look in Premiere's Lumetri instead, to keep the
   master neutral).
4. Save this as your **startup file** (`File > Defaults > Save Startup File`) so
   every new scene starts correct.

### Textures — set color space on import

This is the #1 source of wrong color. In the Image Texture node:

- **Color / albedo / diffuse** maps → `sRGB - Texture`
- **Everything else** (normal, roughness, metallic, displacement, masks, AO) →
  `Non-Color`

### Render output settings

Set two outputs per shot.

**1. Edit deliverable (for Premiere)** — `Output Properties`:
- Format: **FFmpeg video → QuickTime → ProRes 422** (edit master) or **H.264**
  (quick review). PNG/JPEG sequence also fine.
- This is display-encoded: the ACES → Rec.709 view transform is **baked in**, so
  Premiere reads it as ordinary Rec.709. Do **not** also tag/convert it in Premiere.
- Lands in: `06_renders/<shot>/` → published to `07_dailies/` for review.

**2. Shot master (archive / possible regrade)** — render a second pass or use the
compositor File Output node:
- Format: **OpenEXR**, Color space **ACEScg** (or `ACES2065-1` for long-term
  archive), 16-bit half, scene-linear (no view transform baked).
- Lands in: the shot's `lighting/publish/` (or `comp/publish/`).
- You only need this if you later want to regrade in an OCIO-aware app; for the
  Premiere-final path the Rec.709 deliverable is what gets cut.

---

## Part B — Premiere Pro (2025+) handoff

Because Blender already baked the look into Rec.709, Premiere's job is simple.

1. **Sequence color space:** Rec.709 (SDR). In Premiere 2025's color management,
   keep the project on the standard SDR / Rec.709 pipeline.
2. **Incoming footage:** your Blender Rec.709 ProRes/H.264 is already display-
   referred. Let Premiere interpret it as Rec.709. **Do not** apply any input
   LUT or log/HDR tone-mapping to it — that would double-transform the image.
   (Premiere's automatic tone mapping only targets HDR/log sources, so standard
   Rec.709 clips pass through untouched.)
3. **Grading:** do light creative grading in **Lumetri**. Keep it modest — the
   ACES render is already the intended look.
4. **Export:** Rec.709 / SDR. H.264 for reviews, ProRes 422 HQ for masters.
   Outputs go to `08_delivery/versions/` (cuts) and `08_delivery/final/`.

### If you ever outgrow "Premiere-final"

If a real grade becomes necessary later, the EXR ACEScg masters are already
there: take them into **DaVinci Resolve** or **After Effects** (both read the
same pinned OCIO config), grade in ACES, and render Rec.709 back to editorial.
Nothing about the Blender side changes.

---

## Quick gotchas checklist

- Wrong/washed-out textures → albedo not set to `sRGB - Texture`, or data maps
  not set to `Non-Color`.
- Double-bright or crushed edit → view transform applied twice (baked in Blender
  *and* re-interpreted in Premiere). Pick one (it's Blender).
- Colors differ between artists → not everyone on the same pinned config
  (use Option 1).
- Don't apply a Blender **Look** if you plan to grade in Premiere; keep the
  master neutral.
- EXR masters are scene-linear (no view transform); the ProRes/H.264 edit
  deliverable is display-encoded (view transform baked). Don't mix them up.
