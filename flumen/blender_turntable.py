"""Headless turntable render. Run by:
    blender --background <model.blend> --python blender_turntable.py

Reads parameters from env (set by `flumen turntable`):
    FLUMEN_TT_OUTPUT  final .mp4 path
    FLUMEN_TT_FRAMES  number of frames for a full 360 (default 120)
    FLUMEN_TT_RESX/RESY/FPS, FLUMEN_TT_ENGINE (EEVEE|CYCLES), FLUMEN_TT_VIEW
Builds a turntable rig (orbit camera + neutral studio sun lighting) sized to the
model's bounding box, renders a 360 spin to an MP4.
"""

import glob
import math
import os

import bpy
import mathutils

# Preview mode: set up the framing but don't render or quit, so the artist can
# look through the turntable camera interactively and dial in the scale.
_PREVIEW = os.environ.get("FLUMEN_TT_PREVIEW", "0") not in ("0", "", "false", "False")


def _install_render_progress(scene, label="rendering turntable"):
    """Print a FLUMEN_PROGRESS line after each rendered frame so the add-on's
    publish progress bar can follow the background render. Best-effort."""
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


def _bbox_center_size():
    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objs:
        return mathutils.Vector((0, 0, 0)), 1.0
    mn = mathutils.Vector((1e18, 1e18, 1e18))
    mx = mathutils.Vector((-1e18, -1e18, -1e18))
    for o in objs:
        for corner in o.bound_box:
            wc = o.matrix_world @ mathutils.Vector(corner)
            mn = mathutils.Vector((min(mn.x, wc.x), min(mn.y, wc.y), min(mn.z, wc.z)))
            mx = mathutils.Vector((max(mx.x, wc.x), max(mx.y, wc.y), max(mx.z, wc.z)))
    return (mn + mx) / 2.0, max((mx - mn).length, 0.1)


def _iter_fcurves(action):
    """Yield fcurves across Blender versions (legacy action.fcurves and the
    4.4/5.x slotted-action layers/strips/channelbags)."""
    fcurves = getattr(action, "fcurves", None)
    if fcurves:
        for fc in fcurves:
            yield fc
        return
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for cbag in getattr(strip, "channelbags", []):
                for fc in getattr(cbag, "fcurves", []):
                    yield fc


def _force_linear(obj):
    """Best-effort: set all keyframes to LINEAR. Never raises."""
    try:
        ad = obj.animation_data
        if not ad or not ad.action:
            return
        for fc in _iter_fcurves(ad.action):
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] (note) could not force linear interpolation:", exc)


def _apply_view(scene):
    view = os.environ.get("FLUMEN_TT_VIEW")
    if view:
        try:
            scene.view_settings.view_transform = view
        except (TypeError, ValueError):
            pass


def _png_output(scene):
    r = scene.render
    r.film_transparent = False
    # Blender 4.4+/5.x: file_format enum is filtered by media_type. A template
    # set to VIDEO only offers FFMPEG, so switch to IMAGE before choosing PNG.
    try:
        r.image_settings.media_type = "IMAGE"
    except (AttributeError, TypeError):
        pass
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    frames_dir = (os.environ.get("FLUMEN_TT_FRAMES_DIR")
                  or os.path.join(os.path.dirname(os.environ.get("FLUMEN_TT_OUTPUT", ".")),
                                  "_tt_frames"))
    os.makedirs(frames_dir, exist_ok=True)
    r.filepath = os.path.join(frames_dir, "frame_")
    return frames_dir


