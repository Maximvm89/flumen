"""Headless shaded look-review turntable. Run by:
    blender --background --python blender_look_review.py

Loads the published model (locator subtree only), applies a look (materials by the
manifest's assignment map), lights it with an HDRI lookdev environment (+ grey and
chrome reference balls), renders a 360 camera-orbit turntable to a PNG sequence, and
exports the UV layout. Driven by env (set by `animpipe look-review`):

    LEGAMI_LR_MODEL     model .blend (geometry)
    LEGAMI_LR_LOOK      look .blend (materials only)
    LEGAMI_LR_MANIFEST  look manifest json (assignment map)
    LEGAMI_LR_HDRI      HDRI for the world (optional; neutral grey if absent)
    LEGAMI_LR_LOCATOR   publish locator name (default PUBLISH)
    LEGAMI_LR_UV_OUT    UV layout PNG output path (optional)
    LEGAMI_TT_OUTPUT / LEGAMI_TT_FRAMES_DIR / LEGAMI_TT_FRAMES / RESX / RESY /
    LEGAMI_TT_FPS / LEGAMI_TT_ENGINE / LEGAMI_TT_VIEW
"""

import json
import math
import os

import bpy
import mathutils

LOCATOR = os.environ.get("LEGAMI_LR_LOCATOR", "PUBLISH")


def _clear_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)


def _load_model_subtree(path):
    """Append the model and keep ONLY the PUBLISH locator's subtree (no stray
    cubes/cameras/lights from the work scene)."""
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        dst.objects = list(src.objects)
    appended = [o for o in dst.objects if o is not None]
    locator = next((o for o in appended if getattr(o, "type", "") == "EMPTY"
                    and o.name.split(".")[0] == LOCATOR), None)
    if locator is not None:
        keep = {locator, *locator.children_recursive}
    else:
        keep = {o for o in appended if o.type in ("MESH", "EMPTY")}
    coll = bpy.context.scene.collection.objects
    kept = []
    for o in appended:
        if o in keep:
            if o.name not in coll:
                coll.link(o)
            kept.append(o)
        else:
            bpy.data.objects.remove(o, do_unlink=True)
    return [o for o in kept if o.type == "MESH"]


def _apply_look(look_blend, manifest_path):
    names = []
    with bpy.data.libraries.load(look_blend, link=False) as (src, dst):
        names = list(src.materials)
        dst.materials = list(src.materials)
    mats = {nm: m for nm, m in zip(names, dst.materials) if m is not None}
    try:
        assignments = json.load(open(manifest_path)).get("assignments", {})
    except Exception as exc:  # noqa: BLE001
        print("[Legami] look manifest unreadable:", exc)
        assignments = {}
    assigned = 0
    for mesh_name, slot_mats in assignments.items():
        obj = bpy.data.objects.get(mesh_name)
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
    print(f"[Legami] applied look: {len(mats)} material(s) -> {assigned} mesh(es)")


def _add_sun(scene, name, rot, energy):
    ld = bpy.data.lights.new(name, "SUN")
    ld.energy = energy
    ld.angle = math.radians(5)
    ob = bpy.data.objects.new(name, ld)
    scene.collection.objects.link(ob)
    ob.rotation_euler = rot
    return ob


def _setup_lighting(scene, hdri):
    """HDRI lookdev environment if given, else a neutral studio (dim world + three
    suns) so dark materials still read — the model turntable uses the same rig."""
    world = bpy.data.worlds.new("LR_World")
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes.get("Background")
    if hdri and os.path.isfile(hdri):
        try:
            env = nt.nodes.new("ShaderNodeTexEnvironment")
            env.image = bpy.data.images.load(hdri, check_existing=True)
            nt.links.new(env.outputs["Color"], bg.inputs["Color"])
            bg.inputs[1].default_value = 1.0
            print("[Legami] lookdev HDRI:", os.path.basename(hdri))
            return
        except Exception as exc:  # noqa: BLE001
            print("[Legami] HDRI load failed, neutral studio:", exc)
    bg.inputs[0].default_value = (0.05, 0.05, 0.05, 1.0)
    bg.inputs[1].default_value = 1.0
    _add_sun(scene, "LR_Key", (math.radians(50), 0, math.radians(40)), 3.0)
    _add_sun(scene, "LR_Fill", (math.radians(60), 0, math.radians(-120)), 1.2)
    _add_sun(scene, "LR_Rim", (math.radians(120), 0, math.radians(180)), 2.0)
    print("[Legami] neutral studio (no HDRI)")


