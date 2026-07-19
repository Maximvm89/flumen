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


def _sync_render_visibility(scene):
    """WYSIWYG playblast: the render shows exactly what the animator's viewport
    shows. Artists hide duplicate rigs/helpers with the eye icon or the monitor
    toggle — neither of which a real render respects (renders only honour
    hide_render), so a playblast used to show hidden geometry and drop visible
    geometry whose collection had its camera toggle off. Translate before
    rendering:
      * per object: hide_render = NOT visible in the viewport (the eye, the
        monitor toggle and collection-level hiding all folded in),
      * collections: render toggles neutralized — the per-object flags above
        now carry every decision,
      * keyed monitor toggles: their keyframes are mirrored onto hide_render,
        so mid-shot show/hide swaps render too. (The eye can't be keyed in
        Blender — animators key the monitor icon for timed swaps.)
    Runs on the loaded publish copy in memory; nothing is saved back."""
    try:
        vl = bpy.context.view_layer
        vl.update()
    except Exception:  # noqa: BLE001
        vl = scene.view_layers[0] if scene.view_layers else None
    for coll in bpy.data.collections:
        if coll.library is not None:
            continue                      # linked collections are read-only
        try:
            coll.hide_render = False
        except Exception:  # noqa: BLE001
            pass
    synced = keyed = 0
    for o in scene.objects:
        try:
            vis = o.visible_get(view_layer=vl) if vl else not o.hide_viewport
        except Exception:  # noqa: BLE001
            continue
        try:
            if o.hide_render != (not vis):
                o.hide_render = not vis
                synced += 1
        except Exception:  # noqa: BLE001
            continue                      # pure-linked object — leave as authored
        # Mid-shot swaps: mirror any hide_viewport keys onto hide_render.
        ad = getattr(o, "animation_data", None)
        act = getattr(ad, "action", None) if ad else None
        for fc in (getattr(act, "fcurves", []) or []) if act else []:
            if fc.data_path != "hide_viewport":
                continue
            try:
                for kp in fc.keyframe_points:
                    o.hide_render = bool(round(kp.co[1]))
                    o.keyframe_insert("hide_render", frame=kp.co[0])
                keyed += 1
            except Exception:  # noqa: BLE001
                pass
    if synced or keyed:
        print(f"[playblast] viewport-visibility sync: {synced} object(s) "
              f"aligned to what the viewport shows"
              + (f", {keyed} animated toggle(s) mirrored" if keyed else "")
              + ".")


def _sync_viewport_colors():
    """Workbench evaluates NO shader nodes: it draws a base-color image only
    when one is plugged DIRECTLY into the BSDF, and otherwise the material's
    Viewport Display color — default grey, whatever the shader says. Materials
    authored as plain BSDF colors (flat stylized characters) therefore render
    grey. Sync the viewport color from the BSDF's base color value before
    rendering. Local materials only (linked ones are read-only — with
    build-time looks, the materials that matter in a shot ARE local copies)."""
    synced = 0
    for m in bpy.data.materials:
        if m.library is not None or not m.use_nodes or m.node_tree is None:
            continue
        for node in m.node_tree.nodes:
            if node.type not in ("BSDF_PRINCIPLED", "BSDF_DIFFUSE", "EMISSION"):
                continue
            inp = (node.inputs.get("Base Color") or node.inputs.get("Color"))
            if inp is None or inp.links:
                continue        # image/node-driven: a flat color can't help
            c = list(inp.default_value)[:4]
            if len(c) == 3:
                c.append(1.0)
            try:
                if any(abs(a - b) > 0.01 for a, b in zip(m.diffuse_color, c)):
                    m.diffuse_color = c
                    synced += 1
            except Exception:  # noqa: BLE001
                pass
            break
    if synced:
        print(f"[playblast] Workbench: synced viewport colors on {synced} "
              f"material(s) from their shader base color.")


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

    # The playblast contract: it shows what the animator's viewport showed.
    _sync_render_visibility(scene)

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
    # Blender 4.4+/5.x: file_format enum is filtered by media_type. An animator's
    # session set to VIDEO output (FFmpeg flipbooks) only offers FFMPEG — switch
    # to IMAGE before choosing PNG, or this line throws and no frame renders.
    try:
        r.image_settings.media_type = "IMAGE"
    except (AttributeError, TypeError):
        pass
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
        _sync_viewport_colors()
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

    # Nested delivery formats: any format NARROWER than the primary (e.g. 9:16
    # next to a 16:9 primary) renders as a centered slice of it — same vertical
    # FOV, same pixel size — so the vertical clip is literally the middle of the
    # horizontal one. Achieved by locking the camera's vertical sensor size to
    # the primary's effective vertical size for those passes.
    cam = scene.camera.data
    base_x, base_y = formats[0][1], formats[0][2]
    orig_fit, orig_h = cam.sensor_fit, cam.sensor_height
    if orig_fit == "VERTICAL":
        nest_h = None            # vertical FOV already fixed -> formats nest
    elif orig_fit == "HORIZONTAL" or base_x >= base_y:
        nest_h = cam.sensor_width * (base_y / base_x)
    else:
        nest_h = None            # portrait-primary AUTO: leave as-is

    _install_render_progress(scene)
    for name, x, y in formats:
        fdir = os.path.join(frames_dir, name) if name else frames_dir
        os.makedirs(fdir, exist_ok=True)
        r.resolution_x, r.resolution_y = x, y
        if nest_h is not None and x / y < base_x / base_y - 1e-6:
            cam.sensor_fit, cam.sensor_height = "VERTICAL", nest_h
        else:
            cam.sensor_fit, cam.sensor_height = orig_fit, orig_h
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