def _write_meta(frames_dir, scene):
    import json
    try:
        with open(os.path.join(frames_dir, "_tt_meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"fps": scene.render.fps}, fh)
    except Exception:  # noqa: BLE001
        pass


_SKIP_TYPES = {"CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER"}


def _set_scale_stamp(scene, asset, real_size, scale):
    """Burn the asset's real (un-scaled) size + the applied turntable scale into
    the render, so reviewers don't read the normalized framing as real size."""
    r = scene.render
    for f in ("use_stamp_time", "use_stamp_date", "use_stamp_render_time",
              "use_stamp_frame", "use_stamp_scene", "use_stamp_camera",
              "use_stamp_filename", "use_stamp_memory", "use_stamp_hostname",
              "use_stamp_sequencer_strip", "use_stamp_lens", "use_stamp_marker"):
        if hasattr(r, f):
            setattr(r, f, False)
    r.use_stamp = True
    r.use_stamp_note = True
    r.stamp_note_text = (f"{asset}    real {real_size[0]:.2f} x {real_size[1]:.2f} "
                         f"x {real_size[2]:.2f}    |    turntable scale {scale:.3f}x")
    try:
        r.stamp_font_size = 28
    except (AttributeError, TypeError):
        pass


def _preview_setup(scene):
    """Interactive preview: look through the turntable camera with materials shown,
    no render. The artist eyeballs the framing and tweaks template_fit_scale."""
    try:
        scene.frame_set(scene.frame_start)
    except Exception:  # noqa: BLE001
        pass
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for sp in area.spaces:
                if sp.type == "VIEW_3D":
                    try:
                        sp.region_3d.view_perspective = "CAMERA"
                        sp.shading.type = "MATERIAL"
                    except Exception:  # noqa: BLE001
                        pass
    print("[Flumen] PREVIEW — looking through the turntable camera. Adjust "
          "'template_fit_scale' (and 'template_fit_mode') and re-run; close "
          "Blender when done.")


def _delete_hierarchy(obj):
    for child in list(obj.children):
        _delete_hierarchy(child)
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:  # noqa: BLE001
        pass


def _apply_look(objects):
    """If FLUMEN_LR_LOOK is set, append the look's materials and assign them onto the
    given meshes by name (from the manifest's assignment map). Turns a model turntable
    into a shaded look-review turntable, on the same template rig."""
    import json
    look = os.environ.get("FLUMEN_LR_LOOK")
    if not look or not os.path.isfile(look):
        return
    names = []
    with bpy.data.libraries.load(look, link=False) as (src, dst):
        names = list(src.materials)
        dst.materials = list(src.materials)
    mats = {nm: m for nm, m in zip(names, dst.materials) if m is not None}
    try:
        assignments = json.load(open(os.environ.get("FLUMEN_LR_MANIFEST", ""))) \
            .get("assignments", {})
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] look manifest unreadable:", exc)
        assignments = {}
    by_name = {o.name: o for o in objects}
    assigned = 0
    for mesh_name, slot_mats in assignments.items():
        obj = by_name.get(mesh_name) or bpy.data.objects.get(mesh_name)
        if obj is None or obj.type != "MESH":
            continue
        me = obj.data
        for i, mname in enumerate(slot_mats):
            mat = mats.get(mname) if mname else None
            if i < len(me.materials):
                me.materials[i] = mat
            else:
                me.materials.append(mat)
        assigned += 1
    print(f"[Flumen] applied look: {len(mats)} material(s) -> {assigned} mesh(es)")


def _export_uv_segments(objects):
    """Dump the UV wireframe as deduped edge segments to FLUMEN_LR_UV_OUT (JSON) — the
    toolkit draws it with PIL. bpy.ops.uv.export_layout needs a GPU and fails in
    --background, so we read loop UVs directly."""
    out = os.environ.get("FLUMEN_LR_UV_OUT")
    if not out:
        return
    import json
    meshes = [o for o in objects if o.type == "MESH" and not o.hide_render]
    seen, segs, max_u, max_v = set(), [], 1.0, 1.0
    for o in meshes:
        uvl = o.data.uv_layers.active
        if not uvl:
            continue
        uvs = uvl.data
        for poly in o.data.polygons:
            loops = list(poly.loop_indices)
            n = len(loops)
            for i in range(n):
                a = uvs[loops[i]].uv
                b = uvs[loops[(i + 1) % n]].uv
                key = (round(a.x, 4), round(a.y, 4), round(b.x, 4), round(b.y, 4))
                if key in seen or (key[2], key[3], key[0], key[1]) in seen:
                    continue
                seen.add(key)
                segs.append([round(a.x, 5), round(a.y, 5),
                             round(b.x, 5), round(b.y, 5)])
                max_u = max(max_u, a.x, b.x)
                max_v = max(max_v, a.y, b.y)
    try:
        with open(out, "w") as fh:
            json.dump({"segments": segs, "max_u": max_u, "max_v": max_v}, fh)
        print(f"[Flumen] UV wireframe -> {out} ({len(segs)} edges)")
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] UV export skipped:", exc)


