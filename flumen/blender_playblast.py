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


def _sweatbox_hdri_path():
    """A studio-light HDRI shipped with Blender, for the Material-Preview look.
    Overridable via FLUMEN_PB_SWEATBOX_HDRI (a name in Blender's studiolights or
    an absolute path). Returns '' when none is found."""
    want = _env("FLUMEN_PB_SWEATBOX_HDRI", "")
    if want and os.path.isfile(want):
        return want
    try:
        base = os.path.join(bpy.utils.system_resource("DATAFILES"),
                            "studiolights", "world")
        files = sorted(f for f in os.listdir(base)
                       if f.lower().endswith((".exr", ".hdr")))
    except Exception:  # noqa: BLE001
        return ""
    if not files:
        return ""
    # Prefer a bright, neutral studio HDRI; else the first shipped one.
    for pref in (want, "forest.exr", "city.exr", "interior.exr", "studio.exr"):
        if pref and pref in files:
            return os.path.join(base, pref)
    return os.path.join(base, files[0])


def _apply_sweatbox(scene):
    """Give the render the viewport's MATERIAL-PREVIEW look: light every shader
    with a studio HDRI world and IGNORE the shot's own lights, so shaders read
    consistently even in an unlit anim/layout scene. Returns True if applied."""
    # Material Preview never uses scene lights — hide them from the render.
    for o in scene.objects:
        if getattr(o, "type", "") == "LIGHT":
            try:
                o.hide_render = True
            except Exception:  # noqa: BLE001
                pass
    hdri = _sweatbox_hdri_path()
    if not hdri:
        print("[playblast] sweatbox: no studio HDRI found — falling back to the "
              "auto key/fill light rig.")
        _ensure_lighting(scene)
        return False
    try:
        world = bpy.data.worlds.new("SWEATBOX_World")
        world.use_nodes = True
        nt = world.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputWorld")
        bg = nt.nodes.new("ShaderNodeBackground")
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(hdri, check_existing=True)
        try:
            strength = float(_env("FLUMEN_PB_SWEATBOX_STRENGTH", "1.0"))
        except ValueError:
            strength = 1.0
        bg.inputs["Strength"].default_value = strength
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
        scene.world = world
        # The HDRI must LIGHT the shot, not appear in it: a forest photo
        # behind the set reads as a broken background in review. Transparent
        # film keeps the world's lighting/reflections but drops it from
        # camera rays — the empty background encodes black in the MP4.
        scene.render.film_transparent = True
    except Exception as exc:  # noqa: BLE001
        print(f"[playblast] sweatbox: HDRI world setup failed ({exc}); using the "
              f"auto light rig.")
        _ensure_lighting(scene)
        return False
    print(f"[playblast] sweatbox: Material-Preview HDRI world "
          f"({os.path.basename(hdri)}), scene lights ignored.")
    return True


def _cull_offscreen(scene, base_x, base_y):
    """Drop every object whose bounds NEVER enter the camera frustum across
    the frame range. A dressed set costs real render time even off-camera:
    EEVEE uploads its geometry + textures to the GPU, the depsgraph
    re-evaluates it every frame, and raytracing considers it — for a review
    render that is pure waste. Culled objects get hide_viewport (OUT of the
    depsgraph: no per-frame eval, no VRAM), not just hide_render.

    Conservative on purpose: the frustum is widened by FLUMEN_PB_CULL_MARGIN
    (default 20%), the range is sampled every FLUMEN_PB_CULL_STEP frames
    (default 10) so animated objects that pass through frame are kept, an
    object is only out when ALL its bound-box corners fall outside the SAME
    frustum plane, and anything straddling the camera plane is kept. The cost
    is off-screen props vanishing from reflections/bounce — acceptable in a
    review. FLUMEN_PB_CULL=0 disables. Returns how many objects were culled."""
    cam_ob = scene.camera
    if cam_ob is None:
        return 0
    from mathutils import Vector
    try:
        k = 1.0 + float(_env("FLUMEN_PB_CULL_MARGIN", "0.2"))
    except ValueError:
        k = 1.2
    try:
        step = max(1, int(_env("FLUMEN_PB_CULL_STEP", "10")))
    except ValueError:
        step = 10
    types = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
    cand = [o for o in scene.objects
            if o.type in types and not o.hide_render and not o.hide_viewport]
    frames = list(range(scene.frame_start, scene.frame_end + 1, step))
    if frames and frames[-1] != scene.frame_end:
        frames.append(scene.frame_end)
    seen = set()
    for f in frames:
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        try:  # lens can be animated — rebuild the projection every sample
            proj = cam_ob.calc_matrix_camera(dg, x=base_x, y=base_y)
        except Exception:  # noqa: BLE001
            return 0
        to_clip = proj @ cam_ob.matrix_world.inverted()
        for o in cand:
            if o.name in seen:
                continue
            ev = o.evaluated_get(dg)
            mw = ev.matrix_world
            box = [mw @ Vector(c) for c in ev.bound_box]
            pts = [to_clip @ p.to_4d() for p in box]
            if all(p.w <= 0 for p in pts):
                continue                      # fully behind the camera: out
            if any(p.w <= 0 for p in pts):
                seen.add(o.name)              # straddles the camera: keep
                continue
            out = any(all(t(p) for p in pts) for t in (
                lambda p: p.x < -p.w * k, lambda p: p.x > p.w * k,
                lambda p: p.y < -p.w * k, lambda p: p.y > p.w * k))
            if not out:
                seen.add(o.name)
        if len(seen) == len(cand):
            return 0                          # everything visible at some point
    culled = [o for o in cand if o.name not in seen]
    for o in culled:
        try:
            o.hide_render = True
            o.hide_viewport = True
        except Exception:  # noqa: BLE001
            pass
    if culled:
        print(f"[playblast] frustum cull: {len(culled)}/{len(cand)} object(s) "
              f"never in frame across {frames[0]}-{frames[-1]} — dropped from "
              f"the depsgraph (FLUMEN_PB_CULL=0 disables).")
    return len(culled)


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
      * mid-shot show/hide swaps: baked into hide_render KEYS up front (see
        _bake_render_visibility), so a rig that flips variants mid-shot renders
        the same variant the viewport shows.
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
    synced = 0
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
    if synced:
        print(f"[playblast] viewport-visibility sync: {synced} object(s) "
              f"aligned to what the viewport shows.")
    _bake_render_visibility(scene)


