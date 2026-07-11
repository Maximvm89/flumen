"""Headless playblast render, driven by env vars from flumen.playblast.

Blender opens the published shot .blend (camera + linked rigs + animation); this
script renders its frame range through the scene camera into a PNG sequence with a
fast engine (Workbench by default), writing an fps sidecar for the encoder.
"""

import json
import math
import os

import bpy

_EEVEE = {"BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"}
_OK_ENGINES = _EEVEE | {"BLENDER_WORKBENCH", "CYCLES"}


def _install_render_progress(scene, label="rendering playblast"):
    """Print a FLUMEN_PROGRESS line per rendered frame so the add-on's publish
    progress bar can follow the background playblast. Best-effort."""
    import time
    start, end = scene.frame_start, scene.frame_end
    total = max(1, end - start + 1)
    t0 = time.monotonic()

    def _on_post(scn, *_a):
        done = max(1, scn.frame_current - start + 1)
        pct = max(0, min(100, int(done * 100 / total)))
        eta = ""
        elapsed = time.monotonic() - t0
        if 0 < done < total and elapsed > 0:
            eta = str(int((total - done) * (elapsed / done)))
        print(f"FLUMEN_PROGRESS {pct} {eta} {label} frame "
              f"{scn.frame_current}/{end}", flush=True)
    try:
        bpy.app.handlers.render_post.append(_on_post)
    except Exception:  # noqa: BLE001
        pass


def _env(key, default=""):
    return os.environ.get(key, default)


def _set_engine(render, requested):
    """Set the requested engine, falling back across EEVEE id changes / Workbench."""
    for eng in (requested, "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"):
        if not eng:
            continue
        try:
            render.engine = eng
            return render.engine
        except (TypeError, ValueError):
            continue
    return render.engine


def _boost_shadows(scene):
    """Bump EEVEE's shadow pool to the largest size so a busy shot doesn't overflow
    it ('Shadow buffer full'). Same fix as the turntable."""
    ee = getattr(scene, "eevee", None)
    if not ee or not hasattr(ee, "shadow_pool_size"):
        return
    try:
        items = [i.identifier for i in
                 ee.bl_rna.properties["shadow_pool_size"].enum_items]
        if items:
            ee.shadow_pool_size = items[-1]
    except Exception:  # noqa: BLE001
        pass


def _ensure_lighting(scene):
    """If the shot carries no lights (typical in layout), add a soft key sun + a
    little ambient world so the EEVEE playblast reads textured + shaded, not black."""
    if any(getattr(o, "type", "") == "LIGHT" for o in scene.objects):
        return
    if scene.world is None:
        scene.world = bpy.data.worlds.new("PB_World")
    try:
        scene.world.use_nodes = True
        bg = scene.world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs[0].default_value = (0.25, 0.25, 0.27, 1.0)   # ambient fill
            bg.inputs[1].default_value = 1.0
    except Exception:  # noqa: BLE001
        pass
    key = bpy.data.lights.new("PB_Key", type="SUN")
    key.energy = 2.5
    ob = bpy.data.objects.new("PB_Key", key)
    ob.rotation_euler = (math.radians(55), 0.0, math.radians(35))
    scene.collection.objects.link(ob)