def run_template_mode():
    """Append the model into the artist's turntable template, drop the showcase
    placeholder, seat the model on the asset socket, render with the template."""
    scene = bpy.context.scene
    model = os.environ.get("FLUMEN_TT_MODEL")
    control_name = os.environ.get("FLUMEN_TT_CONTROL", "")
    remove_names = [n for n in os.environ.get("FLUMEN_TT_REMOVE", "").split("||") if n]

    # Framing: scale the incoming asset to fit a reference object's volume (the
    # showcase placeholder the camera was composed around), times a zoom knob.
    # fit_scale < 1 zooms out (more margin), > 1 zooms in. Measure the reference
    # BEFORE it gets removed below.
    fit_name = os.environ.get("FLUMEN_TT_FIT", "")
    try:
        fit_scale = float(os.environ.get("FLUMEN_TT_FIT_SCALE") or "1") or 1.0
    except ValueError:
        fit_scale = 1.0
    fit_mode = (os.environ.get("FLUMEN_TT_FIT_MODE", "box") or "box").lower()
    do_stamp = os.environ.get("FLUMEN_TT_STAMP", "1") not in ("0", "", "false", "False")
    asset_name = os.path.splitext(os.path.basename(model or "asset"))[0]
    fit_size = None
    if fit_name:
        ref = bpy.data.objects.get(fit_name)
        if ref:
            rmn = [1e18, 1e18, 1e18]
            rmx = [-1e18, -1e18, -1e18]
            for corner in ref.bound_box:
                w = ref.matrix_world @ mathutils.Vector(corner)
                for i in range(3):
                    rmn[i] = min(rmn[i], w[i])
                    rmx[i] = max(rmx[i], w[i])
            fit_size = [rmx[i] - rmn[i] for i in range(3)]
            print(f"[Flumen] fit reference '{fit_name}' size="
                  f"{tuple(round(s, 3) for s in fit_size)}")
        else:
            print(f"[Flumen] fit reference '{fit_name}' not found; no scaling")

    for nm in remove_names:
        ob = bpy.data.objects.get(nm)
        if ob:
            _delete_hierarchy(ob)
            print(f"[Flumen] removed placeholder '{nm}'")

    locator_name = os.environ.get("FLUMEN_TT_LOCATOR", "")
    with bpy.data.libraries.load(model, link=False) as (src, dst):
        dst.objects = list(src.objects)
    print(f"[Flumen] model file: {model}")
    print(f"[Flumen] objects in model: {[o.name for o in dst.objects if o]}")

    # If a publish locator is present, bring in ONLY it + its descendants;
    # otherwise fall back to all geometry.
    chosen = None
    if locator_name:
        for o in dst.objects:
            if o and o.name.split(".")[0] == locator_name:
                chosen = o
                break
    if chosen is not None:
        wanted = {chosen}
        stack = [chosen]
        while stack:
            cur = stack.pop()
            for c in cur.children:
                wanted.add(c)
                stack.append(c)
        candidates = [o for o in wanted]
        print(f"[Flumen] using locator '{locator_name}' -> {len(candidates)} object(s)")
    else:
        candidates = [o for o in dst.objects if o]
        if locator_name:
            print(f"[Flumen] locator '{locator_name}' not found; using all geometry")

    # Per-asset override (custom props on the PUBLISH locator, set in Blender via
    # Flumen ▸ Turntable). Takes precedence over the project_settings defaults.
    if chosen is not None and chosen.get("flumen_tt_override"):
        m = chosen.get("flumen_tt_fit_mode")
        if m in ("box", "height", "width"):
            fit_mode = m
        sc = chosen.get("flumen_tt_fit_scale")
        if sc is not None:
            try:
                fit_scale = float(sc)
            except (TypeError, ValueError):
                pass
        print(f"[Flumen] per-asset turntable override: "
              f"fit_mode={fit_mode} fit_scale={fit_scale}")

    roots = []
    linked = []
    hidden_n = 0
    for obj in candidates:
        if obj is None or obj.type in _SKIP_TYPES:
            continue
        scene.collection.objects.link(obj)
        # Carry over the artist's visibility. An object disabled in renders
        # (camera icon = hide_render) OR disabled in viewports (monitor icon =
        # hide_viewport) in the source file stays hidden in the turntable; both
        # flags survive the append and both keep an object out of the render.
        # NOTE: the outliner *eye* (H / hide_set) is a per-view-layer toggle that
        # does NOT survive appending into the turntable scene — it cannot be
        # honored, so use the camera/monitor icon to hide from the turntable.
        hidden = bool(obj.hide_render or obj.hide_viewport)
        obj.hide_render = hidden
        obj.hide_viewport = hidden
        if hidden:
            hidden_n += 1
        linked.append(obj)
        if obj.parent is None or obj.parent not in candidates:
            roots.append(obj)
    print(f"[Flumen] linked {len(linked)} object(s): "
          f"{[(o.name, o.type) for o in linked]}")
    if hidden_n:
        print(f"[Flumen] kept {hidden_n} object(s) hidden per artist visibility "
              f"(camera/monitor icon)")

    # A look review reuses this whole template pipeline — just shade the model with
    # the published look first, and export its UVs for the texture/UV sheet.
    _apply_look(linked)
    _export_uv_segments(linked)

    def _world_bbox(objs):
        mn = [1e18, 1e18, 1e18]
        mx = [-1e18, -1e18, -1e18]
        for o in objs:
            for corner in o.bound_box:
                w = o.matrix_world @ mathutils.Vector(corner)
                for i in range(3):
                    mn[i] = min(mn[i], w[i])
                    mx[i] = max(mx[i], w[i])
        return mn, mx

    ctrl = bpy.data.objects.get(control_name) if control_name else None
    if ctrl:
        target = ctrl.matrix_world.translation.copy()
        # 1) Parent everything to the socket (identity) so it spins with it.
        ident = mathutils.Matrix.Identity(4)
        for obj in roots:
            obj.parent = ctrl
            obj.matrix_parent_inverse = ident
        bpy.context.view_layer.update()
        # 1b) Face the camera. Studio convention: models are authored with
        # their front along -Y (Blender's front view). Yaw the asset around
        # the socket so that front meets THIS template's camera at the first
        # frame — modelers never rotate a model to please a turntable again.
        # Everything is measured on the EVALUATED depsgraph: right after
        # parenting, the plain matrix_world can be stale (missing the socket's
        # own rotation — this template's socket sits at -96°), which is exactly
        # how the asset used to come out sideways. The result is written to
        # matrix_basis in socket space, bypassing the world-setter entirely.
        # (Yaw math = flumen.turntable.face_camera_yaw — inlined because this
        # script runs inside Blender's python, not the toolkit's.)
        if os.environ.get("FLUMEN_TT_FACE_CAMERA", "1") != "0" and scene.camera:
            try:
                scene.frame_set(scene.frame_start)
            except Exception:  # noqa: BLE001
                pass
            deps = bpy.context.evaluated_depsgraph_get()
            sock_world = ctrl.evaluated_get(deps).matrix_world.copy()
            cam_pos = scene.camera.evaluated_get(deps).matrix_world.translation
            # The authored front (-Y in the model file) as it now stands in the
            # template world = the socket's rotation applied to -Y.
            front = sock_world.to_3x3() @ mathutils.Vector((0.0, -1.0, 0.0))
            to_cam = cam_pos - sock_world.translation
            yaw = math.atan2(front.x * to_cam.y - front.y * to_cam.x,
                             front.x * to_cam.x + front.y * to_cam.y)
            yaw += math.radians(
                float(os.environ.get("FLUMEN_TT_FRONT_OFFSET", "0") or 0.0))
            if abs(yaw) > 1e-4:
                spin = (mathutils.Matrix.Translation(sock_world.translation)
                        @ mathutils.Matrix.Rotation(yaw, 4, "Z")
                        @ mathutils.Matrix.Translation(-sock_world.translation))
                inv_sock = sock_world.inverted()
                for obj in roots:
                    world = obj.evaluated_get(deps).matrix_world.copy()
                    obj.matrix_basis = inv_sock @ (spin @ world)
                bpy.context.view_layer.update()
                print(f"[Flumen] faced asset to the camera "
                      f"(yaw {math.degrees(yaw):.1f}°)")
        # Scale the asset to fit the reference volume (tightest axis fits inside),
        # then apply the zoom knob. Seating below re-measures and re-centers.
        mesh_now = [o for o in linked if o.type == "MESH" and not o.hide_render]
        if fit_size and mesh_now:
            mn, mx = _world_bbox(mesh_now)
            msize = [mx[i] - mn[i] for i in range(3)]
            # How the asset is sized to the reference volume:
            #   box   - fit the whole bounding box inside (tightest axis; safe)
            #   height- fill vertically (Z); good for characters, arms may run wide
            #   width - fit the widest horizontal extent
            if fit_mode == "height" and msize[2] > 1e-6:
                base = fit_size[2] / msize[2]
            elif fit_mode == "width" and max(msize[0], msize[1]) > 1e-6:
                base = min(fit_size[0], fit_size[1]) / max(msize[0], msize[1])
            else:
                rs = [fit_size[i] / msize[i] for i in range(3) if msize[i] > 1e-6]
                base = min(rs) if rs else None
            if base:
                s = base * fit_scale
                for obj in roots:
                    obj.scale = obj.scale * s
                bpy.context.view_layer.update()
                print(f"[Flumen] scaled asset x{s:.4f} to fit '{fit_name}' "
                      f"(zoom={fit_scale})")
                print(f"[Flumen] asset real size = "
                      f"{tuple(round(v, 3) for v in msize)} (scene units); the "
                      f"turntable is normalized, NOT real-scale")
                if s < 0.5 or s > 2.0:
                    print(f"[Flumen] WARNING: large fit scale {s:.3f}x — check the "
                          f"asset's real-world units; it may be modeled too big/small.")
                if do_stamp:
                    _set_scale_stamp(scene, asset_name, msize, s)
        # Rest height: top surface of the ground object (e.g. the pedestal) if
        # given, otherwise the socket's own Z.
        rest_z = target.z
        ground_name = os.environ.get("FLUMEN_TT_GROUND", "")
        ground = bpy.data.objects.get(ground_name) if ground_name else None
        if ground:
            _gmn, gmx = _world_bbox([ground])
            rest_z = gmx[2]
        dest = mathutils.Vector((target.x, target.y, rest_z))
        # 2) Measure the model's ACTUAL world bbox, then translate it so its
        #    bottom-center lands on the socket x,y at the rest height. No assumptions.
        mesh_objs = [o for o in linked if o.type == "MESH" and not o.hide_render]
        if mesh_objs:
            mn, mx = _world_bbox(mesh_objs)
            anchor = mathutils.Vector(((mn[0] + mx[0]) / 2.0,
                                       (mn[1] + mx[1]) / 2.0, mn[2]))
            off = dest - anchor
            for obj in roots:
                m = obj.matrix_world.copy()
                m.translation = m.translation + off
                obj.matrix_world = m
            bpy.context.view_layer.update()
        for obj in roots:
            wloc = tuple(round(v, 3) for v in obj.matrix_world.translation)
            print(f"[Flumen] seated '{obj.name}' at {wloc} (socket "
                  f"{tuple(round(v, 3) for v in target)})")
    elif control_name:
        print(f"[Flumen] WARNING: socket '{control_name}' not found in template; "
              f"model left at origin")

    # Respect the template's color/engine/resolution/animation; only set output.
    if _PREVIEW:
        _preview_setup(scene)
        return
    _boost_shadows(scene)
    frames_dir = _png_output(scene)
    _install_render_progress(scene)
    bpy.ops.render.render(animation=True)
    _write_meta(frames_dir, scene)
    print("[Flumen] template turntable frames rendered to", frames_dir)