def _hide_is_animated(o):
    """True when this object's viewport visibility CHANGES over time: a driver
    or F-curve on hide_viewport/hide_render at the object level (drivers walk
    ad.drivers; keys walk legacy .fcurves plus 4.4+ slotted channelbags)."""
    ad = getattr(o, "animation_data", None)
    if ad is None:
        return False
    for fc in getattr(ad, "drivers", []) or []:
        if "hide" in fc.data_path:
            return True
    act = getattr(ad, "action", None)
    if act is None:
        return False
    fcs = list(getattr(act, "fcurves", []) or [])            # legacy
    for lay in getattr(act, "layers", []) or []:             # slotted
        for st in getattr(lay, "strips", []) or []:
            try:
                cb = st.channelbag(ad.action_slot) if ad.action_slot else None
            except Exception:  # noqa: BLE001
                cb = None
            if cb:
                fcs.extend(cb.fcurves)
    return any("hide" in fc.data_path for fc in fcs)


def _bake_render_visibility(scene):
    """Bake per-frame viewport visibility into KEYS on hide_render before
    rendering — the only mechanism a background animation render honours.

    A mid-shot visibility swap is rarely a keyframe on the mesh: rigs drive it
    with a DRIVER reading a rig switch (the cat's bandage variants). Renders
    only honour hide_render, so the swap must be mirrored. A per-frame handler
    (frame_change_post/render_pre) is NOT the way: measured in background
    mode, animation evaluates into the render depsgraph's copies only, the
    originals' hide_viewport stays stale, and visible_get() inside the
    handler returns garbage — the keyed cube never disappeared and the driven
    one rendered wrong from frame one. Plain frame_set OUTSIDE a render
    evaluates drivers and keys correctly (measured too), so: walk the frame
    range up front, sample visible_get per frame for the few objects whose
    hide is animated, and key hide_render (CONSTANT steps) at every change.
    The render's own animation evaluation then plays those keys back."""
    try:
        vl = bpy.context.view_layer
    except Exception:  # noqa: BLE001
        vl = scene.view_layers[0] if scene.view_layers else None
    cands = []
    for o in scene.objects:
        try:
            if _hide_is_animated(o):
                cands.append(o)
        except Exception:  # noqa: BLE001
            continue
    if not cands:
        return 0
    rows = {o.name: [] for o in cands}
    cur = scene.frame_current
    for f in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(f)
        for o in cands:
            try:
                rows[o.name].append((f, bool(o.visible_get(view_layer=vl))))
            except Exception:  # noqa: BLE001
                pass
    baked = 0
    for o in cands:
        prev = None
        keyed_frames = []
        for f, vis in rows[o.name]:
            if vis == prev:
                continue
            prev = vis
            try:
                o.hide_render = not vis
                o.keyframe_insert("hide_render", frame=f)
                keyed_frames.append(f)
            except Exception:  # noqa: BLE001 — pure-linked: leave as authored
                break
        if keyed_frames:
            baked += 1
            ad = o.animation_data
            act = getattr(ad, "action", None) if ad else None
            fcs = list(getattr(act, "fcurves", []) or []) if act else []
            for lay in getattr(act, "layers", []) or []:
                for st in getattr(lay, "strips", []) or []:
                    try:
                        cb = st.channelbag(ad.action_slot)
                    except Exception:  # noqa: BLE001
                        cb = None
                    if cb:
                        fcs.extend(cb.fcurves)
            for fc in fcs:
                if fc.data_path == "hide_render":
                    for k in fc.keyframe_points:
                        k.interpolation = "CONSTANT"
    scene.frame_set(cur)
    if baked:
        print(f"[playblast] visibility bake: keyed hide_render on {baked} "
              f"object(s) with animated viewport visibility (drivers/keys "
              f"sampled {scene.frame_start}-{scene.frame_end}).")
    return baked


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
        # Workbench TEXTURE mode draws each material's ACTIVE image node — if
        # a non-image node was active when the file was saved (the artist's
        # last click), the material renders flat grey. Point it at the
        # base-color image (or any image) before rendering.
        nt = m.node_tree
        active = getattr(nt.nodes, "active", None)
        if active is None or active.type != "TEX_IMAGE":
            target = None
            for nd in nt.nodes:
                if nd.type != "BSDF_PRINCIPLED":
                    continue
                inp = nd.inputs.get("Base Color")
                if (inp and inp.links
                        and inp.links[0].from_node.type == "TEX_IMAGE"):
                    target = inp.links[0].from_node
                break
            if target is None:
                target = next((n for n in nt.nodes if n.type == "TEX_IMAGE"
                               and n.image is not None), None)
            if target is not None:
                try:
                    nt.nodes.active = target
                except Exception:  # noqa: BLE001
                    pass
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
        sweatbox = _env("FLUMEN_PB_SWEATBOX", "0") == "1"
        # Sweatbox: studio-HDRI Material-Preview look (scene lights ignored).
        # Normal playblast: auto key/fill rig only when the shot has no lights.
        if sweatbox:
            _apply_sweatbox(scene)
        else:
            _ensure_lighting(scene)
        _boost_shadows(scene)
        # Preview quality: a few samples read fine in motion, and raytraced
        # GI/reflections are wasted on a fast playblast — together this is most
        # of the difference between a "playblast" and a full render. The sweatbox
        # opts back INTO more samples + raytracing so HDRI reflections/GI and
        # contact shadows read (it's a shading review, not a speed pass).
        ee = getattr(scene, "eevee", None)
        if ee is not None:
            try:
                ee.taa_render_samples = max(1, int(_env("FLUMEN_PB_SAMPLES",
                                                        "64" if sweatbox
                                                        else "16")))
            except Exception:  # noqa: BLE001
                pass
            try:
                ee.use_raytracing = bool(sweatbox)
            except Exception:  # noqa: BLE001
                pass
            if sweatbox:
                # use_shadows: EEVEE Next; use_fast_gi: EEVEE Next ambient bounce
                # for soft HDRI GI. Both guarded — names differ across versions
                # and the old 'use_gtao' is gone in EEVEE Next.
                for attr in ("use_shadows", "use_fast_gi"):
                    try:
                        setattr(ee, attr, True)
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
    # The nesting base is the PROJECT's primary format, passed in separately —
    # rendering only the narrow format (a Sweatbox tick box) must not turn that
    # format into its own base, or it would be framed differently than in a
    # full dual-format render. Falls back to the first rendered format.
    base_x, base_y = formats[0][1], formats[0][2]
    try:
        bx, by = _env("FLUMEN_PB_BASE", "").lower().split("x", 1)
        base_x, base_y = int(bx), int(by)
    except ValueError:
        pass
    orig_fit, orig_h = cam.sensor_fit, cam.sensor_height
    if orig_fit == "VERTICAL":
        nest_h = None            # vertical FOV already fixed -> formats nest
    elif orig_fit == "HORIZONTAL" or base_x >= base_y:
        nest_h = cam.sensor_width * (base_y / base_x)
    else:
        nest_h = None            # portrait-primary AUTO: leave as-is

    # Off-camera cull, computed against the WIDEST frustum (the nesting base
    # covers every narrower format). Default on for the sweatbox — the dressed
    # environment is where render time exploded — opt-in/out via FLUMEN_PB_CULL.
    sweat = _env("FLUMEN_PB_SWEATBOX", "0") == "1"
    if _env("FLUMEN_PB_CULL", "1" if sweat else "0") == "1":
        try:
            _cull_offscreen(scene, base_x, base_y)
        except Exception as exc:  # noqa: BLE001 — never lose a render to this
            print(f"[playblast] frustum cull skipped: {exc}")

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
