"""Headless turntable render. Run by:
    blender --background <model.blend> --python blender_turntable.py

Reads parameters from env (set by `animpipe turntable`):
    LEGAMI_TT_OUTPUT  final .mp4 path
    LEGAMI_TT_FRAMES  number of frames for a full 360 (default 120)
    LEGAMI_TT_RESX/RESY/FPS, LEGAMI_TT_ENGINE (EEVEE|CYCLES), LEGAMI_TT_VIEW
Builds a turntable rig (orbit camera + neutral studio sun lighting) sized to the
model's bounding box, renders a 360 spin to an MP4.
"""

import glob
import math
import os

import bpy
import mathutils


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
        print("[Legami] (note) could not force linear interpolation:", exc)


def _apply_view(scene):
    view = os.environ.get("LEGAMI_TT_VIEW")
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
    frames_dir = (os.environ.get("LEGAMI_TT_FRAMES_DIR")
                  or os.path.join(os.path.dirname(os.environ.get("LEGAMI_TT_OUTPUT", ".")),
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


def _delete_hierarchy(obj):
    for child in list(obj.children):
        _delete_hierarchy(child)
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:  # noqa: BLE001
        pass


def run_template_mode():
    """Append the model into the artist's turntable template, drop the showcase
    placeholder, seat the model on the asset socket, render with the template."""
    scene = bpy.context.scene
    model = os.environ.get("LEGAMI_TT_MODEL")
    control_name = os.environ.get("LEGAMI_TT_CONTROL", "")
    remove_names = [n for n in os.environ.get("LEGAMI_TT_REMOVE", "").split("||") if n]

    for nm in remove_names:
        ob = bpy.data.objects.get(nm)
        if ob:
            _delete_hierarchy(ob)
            print(f"[Legami] removed placeholder '{nm}'")

    locator_name = os.environ.get("LEGAMI_TT_LOCATOR", "")
    with bpy.data.libraries.load(model, link=False) as (src, dst):
        dst.objects = list(src.objects)
    print(f"[Legami] model file: {model}")
    print(f"[Legami] objects in model: {[o.name for o in dst.objects if o]}")

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
        print(f"[Legami] using locator '{locator_name}' -> {len(candidates)} object(s)")
    else:
        candidates = [o for o in dst.objects if o]
        if locator_name:
            print(f"[Legami] locator '{locator_name}' not found; using all geometry")

    roots = []
    linked = []
    for obj in candidates:
        if obj is None or obj.type in _SKIP_TYPES:
            continue
        scene.collection.objects.link(obj)
        obj.hide_render = False
        obj.hide_viewport = False
        linked.append(obj)
        if obj.parent is None or obj.parent not in candidates:
            roots.append(obj)
    print(f"[Legami] linked {len(linked)} object(s): "
          f"{[(o.name, o.type) for o in linked]}")

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
        # Rest height: top surface of the ground object (e.g. the pedestal) if
        # given, otherwise the socket's own Z.
        rest_z = target.z
        ground_name = os.environ.get("LEGAMI_TT_GROUND", "")
        ground = bpy.data.objects.get(ground_name) if ground_name else None
        if ground:
            _gmn, gmx = _world_bbox([ground])
            rest_z = gmx[2]
        dest = mathutils.Vector((target.x, target.y, rest_z))
        # 2) Measure the model's ACTUAL world bbox, then translate it so its
        #    bottom-center lands on the socket x,y at the rest height. No assumptions.
        mesh_objs = [o for o in linked if o.type == "MESH"]
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
            print(f"[Legami] seated '{obj.name}' at {wloc} (socket "
                  f"{tuple(round(v, 3) for v in target)})")
    elif control_name:
        print(f"[Legami] WARNING: socket '{control_name}' not found in template; "
              f"model left at origin")

    # Respect the template's color/engine/resolution/animation; only set output.
    frames_dir = _png_output(scene)
    bpy.ops.render.render(animation=True)
    _write_meta(frames_dir, scene)
    print("[Legami] template turntable frames rendered to", frames_dir)


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

    frames = int(os.environ.get("LEGAMI_TT_FRAMES", "120"))
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
    _set_engine(scene, os.environ.get("LEGAMI_TT_ENGINE", "EEVEE"))
    r.resolution_x = int(os.environ.get("LEGAMI_TT_RESX", "1280"))
    r.resolution_y = int(os.environ.get("LEGAMI_TT_RESY", "720"))
    r.resolution_percentage = 100
    r.fps = int(os.environ.get("LEGAMI_TT_FPS", "24"))
    # Render a PNG sequence; the toolkit encodes the MP4 afterwards (this build
    # of Blender may lack FFmpeg, so we never rely on Blender for video).
    _apply_view(scene)
    frames_dir = _png_output(scene)
    bpy.ops.render.render(animation=True)
    _write_meta(frames_dir, scene)
    print("[Legami] turntable frames rendered to", frames_dir)


try:
    if os.environ.get("LEGAMI_TT_MODEL"):
        run_template_mode()
    else:
        build_and_render()
finally:
    bpy.ops.wm.quit_blender()