def main():
    scene = bpy.context.scene
    frames_dir = _env("FLUMEN_PB_FRAMES_DIR")
    if not frames_dir:
        print("[playblast] no frames dir; aborting.")
        return
    os.makedirs(frames_dir, exist_ok=True)

    # A camera is required to render — prefer the scene camera, else the first one.
    if scene.camera is None:
        scene.camera = next((o for o in scene.objects if o.type == "CAMERA"), None)
    if scene.camera is None:
        print("[playblast] no camera in the shot; nothing to render.")
        return

    r = scene.render
    requested = _env("FLUMEN_PB_ENGINE", "BLENDER_EEVEE_NEXT")
    engine = _set_engine(r, requested if requested in _OK_ENGINES else "BLENDER_EEVEE_NEXT")
    # Delivery formats: "16x9:1920x1080,9x16:1080x1920" renders the shot once
    # per format into <frames_dir>/<name>/. Absent -> single legacy render.
    formats = []
    for part in _env("FLUMEN_PB_FORMATS", "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        try:
            name, res = part.split(":", 1)
            x, y = res.lower().split("x", 1)
            formats.append((name.strip(), int(x), int(y)))
        except ValueError:
            print(f"[playblast] bad format spec ignored: {part!r}")
    if not formats:
        formats = [("", int(_env("FLUMEN_PB_RESX", "1280")),
                    int(_env("FLUMEN_PB_RESY", "720")))]
    r.resolution_percentage = 100
    r.film_transparent = False
    r.image_settings.file_format = "PNG"

    # Burn frame number + camera into the corner so reviewers can call timings.
    r.use_stamp = True
    for attr, on in (("use_stamp_frame", True), ("use_stamp_camera", True),
                     ("use_stamp_date", False), ("use_stamp_render_time", False),
                     ("use_stamp_filename", False), ("use_stamp_scene", False)):
        if hasattr(r, attr):
            setattr(r, attr, on)

    # EEVEE (default): renders the real materials + textures + lighting, so the
    # playblast matches the artist's shaded viewport. Make sure it's lit and the
    # shadow pool is big enough.
    if engine in _EEVEE:
        _ensure_lighting(scene)
        _boost_shadows(scene)
    # Workbench: fast solid shading. TEXTURE colour shows the texture maps but is
    # flat/shadeless; MATERIAL shows flat base colours. Opt in via playblast.engine.
    elif engine == "BLENDER_WORKBENCH":
        color = _env("FLUMEN_PB_COLOR", "TEXTURE").upper()
        if color not in {"MATERIAL", "TEXTURE", "SINGLE", "OBJECT", "VERTEX", "RANDOM"}:
            color = "TEXTURE"
        try:
            shading = scene.display.shading
            shading.light = "STUDIO"
            shading.color_type = color
            shading.show_cavity = False
        except Exception:  # noqa: BLE001
            pass

    view = _env("FLUMEN_PB_VIEW", "")
    if view:
        try:
            scene.view_settings.view_transform = view
        except Exception:  # noqa: BLE001
            pass

    # Frame range comes from the file (Build shot set it); allow an env override.
    if _env("FLUMEN_PB_START"):
        scene.frame_start = int(_env("FLUMEN_PB_START"))
    if _env("FLUMEN_PB_END"):
        scene.frame_end = int(_env("FLUMEN_PB_END"))

    with open(os.path.join(frames_dir, "_tt_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"fps": int(scene.render.fps)}, fh)

    # Element breakdown for the playblast HUD: each element holder carries the step
    # it was loaded from + the animation version playing (stamped at Build/publish).
    elements = []
    for c in bpy.data.collections:
        if c.name.startswith("element__"):
            elements.append({"id": c.name[len("element__"):],
                             # legacy fallback: shots published before the app
                             # rename carry legami_* stamps
                             "step": c.get("flumen_step", "") or c.get("legami_step", ""),
                             "anim": c.get("flumen_anim", "") or c.get("legami_anim", "")})
    elements.sort(key=lambda e: e["id"])

    _install_render_progress(scene)
    for name, x, y in formats:
        fdir = os.path.join(frames_dir, name) if name else frames_dir
        os.makedirs(fdir, exist_ok=True)
        r.resolution_x, r.resolution_y = x, y
        r.filepath = os.path.join(fdir, "frame_")
        with open(os.path.join(fdir, "_pb_info.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"elements": elements}, fh)
        print(f"[playblast] {engine} {x}x{y}"
              + (f" [{name}]" if name else "")
              + f" frames {scene.frame_start}-{scene.frame_end} "
                f"cam={scene.camera.name}")
        bpy.ops.render.render(animation=True)


main()