def _boost_shadows(scene):
    """EEVEE Next renders a virtual shadow‑map atlas; a busy template (several
    lights) + a shaded asset can overflow it → 'Shadow buffer full (… / 2048)' and
    dropped shadows. Bump the shadow pool to the largest available size."""
    ee = getattr(scene, "eevee", None)
    if not ee or not hasattr(ee, "shadow_pool_size"):
        return
    try:
        items = [i.identifier for i in
                 ee.bl_rna.properties["shadow_pool_size"].enum_items]
        if items:
            ee.shadow_pool_size = items[-1]   # largest pool
            print(f"[Flumen] shadow pool -> {items[-1]}")
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] could not raise shadow pool:", exc)


def _set_engine(scene, want):
    candidates = (["CYCLES"] if want.upper() == "CYCLES"
                  else ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"])
    for eng in candidates:
        try:
            scene.render.engine = eng
            return eng
        except (TypeError, ValueError):
            continue
    return scene.render.engine


def build_and_render():
    scene = bpy.context.scene
    _apply_look(list(scene.objects))
    _export_uv_segments(list(scene.objects))
    center, size = _bbox_center_size()

    pivot = bpy.data.objects.new("TT_Pivot", None)
    scene.collection.objects.link(pivot)
    pivot.location = center

    cam_data = bpy.data.cameras.new("TT_Cam")
    cam = bpy.data.objects.new("TT_Cam", cam_data)
    scene.collection.objects.link(cam)
    dist = size * 1.6
    cam.location = center + mathutils.Vector((0.0, -dist, size * 0.25))
    cam.parent = pivot
    track = cam.constraints.new("TRACK_TO")
    track.target = pivot
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    scene.camera = cam

    frames = int(os.environ.get("FLUMEN_TT_FRAMES", "120"))
    scene.frame_start = 1
    scene.frame_end = frames
    # Make new keyframes LINEAR up front so the spin is constant speed (works
    # regardless of the Action fcurve API, which changed in Blender 4.4/5.x).
    try:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    except (AttributeError, TypeError):
        pass
    pivot.rotation_euler = (0, 0, 0)
    pivot.keyframe_insert("rotation_euler", frame=1)
    pivot.rotation_euler = (0, 0, math.radians(360))
    pivot.keyframe_insert("rotation_euler", frame=frames + 1)
    _force_linear(pivot)

    # Neutral studio: dim world + three suns (scale-independent).
    world = scene.world or bpy.data.worlds.new("TT_World")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.05, 0.05, 0.05, 1.0)
        bg.inputs[1].default_value = 1.0

    def add_sun(name, rot, energy):
        ld = bpy.data.lights.new(name, "SUN")
        ld.energy = energy
        ld.angle = math.radians(5)
        ob = bpy.data.objects.new(name, ld)
        scene.collection.objects.link(ob)
        ob.rotation_euler = rot
        return ob

    add_sun("TT_Key", (math.radians(50), 0, math.radians(40)), 3.0)
    add_sun("TT_Fill", (math.radians(60), 0, math.radians(-120)), 1.2)
    add_sun("TT_Rim", (math.radians(120), 0, math.radians(180)), 2.0)

    r = scene.render
    _set_engine(scene, os.environ.get("FLUMEN_TT_ENGINE", "EEVEE"))
    r.resolution_x = int(os.environ.get("FLUMEN_TT_RESX", "1280"))
    r.resolution_y = int(os.environ.get("FLUMEN_TT_RESY", "720"))
    r.resolution_percentage = 100
    r.fps = int(os.environ.get("FLUMEN_TT_FPS", "24"))
    # Render a PNG sequence; the toolkit encodes the MP4 afterwards (this build
    # of Blender may lack FFmpeg, so we never rely on Blender for video).
    _apply_view(scene)
    if _PREVIEW:
        _preview_setup(scene)
        return
    _boost_shadows(scene)
    frames_dir = _png_output(scene)
    _install_render_progress(scene)
    bpy.ops.render.render(animation=True)
    _write_meta(frames_dir, scene)
    print("[Flumen] turntable frames rendered to", frames_dir)


def uv_only():
    """Fast path for a sheet‑only review: just dump the UV wireframe of the open
    model's published geometry (no render). The model is opened directly, so keep
    only the PUBLISH locator's meshes if present."""
    loc = bpy.data.objects.get(os.environ.get("FLUMEN_TT_LOCATOR", "PUBLISH"))
    meshes = ([o for o in loc.children_recursive if o.type == "MESH"] if loc
              else [o for o in bpy.context.scene.objects if o.type == "MESH"])
    _export_uv_segments(meshes)
    print("[Flumen] UV‑only export complete")


try:
    if os.environ.get("FLUMEN_TT_UV_ONLY"):
        uv_only()
    elif os.environ.get("FLUMEN_TT_MODEL"):
        run_template_mode()
    else:
        build_and_render()
finally:
    if not _PREVIEW:
        bpy.ops.wm.quit_blender()