def _ball(name, location, radius, base_color, metallic, roughness):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=location,
                                         segments=48, ring_count=24)
    ob = bpy.context.active_object
    ob.name = name
    bpy.ops.object.shade_smooth()
    mat = bpy.data.materials.new(name + "_mat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = base_color
        bsdf.inputs["Metallic"].default_value = metallic
        bsdf.inputs["Roughness"].default_value = roughness
    ob.data.materials.append(mat)
    return ob


def _add_balls(center, size, ground_z):
    r = max(size * 0.09, 0.05)
    x = center.x + size * 0.55
    grey = _ball("LR_GreyBall", (x, center.y, ground_z + r), r,
                 (0.18, 0.18, 0.18, 1.0), 0.0, 0.5)
    chrome = _ball("LR_ChromeBall", (x + r * 2.3, center.y, ground_z + r), r,
                   (1.0, 1.0, 1.0, 1.0), 1.0, 0.0)
    return [grey, chrome]


def _asset_bbox(meshes):
    mn = mathutils.Vector((1e18, 1e18, 1e18))
    mx = mathutils.Vector((-1e18, -1e18, -1e18))
    for o in meshes:
        if o.hide_render:
            continue
        for c in o.bound_box:
            w = o.matrix_world @ mathutils.Vector(c)
            mn = mathutils.Vector((min(mn.x, w.x), min(mn.y, w.y), min(mn.z, w.z)))
            mx = mathutils.Vector((max(mx.x, w.x), max(mx.y, w.y), max(mx.z, w.z)))
    return mn, mx


def _force_linear(obj):
    ad = obj.animation_data
    act = ad.action if ad else None
    if not act:
        return
    fcurves = getattr(act, "fcurves", None) or []
    if not fcurves:
        for layer in getattr(act, "layers", []):
            for strip in getattr(layer, "strips", []):
                for cbag in getattr(strip, "channelbags", []):
                    fcurves = list(fcurves) + list(getattr(cbag, "fcurves", []))
    for fc in fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"


def _build_camera(scene, bmin, bmax, frames, res_x, res_y):
    center = (bmin + bmax) / 2.0
    dx, dy, dz = (bmax.x - bmin.x), (bmax.y - bmin.y), (bmax.z - bmin.z)
    pivot = bpy.data.objects.new("LR_Pivot", None)
    scene.collection.objects.link(pivot)
    pivot.location = center
    cam_data = bpy.data.cameras.new("LR_Cam")
    cam = bpy.data.objects.new("LR_Cam", cam_data)
    scene.collection.objects.link(cam)

    # Frame to fit the bbox: the asset spins, so the worst-case horizontal extent is
    # its footprint diagonal; vertical is its height. Place the camera far enough
    # that BOTH fit the (aspect-dependent) FOV, with a margin.
    hfov = 2 * math.atan(cam_data.sensor_width / (2 * cam_data.lens))
    aspect = max(res_x, 1) / max(res_y, 1)
    vfov = 2 * math.atan(math.tan(hfov / 2) / aspect)
    width = math.hypot(dx, dy)
    d_h = (width / 2) / math.tan(hfov / 2)
    d_v = (dz / 2) / math.tan(vfov / 2)
    dist = max(d_h, d_v, 0.1) * 1.2

    cam.location = center + mathutils.Vector((0.0, -dist, dz * 0.10))
    cam.parent = pivot
    track = cam.constraints.new("TRACK_TO")
    track.target = pivot
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    scene.camera = cam
    scene.frame_start = 1
    scene.frame_end = frames
    try:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    except (AttributeError, TypeError):
        pass
    pivot.rotation_euler = (0, 0, 0)
    pivot.keyframe_insert("rotation_euler", frame=1)
    pivot.rotation_euler = (0, 0, math.radians(360))
    pivot.keyframe_insert("rotation_euler", frame=frames + 1)
    _force_linear(pivot)


def _set_engine(scene, want):
    cands = (["CYCLES"] if want.upper() == "CYCLES"
             else ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"])
    for eng in cands:
        try:
            scene.render.engine = eng
            return
        except (TypeError, ValueError):
            continue


def _png_output(scene):
    r = scene.render
    r.film_transparent = False
    try:
        r.image_settings.media_type = "IMAGE"
    except (AttributeError, TypeError):
        pass
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    frames_dir = (os.environ.get("LEGAMI_TT_FRAMES_DIR")
                  or os.path.join(os.path.dirname(
                      os.environ.get("LEGAMI_TT_OUTPUT", ".")), "_lr_frames"))
    os.makedirs(frames_dir, exist_ok=True)
    r.filepath = os.path.join(frames_dir, "frame_")
    return frames_dir


def _apply_view(scene):
    view = os.environ.get("LEGAMI_TT_VIEW")
    if view:
        try:
            scene.view_settings.view_transform = view
        except (TypeError, ValueError):
            pass


def _write_meta(frames_dir, scene):
    try:
        with open(os.path.join(frames_dir, "_tt_meta.json"), "w") as fh:
            json.dump({"fps": scene.render.fps}, fh)
    except Exception:  # noqa: BLE001
        pass


def _export_uv(meshes, out):
    """Dump the UV wireframe as edge segments to JSON (the toolkit draws it with
    PIL). bpy.ops.uv.export_layout needs a GPU and fails in --background, so we read
    loop UVs directly — headless-safe. Edges are deduped to keep the file lean."""
    if not out or not meshes:
        return
    seen = set()
    segs = []
    max_u = 1.0
    max_v = 1.0
    for o in meshes:
        me = o.data
        uvl = me.uv_layers.active
        if not uvl:
            continue
        uvs = uvl.data
        for poly in me.polygons:
            loops = list(poly.loop_indices)
            n = len(loops)
            for i in range(n):
                a = uvs[loops[i]].uv
                b = uvs[loops[(i + 1) % n]].uv
                key = (round(a.x, 4), round(a.y, 4), round(b.x, 4), round(b.y, 4))
                rkey = (key[2], key[3], key[0], key[1])
                if key in seen or rkey in seen:
                    continue
                seen.add(key)
                segs.append([round(a.x, 5), round(a.y, 5),
                             round(b.x, 5), round(b.y, 5)])
                max_u = max(max_u, a.x, b.x)
                max_v = max(max_v, a.y, b.y)
    try:
        with open(out, "w") as fh:
            json.dump({"segments": segs, "max_u": max_u, "max_v": max_v}, fh)
        print(f"[Legami] UV wireframe -> {out} ({len(segs)} edges, "
              f"{math.ceil(max_u)}x{math.ceil(max_v)} tiles)")
    except Exception as exc:  # noqa: BLE001
        print("[Legami] UV export skipped:", exc)


def main():
    scene = bpy.context.scene
    _clear_scene()
    meshes = _load_model_subtree(os.environ["LEGAMI_LR_MODEL"])
    _apply_look(os.environ["LEGAMI_LR_LOOK"], os.environ.get("LEGAMI_LR_MANIFEST", ""))
    if not meshes:
        print("[Legami] no geometry to render")
        return

    bmin, bmax = _asset_bbox(meshes)
    center = (bmin + bmax) / 2.0
    size = max((bmax - bmin).length, 0.1)
    res_x = int(os.environ.get("LEGAMI_TT_RESX", "1280"))
    res_y = int(os.environ.get("LEGAMI_TT_RESY", "720"))
    _setup_lighting(scene, os.environ.get("LEGAMI_LR_HDRI", ""))
    balls = _add_balls(center, size, bmin.z)
    # Frame over the asset + the reference balls so the balls stay in shot.
    fmin, fmax = _asset_bbox(meshes + balls)
    frames = int(os.environ.get("LEGAMI_TT_FRAMES", "120"))
    _build_camera(scene, fmin, fmax, frames, res_x, res_y)

    r = scene.render
    _set_engine(scene, os.environ.get("LEGAMI_TT_ENGINE", "EEVEE"))
    r.resolution_x = res_x
    r.resolution_y = res_y
    r.resolution_percentage = 100
    r.fps = int(os.environ.get("LEGAMI_TT_FPS", "24"))
    _apply_view(scene)

    _export_uv(meshes, os.environ.get("LEGAMI_LR_UV_OUT", ""))

    frames_dir = _png_output(scene)
    bpy.ops.render.render(animation=True)
    _write_meta(frames_dir, scene)
    print("[Legami] look-review frames rendered to", frames_dir)


try:
    main()
finally:
    bpy.ops.wm.quit_blender()
