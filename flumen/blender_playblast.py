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
    """Playblast light rig for shots that carry no lights (typical in layout):
    two SHADOWLESS suns parented to the shot camera (key from the upper-left of
    the view, softer fill from the lower-right) plus a touch of world ambient.
    Shadowless suns pass through walls — a closed interior set reads like the
    artist's studio-lit viewport instead of black — have no distance falloff to
    tune per set scale, and are the cheapest light EEVEE can render. A previous
    single fixed sun couldn't reach inside enclosed environments.
    Shots with their own lights are left untouched; FLUMEN_PB_AUTOLIGHT=0
    (playblast.auto_light in project settings) disables the rig entirely."""
    if _env("FLUMEN_PB_AUTOLIGHT", "1") == "0":
        return
    if any(getattr(o, "type", "") == "LIGHT" for o in scene.objects):
        return
    if scene.world is None:
        scene.world = bpy.data.worlds.new("PB_World")
    try:
        scene.world.use_nodes = True
        bg = scene.world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs[0].default_value = (0.12, 0.12, 0.13, 1.0)   # ambient floor
            bg.inputs[1].default_value = 1.0
    except Exception:  # noqa: BLE001
        pass
    for name, energy, rx, ry in (("PB_Key", 2.2, -25.0, 30.0),
                                 ("PB_Fill", 0.7, 10.0, -40.0)):
        lt = bpy.data.lights.new(name, type="SUN")
        lt.energy = energy
        try:
            lt.use_shadow = False
        except Exception:  # noqa: BLE001
            pass
        ob = bpy.data.objects.new(name, lt)
        if scene.camera is not None:
            ob.parent = scene.camera       # rig follows the animated camera
            ob.rotation_euler = (math.radians(rx), math.radians(ry), 0.0)
        else:
            ob.rotation_euler = (math.radians(55 + rx), 0.0,
                                 math.radians(35 + ry))
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

    # A shot's geometry is all LINKED (only the camera is local). If the element
    # publishes are missing on this machine (e.g. cleaned from disk after Build
    # shot), Blender loads empty placeholders and the clip renders as an empty
    # void — fail loudly instead of shipping that to dailies.
    missing = [lib.filepath for lib in bpy.data.libraries
               if not os.path.isfile(bpy.path.abspath(lib.filepath))]
    empty = [c.name for c in bpy.data.collections
             if c.name.startswith("element__") and len(c.all_objects) == 0]
    if empty:
        print("[playblast] ERROR: these shot elements are EMPTY — the playblast "
              "would render a void:")
        for name in empty:
            print(f"    {name}")
        if missing:
            print("[playblast] missing linked libraries:")
            for m in missing:
                print(f"    {m}")
        print("[playblast] Re-run 'Build shot' (or re-open the task from the "
              "Workspace app) to re-fetch the publishes, then publish again.")
        return
    if missing:
        print("[playblast] warning: missing linked libraries (render may lack "
              "content):")
        for m in missing:
            print(f"    {m}")

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
    # Preview scale: 50 renders at half the delivery size (~4x fewer pixels).
    try:
        r.resolution_percentage = max(1, min(100, int(_env("FLUMEN_PB_RESPCT",
                                                           "100"))))
    except ValueError:
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
        # Preview quality: a few samples read fine in motion, and raytraced
        # GI/reflections are wasted on a playblast — together this is most of
        # the difference between a "playblast" and a full render.
        ee = getattr(scene, "eevee", None)
        if ee is not None:
            try:
                ee.taa_render_samples = max(1, int(_env("FLUMEN_PB_SAMPLES",
                                                        "16")))
            except Exception:  # noqa: BLE001
                pass
            for attr in ("use_raytracing",):
                try:
                    setattr(ee, attr, False)
                except Exception:  # noqa: BLE001
                    pass
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
