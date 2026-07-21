"""Blender operators for the Flumen pipeline addon."""

import json
import os
import shutil
import subprocess
import types

import bpy

from . import settings_io
from . import checks
from . import textures
from . import look as look_mod
from . import anim as anim_mod
from . import dressing as dressing_mod
from ._common import (  # shared toolkit-shell + logging plumbing
    _prefs, _pref_local_root, _toolkit_cmd, PUBLISH_LOG, _publog, _no_window,
    _preflight_server, _shell_toolkit, _shell_json, _apply_one, active_task)
from .lights import (  # lighting operators (registered via CLASSES below)
    FLUMEN_OT_add_lights, FLUMEN_OT_publish_lights, FLUMEN_OT_load_lights)
from .looks import (  # look-apply operator + build-time element-look helper
    FLUMEN_OT_apply_look, _apply_element_look)
from .startup import (  # session-startup hooks (called from __init__) + scaffolds
    enable_project_addons, apply_project_color, scaffold_empty_scene,
    scaffold_surface_scene, _purge_orphan_data)
from .build_shot import (  # build-shot machinery (moved out; re-exported here)
    ELEMENT_HOLDER_PREFIX, FLUMEN_AssemblyItem, FLUMEN_AnimItem,
    FLUMEN_OT_build_shot, FLUMEN_OT_load_animation,
    _snapshot_poses, _collect_element_animation, _element_anim_hashes,
    _element_loaded_file, _project_rel, _named_holder, _fetch_publish_path,
    _link_collection_override, _apply_build_frame_range, _apply_dressing_props,
    _apply_element_animation, _action_fcurves, _ELEMENT_LOADERS)


def apply_settings(scene, data: dict, root: str, report: list):
    """Apply the project_settings dict to a scene. Returns nothing; fills report."""
    cm = data.get("color_management", {})
    rn = data.get("render", {})
    un = data.get("units", {})
    fr = data.get("frame_range", {})
    out = data.get("output", {})

    # --- Color management (names must exist in the active OCIO config) ---
    ds = scene.display_settings
    vs = scene.view_settings
    if cm.get("display_device"):
        _apply_one(report, "display_device",
                   lambda: setattr(ds, "display_device", cm["display_device"]))
    if cm.get("view_transform"):
        _apply_one(report, "view_transform",
                   lambda: setattr(vs, "view_transform", cm["view_transform"]))
    if cm.get("look") is not None:
        _apply_one(report, "look", lambda: setattr(vs, "look", cm["look"]))
    if cm.get("exposure") is not None:
        _apply_one(report, "exposure",
                   lambda: setattr(vs, "exposure", float(cm["exposure"])))
    if cm.get("gamma") is not None:
        _apply_one(report, "gamma", lambda: setattr(vs, "gamma", float(cm["gamma"])))
    if cm.get("sequencer_space"):
        _apply_one(report, "sequencer colorspace",
                   lambda: setattr(scene.sequencer_colorspace_settings, "name",
                                   cm["sequencer_space"]))

    # --- Render ---
    if rn.get("engine"):
        _apply_one(report, "render engine",
                   lambda: setattr(scene.render, "engine", rn["engine"]))
    if rn.get("film_transparent") is not None:
        _apply_one(report, "film transparent",
                   lambda: setattr(scene.render, "film_transparent",
                                   bool(rn["film_transparent"])))
    if rn.get("resolution_x"):
        _apply_one(report, "resolution_x",
                   lambda: setattr(scene.render, "resolution_x", int(rn["resolution_x"])))
    if rn.get("resolution_y"):
        _apply_one(report, "resolution_y",
                   lambda: setattr(scene.render, "resolution_y", int(rn["resolution_y"])))
    if rn.get("resolution_percentage"):
        _apply_one(report, "resolution %",
                   lambda: setattr(scene.render, "resolution_percentage",
                                   int(rn["resolution_percentage"])))
    if rn.get("fps"):
        _apply_one(report, "fps", lambda: setattr(scene.render, "fps", int(rn["fps"])))
    if rn.get("fps_base"):
        _apply_one(report, "fps_base",
                   lambda: setattr(scene.render, "fps_base", float(rn["fps_base"])))

    # --- Cycles (only if that engine is active) ---
    cyc = rn.get("cycles", {})
    if cyc and getattr(scene.render, "engine", "") == "CYCLES" and hasattr(scene, "cycles"):
        if cyc.get("device"):
            _apply_one(report, "cycles device",
                       lambda: setattr(scene.cycles, "device", cyc["device"]))
        if cyc.get("samples"):
            _apply_one(report, "cycles samples",
                       lambda: setattr(scene.cycles, "samples", int(cyc["samples"])))
        if cyc.get("use_denoising") is not None:
            _apply_one(report, "cycles denoising",
                       lambda: setattr(scene.cycles, "use_denoising",
                                       bool(cyc["use_denoising"])))

    # --- EEVEE (only if that engine is active) — the project's finals engine ---
    eev = rn.get("eevee", {})
    if eev and str(getattr(scene.render, "engine", "")).startswith(
            "BLENDER_EEVEE") and hasattr(scene, "eevee"):
        if eev.get("taa_render_samples"):
            _apply_one(report, "eevee samples",
                       lambda: setattr(scene.eevee, "taa_render_samples",
                                       int(eev["taa_render_samples"])))
        if eev.get("use_raytracing") is not None:
            _apply_one(report, "eevee raytracing",
                       lambda: setattr(scene.eevee, "use_raytracing",
                                       bool(eev["use_raytracing"])))

    # --- Frame range ---
    if fr.get("start") is not None:
        _apply_one(report, "frame start",
                   lambda: setattr(scene, "frame_start", int(fr["start"])))
    if fr.get("end") is not None:
        _apply_one(report, "frame end",
                   lambda: setattr(scene, "frame_end", int(fr["end"])))

    # --- Units ---
    if un.get("system"):
        _apply_one(report, "unit system",
                   lambda: setattr(scene.unit_settings, "system", un["system"]))
    if un.get("scale_length"):
        _apply_one(report, "unit scale",
                   lambda: setattr(scene.unit_settings, "scale_length",
                                   float(un["scale_length"])))
    if un.get("length_unit"):
        _apply_one(report, "length unit",
                   lambda: setattr(scene.unit_settings, "length_unit", un["length_unit"]))

    # --- Output ---
    if out.get("base_path_rel"):
        base = os.path.join(root, out["base_path_rel"])
        _apply_one(report, "output path",
                   lambda: setattr(scene.render, "filepath", base + os.sep))
    if out.get("file_format"):
        # media_type filters the file_format enum (Blender 4.4+/5.x): a scene
        # left on VIDEO output only offers FFMPEG until flipped back to IMAGE.
        _apply_one(report, "media type",
                   lambda: setattr(scene.render.image_settings, "media_type",
                                   "IMAGE"))
        _apply_one(report, "file format",
                   lambda: setattr(scene.render.image_settings, "file_format",
                                   out["file_format"]))
    if out.get("color_depth"):
        _apply_one(report, "color depth",
                   lambda: setattr(scene.render.image_settings, "color_depth",
                                   str(out["color_depth"])))
    if out.get("exr_codec"):
        _apply_one(report, "exr codec",
                   lambda: setattr(scene.render.image_settings, "exr_codec",
                                   out["exr_codec"]))


class FLUMEN_OT_apply_project_settings(bpy.types.Operator):
    bl_idname = "flumen.apply_project_settings"
    bl_label = "Apply Project Settings"
    bl_description = "Apply the project's standard color, render, units and output settings to this scene"
    bl_options = {"REGISTER", "UNDO"}

    apply_all_scenes: bpy.props.BoolProperty(
        name="All Scenes", default=False,
        description="Apply to every scene in this file, not just the active one")

    def execute(self, context):
        root = settings_io.find_project_root(_pref_local_root())
        if not root:
            self.report({"ERROR"}, "No project root. Launch via the Flumen launcher, "
                                   "or set Local Project Root in addon preferences.")
            return {"CANCELLED"}
        try:
            data = settings_io.load_settings(root)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        scenes = list(bpy.data.scenes) if self.apply_all_scenes else [context.scene]
        warnings: list[str] = []
        for sc in scenes:
            apply_settings(sc, data, root, warnings)

        ocio = os.environ.get("BLENDER_OCIO", "(not set)")
        if warnings:
            self.report({"WARNING"},
                        f"Applied with {len(warnings)} skipped setting(s). See console.")
            print("[Flumen] Project settings applied with warnings:")
            print("\n".join(warnings))
            print(f"[Flumen] BLENDER_OCIO = {ocio}")
        else:
            self.report({"INFO"}, "Project settings applied.")
        return {"FINISHED"}


class FLUMEN_OT_verify_ocio(bpy.types.Operator):
    bl_idname = "flumen.verify_ocio"
    bl_label = "Verify Color Config"
    bl_description = "Check that Blender loaded the project OCIO config and the project's color names exist"

    def execute(self, context):
        root = settings_io.find_project_root(_pref_local_root())
        env_ocio = os.environ.get("BLENDER_OCIO")
        expected = settings_io.ocio_path(root) if root else None

        msgs = []
        if not env_ocio:
            msgs.append("BLENDER_OCIO is NOT set — Blender is using its bundled config. "
                        "Launch via the Flumen launcher.")
        elif expected and os.path.normpath(env_ocio) != os.path.normpath(expected):
            msgs.append(f"BLENDER_OCIO points to {env_ocio}, expected {expected}.")
        else:
            msgs.append(f"OCIO OK: {env_ocio}")

        # Check the project's color names exist in the active config.
        if root:
            try:
                data = settings_io.load_settings(root)
                cm = data.get("color_management", {})
                vt = cm.get("view_transform")
                views = [i.identifier for i in
                         context.scene.view_settings.bl_rna.properties["view_transform"].enum_items]
                if vt and vt not in views:
                    msgs.append(f"View transform '{vt}' NOT found in active config. "
                                f"Available: {', '.join(views[:8])}...")
                else:
                    msgs.append(f"View transform '{vt}' present.")
            except Exception as exc:  # noqa: BLE001
                msgs.append(f"Could not verify color names: {exc}")

        level = "INFO" if all("OK" in m or "present" in m for m in msgs) else "WARNING"
        self.report({level}, " | ".join(msgs))
        print("[Flumen] Verify color config:\n  " + "\n  ".join(msgs))
        return {"FINISHED"}


class FLUMEN_OT_pull_settings(bpy.types.Operator):
    bl_idname = "flumen.pull_settings"
    bl_label = "Pull Latest From FTP"
    bl_description = "Re-sync the project config (OCIO + project_settings.json) from the FTP"

    def execute(self, context):
        if _shell_toolkit(["sync", "--remote", "02_pipeline"], self.report):
            self.report({"INFO"}, "Synced latest config. Now Apply Project Settings.")
            return {"FINISHED"}
        return {"CANCELLED"}


class FLUMEN_OT_save_to_task(bpy.types.Operator):
    bl_idname = "flumen.save_to_task"
    bl_label = "Save into task work folder"
    bl_description = ("Save the current .blend into this task's work/ folder with "
                      "an auto-incremented version")

    def execute(self, context):
        task = active_task()
        if not task or not task["work_dir"]:
            self.report({"ERROR"}, "No active task. Open this scene from the "
                                   "Workspace app's 'Open in Blender'.")
            return {"CANCELLED"}
        path = _save_work_version(task)
        if not path:
            self.report({"ERROR"}, "Could not save into the work folder — "
                                   "see the pipeline log.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Saved {os.path.basename(path)}")
        return {"FINISHED"}


def _absolute_externals():
    """Local datablocks (images, linked libraries) whose file path is absolute.
    An absolute path is one machine's disk layout — dead the moment the work
    file syncs to a teammate on another OS (C:\\Users\\… read on a Mac)."""
    out = []
    for coll in (bpy.data.images, bpy.data.libraries):
        for d in coll:
            if getattr(d, "library", None) is not None:
                continue                     # owned by a linked file, not ours
            fp = getattr(d, "filepath", "") or ""
            if fp and not fp.startswith("//"):
                out.append(d)
    return out


def _save_work_version(task) -> str | None:
    """Save the current session AS-IS into the task's work/ folder with the
    next version number. Returns the saved path, or None on failure. The
    session's file path becomes the new work file.

    After the save (the file now has a location to be relative TO), any
    absolute texture/library paths are converted to '//' relative and the file
    re-saved — absolute paths are the classic way a work file authored on one
    machine breaks on the next (typically: the model was appended before the
    file was ever saved, which bakes in absolute paths)."""
    try:
        work_dir = task["work_dir"]
        os.makedirs(work_dir, exist_ok=True)
        base = f"{task['entity'].replace('/', '_')}_{task['step']}"
        existing = [f for f in os.listdir(work_dir)
                    if f.startswith(base) and f.endswith(".blend")]
        version = len(existing) + 1
        path = os.path.join(work_dir, f"{base}_v{version:03d}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] work save failed:", exc)
        return None
    stale = _absolute_externals()
    if stale:
        try:
            bpy.ops.file.make_paths_relative()
            bpy.ops.wm.save_mainfile()
            print(f"[Flumen] work save: made {len(stale)} absolute path(s) "
                  f"relative (cross-machine safety).")
        except Exception as exc:  # noqa: BLE001 — the versioned save stands
            print("[Flumen] work save: could not relativize paths:", exc)
    return path


def publish_locator_name():
    """Name of the locator that marks what to publish (from project settings,
    default 'PUBLISH')."""
    try:
        root = settings_io.find_project_root(_pref_local_root())
        data = settings_io.load_settings(root)
        return (data.get("publish") or {}).get("locator") or "PUBLISH"
    except Exception:  # noqa: BLE001
        return "PUBLISH"


def _descendants(obj):
    out = []
    for child in obj.children:
        out.append(child)
        out.extend(_descendants(child))
    return out


def active_publish_locator():
    """The PUBLISH locator object in this file, or None."""
    return bpy.data.objects.get(publish_locator_name())


def _used_texture_images():
    """Image textures actually used (have users): plain files, UDIM tilesets, and
    sequences. These are the textures a surface look depends on."""
    return [img for img in bpy.data.images
            if getattr(img, "source", "") in ("FILE", "TILED", "SEQUENCE")
            and getattr(img, "users", 0) > 0]


def _image_src(img):
    """Absolute path to an image's source file ('//' resolved), or '' if none."""
    fp = getattr(img, "filepath_raw", "") or getattr(img, "filepath", "")
    return bpy.path.abspath(fp) if fp else ""


def _image_missing(img):
    """A used texture is 'missing' if it isn't packed and has no file on disk —
    publishing it would ship a dead path. UDIM tilesets check their first tile."""
    if getattr(img, "packed_file", None):
        return False
    src = _image_src(img)
    if not src:
        return True
    if getattr(img, "source", "") == "TILED":
        tiles = list(getattr(img, "tiles", []) or [])
        n = tiles[0].number if tiles else 1001
        return not os.path.isfile(src.replace("<UDIM>", str(n)))
    return not os.path.isfile(src)


def _texture_check_records():
    """Lightweight records for checks.check_surface (plain namespaces so checks.py
    stays bpy-free). Only the textures the publish would actually ship are checked —
    the materials on the meshes under the PUBLISH locator — NOT stray images left in
    the file (e.g. the loaded model's original texture refs, which aren't synced on
    every machine and would falsely block a publish)."""
    loc = bpy.data.objects.get(publish_locator_name())
    meshes = [o for o in (_descendants(loc) if loc else bpy.context.scene.objects)
              if getattr(o, "type", "") == "MESH"]
    materials = {s.material for o in meshes
                 for s in (getattr(o, "material_slots", []) or []) if s.material}
    # LOCAL images only: a linked material's images resolve relative to THEIR
    # library file, not this one — they were validated when their own publish
    # shipped, and checking them here would false-flag linked content.
    return [types.SimpleNamespace(name=img.name, is_missing=_image_missing(img))
            for img in _images_of_materials(materials)
            if getattr(img, "library", None) is None]


def _images_of_materials(materials):
    """The image textures actually referenced by these materials (walks node groups).
    Used so a look publishes ONLY its own maps — not every stray/duplicate image
    datablock that happens to be in the work file."""
    seen, imgs, stack = set(), [], []
    for mat in materials:
        if mat and getattr(mat, "use_nodes", False) and mat.node_tree:
            stack.append(mat.node_tree)
    visited = set()
    while stack:
        nt = stack.pop()
        if id(nt) in visited:
            continue
        visited.add(id(nt))
        for nd in nt.nodes:
            img = getattr(nd, "image", None)
            if (img is not None and img.name not in seen
                    and getattr(img, "source", "") in ("FILE", "TILED", "SEQUENCE")):
                seen.add(img.name)
                imgs.append(img)
            if getattr(nd, "type", "") == "GROUP" and getattr(nd, "node_tree", None):
                stack.append(nd.node_tree)
    return imgs


def _materialize_look_textures(textures_dir, materials):
    """Write the look's textures into textures_dir as external file(s) — UDIM tiles
    via the '<UDIM>' token, plain/packed images as single files — repointing each
    image there so a subsequent libraries.write(path_remap='RELATIVE') bakes a
    '//textures/…' path. Returns (written_paths, manifest_entries, restore), where
    restore() puts the artist's session images back (re-packing those that were
    packed) so publishing is non-destructive."""
    os.makedirs(textures_dir, exist_ok=True)
    originals = {}     # img -> (orig filepath_raw, was_packed, external target)
    entries, written, done = [], [], set()
    for img in _images_of_materials(materials):
        was_packed = bool(getattr(img, "packed_file", None))
        raw = getattr(img, "filepath_raw", "") or img.filepath
        ext = (os.path.splitext(raw)[1] or os.path.splitext(img.name)[1] or ".png")
        cs = getattr(getattr(img, "colorspace_settings", None), "name", "")
        tiled = getattr(img, "source", "") == "TILED"
        if tiled:
            target = os.path.join(textures_dir,
                                  f"{textures.udim_stem(img.name)}.<UDIM>{ext}")
            tiles = [t.number for t in img.tiles]
        else:
            target = os.path.join(textures_dir,
                                  f"{os.path.splitext(os.path.basename(img.name))[0]}{ext}")
            tiles = [None]

        if was_packed:
            # Packed: pixels are in memory — write them out, then drop the pack.
            img.filepath_raw = target
            _set_image_format(img, ext)
            img.save()
            try:
                img.unpack(method="REMOVE")
            except Exception:  # noqa: BLE001
                pass
            files = [target.replace("<UDIM>", str(t)) if t else target for t in tiles]
        else:
            # External (and possibly not loaded in headless): copy the source files
            # straight across — img.save() would fail with 'no image data'.
            src = bpy.path.abspath(raw)
            files = []
            for t in tiles:
                s = src.replace("<UDIM>", str(t)) if t else src
                d = target.replace("<UDIM>", str(t)) if t else target
                if os.path.isfile(s):
                    shutil.copy2(s, d)
                    files.append(d)
            img.filepath_raw = target        # repoint for the look .blend remap
            try:
                img.reload()                 # load from the copy so img.size is real
            except Exception:  # noqa: BLE001
                pass

        originals[img] = (raw, was_packed, target)
        w, h = (list(getattr(img, "size", [0, 0])) + [0, 0])[:2]
        for f in files:
            if os.path.isfile(f) and f not in done:
                done.add(f)
                written.append(f)
                entries.append(textures.texture_entry(
                    f, os.path.basename(f), w, h, cs, textures.sha1_file(f)))

    def restore():
        for img, (raw, was_packed, target) in originals.items():
            try:
                if was_packed:               # re-embed from the identical external
                    img.filepath_raw = target
                    img.pack()
                img.filepath_raw = raw       # and restore the original reference
            except Exception:  # noqa: BLE001
                pass

    return written, entries, restore


def _set_image_format(img, ext):
    fmt = textures.format_for_ext(ext)
    if fmt:
        try:
            img.file_format = fmt
        except Exception:  # noqa: BLE001
            pass


def _collect_look(context):
    """The materials to publish as a look + the mesh→material assignment map, taken
    from the geometry under the PUBLISH locator."""
    loc = bpy.data.objects.get(publish_locator_name())
    pool = (_descendants(loc) if loc is not None else list(context.scene.objects))
    meshes = [o for o in pool if getattr(o, "type", "") == "MESH"]
    amap = look_mod.assignment_map(meshes)
    materials = {s.material for o in meshes
                 for s in getattr(o, "material_slots", []) or [] if s.material}
    return materials, amap


def _profile_stats(context, heavy_modifiers):
    """Scene cost stats for the profiler (bpy side): polys/objects/textures/heavy
    modifiers. Poly counts are base-mesh (pre-modifier) — that's why unapplied
    heavy modifiers are flagged separately."""
    heavy_set = set(heavy_modifiers or [])
    poly_count = 0
    heavy = []
    objects = list(context.scene.objects)
    for o in objects:
        data = getattr(o, "data", None)
        if getattr(o, "type", "") == "MESH" and data is not None:
            try:
                poly_count += len(data.polygons)
            except Exception:  # noqa: BLE001
                pass
        for m in getattr(o, "modifiers", []) or []:
            if getattr(m, "type", "") in heavy_set:
                heavy.append((o.name, m.type))
    textures = []
    for img in _used_texture_images():
        size = list(getattr(img, "size", None) or (0, 0))
        textures.append({"name": getattr(img, "name", "?"),
                         "width": int(size[0]), "height": int(size[1]),
                         "channels": int(getattr(img, "channels", 4) or 4),
                         "is_float": bool(getattr(img, "is_float", False))})
    return {"poly_count": poly_count, "object_count": len(objects),
            "textures": textures, "heavy_modifiers": heavy}


def _run_task_checks(step, context, ttype=None, entity=""):
    """run_checks with surface texture state injected for the surface step, the
    task type so shot publishes get the shot gate (camera + frame range), and —
    for profiled categories/steps (environments) — the WARN-only cost profile."""
    # Missing-texture gate for every ASSET step (model/surface/rig/dressing…):
    # a dead texture path in the work file becomes a dead publish — or a failed
    # save outright when the file has auto-pack enabled. Shots are exempt (they
    # are assembled from linked publishes, checked at their own publish time).
    extra = _texture_check_records() if ttype != "shot" else None
    profile_stats, profiling = None, None
    if ttype == "asset" and entity:
        root = settings_io.find_project_root(_pref_local_root())
        settings = settings_io.load_settings(root) if root else {}
        profiling = checks.profile_thresholds(settings)
        if (entity.split("/")[0] in (profiling.get("apply_to_categories") or [])
                and step in (profiling.get("apply_to_steps") or [])):
            profile_stats = _profile_stats(
                context, profiling.get("heavy_modifiers") or [])
    return checks.run_checks(step, context.scene, list(context.scene.objects),
                             publish_locator_name(), textures=extra, ttype=ttype,
                             profile_stats=profile_stats, profiling=profiling,
                             collections=list(bpy.data.collections))


class FLUMEN_OT_turntable_framing(bpy.types.Operator):
    bl_idname = "flumen.turntable_framing"
    bl_label = "Turntable Framing"
    bl_description = ("Set this asset's turntable scale/fit. Stored on the PUBLISH "
                      "locator, so it travels with the publish — per character, not global")

    override: bpy.props.BoolProperty(
        name="Override project default", default=False,
        description="Use this asset's own framing instead of the project setting")
    fit_mode: bpy.props.EnumProperty(
        name="Fit", default="box",
        items=[("box", "Box — fit whole bounding box", "Scale the whole bbox to fit"),
               ("height", "Height — fill vertically", "Fill the frame top-to-bottom"),
               ("width", "Width — fit widest horizontal", "Fit the widest horizontal extent")])
    fit_scale: bpy.props.FloatProperty(
        name="Zoom", default=1.0, min=0.05, max=5.0, soft_min=0.2, soft_max=2.0,
        description="<1 = smaller / more margin, >1 = bigger")

    def invoke(self, context, event):
        loc = active_publish_locator()
        if not loc:
            self.report({"ERROR"}, "Add a Publish Locator first (Flumen ▸ Add Publish Locator).")
            return {"CANCELLED"}
        self.override = bool(loc.get("flumen_tt_override", 0))
        m = loc.get("flumen_tt_fit_mode")
        if m in ("box", "height", "width"):
            self.fit_mode = m
        sc = loc.get("flumen_tt_fit_scale")
        if sc is not None:
            self.fit_scale = float(sc)
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "override")
        sub = col.column()
        sub.enabled = self.override
        sub.prop(self, "fit_mode")
        sub.prop(self, "fit_scale", slider=True)
        col.separator()
        col.label(text="Saved on the PUBLISH locator — per character.", icon="INFO")

    def execute(self, context):
        loc = active_publish_locator()
        if not loc:
            self.report({"ERROR"}, "No Publish Locator.")
            return {"CANCELLED"}
        loc["flumen_tt_override"] = 1 if self.override else 0
        loc["flumen_tt_fit_mode"] = self.fit_mode
        loc["flumen_tt_fit_scale"] = float(self.fit_scale)
        state = (f"{self.fit_mode} @ {self.fit_scale:.2f}x" if self.override
                 else "project default")
        self.report({"INFO"}, f"Turntable framing → {state} (on {loc.name}).")
        return {"FINISHED"}


class FLUMEN_OT_add_locator(bpy.types.Operator):
    bl_idname = "flumen.add_publish_locator"
    bl_label = "Add Publish Locator"
    bl_description = ("Create the locator empty that marks what gets published — "
                      "parent your asset geometry under it")

    def execute(self, context):
        name = publish_locator_name()
        if bpy.data.objects.get(name):
            self.report({"INFO"}, f"'{name}' already exists.")
            return {"FINISHED"}
        empty = bpy.data.objects.new(name, None)
        empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = 0.5
        context.scene.collection.objects.link(empty)
        self.report({"INFO"}, f"Created '{name}'. Parent your asset geometry under it.")
        return {"FINISHED"}


class FLUMEN_OT_add_publish_collection(bpy.types.Operator):
    bl_idname = "flumen.add_publish_collection"
    bl_label = "Add Publish Collection"
    bl_description = ("Create the PUBLISH collection that marks what gets "
                      "published — move your environment's collections inside "
                      "it. A scene using the old PUBLISH locator empty is "
                      "converted: its objects move into the collection and the "
                      "empty is removed")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        name = publish_locator_name()
        if bpy.data.collections.get(name) is not None:
            self.report({"INFO"}, f"'{name}' collection already exists.")
            return {"FINISHED"}
        coll = bpy.data.collections.new(name)
        context.scene.collection.children.link(coll)
        loc = bpy.data.objects.get(name)
        if loc is not None:
            # Convert the locator form: the empty's subtree moves into the
            # collection (world transforms preserved), the empty goes away.
            subtree = _descendants(loc)
            for o in subtree:
                for c in list(o.users_collection):
                    try:
                        c.objects.unlink(o)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    coll.objects.link(o)
                except Exception:  # noqa: BLE001
                    pass
            for o in [c for c in subtree if c.parent is loc]:
                w = o.matrix_world.copy()
                o.parent = None
                o.matrix_world = w
            try:
                bpy.data.objects.remove(loc, do_unlink=True)
            except Exception:  # noqa: BLE001
                pass
            self.report({"INFO"},
                        f"Converted the '{name}' locator into the '{name}' "
                        f"collection ({len(subtree)} object(s) moved) — you can "
                        f"now organize them into collections inside it.")
            return {"FINISHED"}
        self.report({"INFO"}, f"Created the '{name}' collection — move the "
                              f"collections you want published inside it.")
        return {"FINISHED"}


def _server_next_version(task_id: str, base: str) -> int | None:
    """Authoritative next version from the task's server publish history (via the
    toolkit). None if the toolkit/server isn't reachable, so we fall back to local."""
    cmd, td = _toolkit_cmd(["next-version", "--task", task_id, "--base", base])
    if cmd is None:
        _publog("next-version: toolkit not available — launch Blender from "
                "the Workspace app")
        return None
    try:
        p = subprocess.run(cmd, cwd=td, text=True, capture_output=True,
                           **_no_window())
        if p.returncode != 0:
            _publog(f"next-version failed (rc {p.returncode}): "
                    f"{(p.stderr or p.stdout or '').strip()}")
            return None
        return int((p.stdout or "").strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        _publog(f"next-version failed: {exc}")
        return None


def _export_fbx(filepath: str, use_selection: bool = False) -> bool:
    """Export a Maya-friendly FBX (Y-up, baked transforms, meters)."""
    try:
        bpy.ops.export_scene.fbx(
            filepath=filepath, use_selection=use_selection,
            object_types={"MESH", "EMPTY", "ARMATURE"},
            apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL",
            bake_space_transform=True, axis_forward="-Z", axis_up="Y",
            mesh_smooth_type="FACE", path_mode="AUTO")
        return True
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] FBX export failed:", exc)
        return False


def _draw_checks(layout, issues):
    box = layout.box()
    box.label(text="Sanity checks:")
    if not issues:
        box.label(text="All checks passed.", icon="CHECKMARK")
        return
    for level, msg in issues:
        box.label(text=msg, icon="ERROR" if level == checks.ERROR else "INFO")


class FLUMEN_OT_check(bpy.types.Operator):
    bl_idname = "flumen.run_checks"
    bl_label = "Run Sanity Checks"
    bl_description = "Run the pre-publish sanity checks for this task and show issues"

    _issues: list = []

    def invoke(self, context, event):
        task = active_task()
        step = task["step"] if task else ""
        self._issues = _run_task_checks(step, context,
                                        (task or {}).get("type"),
                                        (task or {}).get("entity", ""))
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        _draw_checks(self.layout, self._issues)
        if checks.has_errors(self._issues):
            self.layout.label(text="Errors would block a publish.", icon="CANCEL")
        fixable, shared, _anim = checks.fixable_scale_objects(context.scene.objects)
        if fixable or not _units_ok(context.scene):
            self.layout.separator()
            self.layout.operator("flumen.auto_fix", icon="TOOL_SETTINGS")
        elif shared:
            self.layout.label(text=f"{len(shared)} scale issue(s) are on shared "
                                   f"meshes — not auto-fixable.", icon="INFO")

    def execute(self, context):
        return {"FINISHED"}  # informational only


def _units_ok(scene):
    us = getattr(scene, "unit_settings", None)
    return (us is not None and getattr(us, "system", "") == "METRIC"
            and abs(float(getattr(us, "scale_length", 1.0)) - 1.0) <= 1e-6)


class FLUMEN_OT_test_connection(bpy.types.Operator):
    bl_idname = "flumen.test_connection"
    bl_label = "Test server connection"
    bl_description = ("Verify the server login and project folder that publishes "
                      "upload to — run this first when a publish or turntable "
                      "never arrives on the server")

    def execute(self, context):
        self.report({"INFO"}, "Testing the server connection…")
        ok, note = _preflight_server()
        _publog(f"test connection: {'OK' if ok else 'FAILED'} — {note}")
        icon = "CHECKMARK" if ok else "ERROR"
        title = "Server connection OK" if ok else "Server connection FAILED"
        import textwrap
        lines = textwrap.wrap(note, 64)
        if not ok:
            lines += ["", f"Details: {PUBLISH_LOG}"]

        def _draw(popup, _ctx):
            for chunk in lines:
                popup.layout.label(text=chunk)
        context.window_manager.popup_menu(_draw, title=title, icon=icon)
        return {"FINISHED"} if ok else {"CANCELLED"}


class FLUMEN_OT_show_log(bpy.types.Operator):
    bl_idname = "flumen.show_log"
    bl_label = "Show pipeline log"
    bl_description = ("Load the tails of ~/.flumen/publish.log (publish/upload "
                      "trace) and ~/.flumen/blender.log (this Blender's console "
                      "output) into Blender's Text Editor. Run again to refresh")
    _TEXT_NAME = "pipeline logs (tail)"
    _LINES = 400

    def execute(self, context):
        blender_log = os.path.join(os.path.expanduser("~"), ".flumen",
                                   "blender.log")
        sections = []
        for path in (PUBLISH_LOG, blender_log):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    tail = "".join(fh.readlines()[-self._LINES:])
                sections.append(f"#### {path} — last {self._LINES} lines\n\n"
                                + tail)
            except OSError:
                continue
        if not sections:
            self.report({"WARNING"}, f"No logs yet under ~/.flumen — publish "
                                     f"once, or launch Blender via the "
                                     f"Workspace app to capture output.")
            return {"CANCELLED"}
        txt = bpy.data.texts.get(self._TEXT_NAME)
        if txt is None:
            txt = bpy.data.texts.new(self._TEXT_NAME)
        txt.clear()
        txt.write("# Flumen menu > Show pipeline log to refresh\n\n"
                  + "\n\n".join(sections))
        txt.cursor_set(max(0, len(txt.lines) - 1))   # jump to the end (newest)
        # Show it: reuse an open Text Editor if there is one.
        shown = False
        for area in context.screen.areas:
            if area.type == "TEXT_EDITOR":
                area.spaces.active.text = txt
                shown = True
                break
        self.report({"INFO"}, "Log loaded" + ("" if shown else
                    f" into the Text Editor datablock '{self._TEXT_NAME}' — "
                    f"switch any editor to Text Editor to read it"))
        return {"FINISHED"}


class FLUMEN_OT_auto_fix(bpy.types.Operator):
    bl_idname = "flumen.auto_fix"
    bl_label = "Auto-fix issues"
    bl_description = ("Fix what the current step's checks actually flag: metric "
                      "units everywhere; unapplied scales on model tasks. Skips "
                      "shared-mesh instances (fixing one would deform the "
                      "others), keyframed objects and linked/override elements")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        did = []
        # 1) Units — checked on every step, two values, zero risk.
        if not _units_ok(context.scene):
            context.scene.unit_settings.system = "METRIC"
            context.scene.unit_settings.scale_length = 1.0
            did.append("units -> metric/1.0")
        # 2) Unapplied scales — only where the checks flag them: the model step
        # (or a taskless session). Shot/dressing scenes are linked content the
        # fixer can't (and shouldn't) touch; other asset steps don't check scale.
        task = active_task()
        if task and task.get("step") != "model":
            msg = ("Fixed: " + "; ".join(did) if did
                   else "Nothing to fix on this step (scale fixes apply on "
                        "model tasks).")
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        fixable, shared, animated, linked = checks.fixable_scale_objects(
            context.scene.objects)
        applied = failed = 0
        if fixable:
            try:
                with context.temp_override(
                        selected_editable_objects=list(fixable),
                        active_object=fixable[0]):
                    bpy.ops.object.transform_apply(
                        location=False, rotation=False, scale=True)
                applied = len(fixable)
            except Exception:  # noqa: BLE001 — fall back to one-by-one
                for o in fixable:
                    try:
                        with context.temp_override(
                                selected_editable_objects=[o], active_object=o):
                            bpy.ops.object.transform_apply(
                                location=False, rotation=False, scale=True)
                        applied += 1
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        print(f"[Flumen] auto-fix: could not apply scale on "
                              f"{o.name}: {exc}")
        if applied:
            did.append(f"applied scale on {applied} mesh(es)")
        skipped = []
        if shared:
            names = ", ".join(o.name for o in shared[:3])
            skipped.append(f"{len(shared)} shared-mesh ({names}"
                           + ("…" if len(shared) > 3 else "") + ")")
            print("[Flumen] auto-fix skipped (shared mesh data — applying would "
                  "deform the other instances): "
                  + ", ".join(o.name for o in shared))
        if animated:
            skipped.append(f"{len(animated)} keyframed")
            print("[Flumen] auto-fix skipped (keyframed): "
                  + ", ".join(o.name for o in animated))
        if linked:
            skipped.append(f"{len(linked)} linked")
            print("[Flumen] auto-fix skipped (linked/override — fix in the "
                  "source asset file): " + ", ".join(o.name for o in linked))
        if failed:
            skipped.append(f"{failed} failed")
        msg = ("Fixed: " + "; ".join(did) if did else "Nothing left to fix")
        if skipped:
            msg += "  — skipped: " + ", ".join(skipped) + " [full list in blender.log]"
        self.report({"INFO"}, msg)
        return {"FINISHED"}


def _wrap_publish_in_collection(context, coll_name, loc):
    """Move the PUBLISH subtree into a fresh collection named `coll_name` so the
    saved publish .blend contains ONE linkable collection (downstream shots link the
    rig/model by this collection name to get clean library overrides). Returns a
    restore() callable that puts the objects back and removes the temp collection —
    call it after the copy is written so the artist's working session is untouched."""
    if loc is None:
        return lambda: None
    objs = [loc, *_descendants(loc)]
    prior = {o: list(o.users_collection) for o in objs}   # restore exactly
    # The wrap MUST get exactly `coll_name` — downstream linking and the clean
    # post-process both find it by name. If the artist already has a collection
    # with the asset's name (common: character in a 'panda' collection), the new
    # one would silently become 'panda.001' and the publish would go out empty.
    clash = bpy.data.collections.get(coll_name)
    if clash is not None:
        try:
            clash.name = coll_name + ".work"
        except Exception:  # noqa: BLE001 — library-linked: read-only name
            clash = None
    coll = bpy.data.collections.new(coll_name)
    if coll.name != coll_name:
        print(f"[Flumen] publish wrap could not claim the name '{coll_name}' "
              f"(got '{coll.name}') — a linked collection may own it.")
    context.scene.collection.children.link(coll)
    for o in objs:
        for c in prior[o]:
            try:
                c.objects.unlink(o)
            except Exception:  # noqa: BLE001
                pass
        try:
            coll.objects.link(o)
        except Exception:  # noqa: BLE001
            pass

    def restore():
        for o in objs:
            try:
                coll.objects.unlink(o)
            except Exception:  # noqa: BLE001
                pass
            for c in prior[o]:
                try:
                    c.objects.link(o)
                except Exception:  # noqa: BLE001
                    pass
        try:
            context.scene.collection.children.unlink(coll)
        except Exception:  # noqa: BLE001
            pass
        try:
            bpy.data.collections.remove(coll)
        except Exception:  # noqa: BLE001
            pass
        if clash is not None:
            try:
                clash.name = coll_name
            except Exception:  # noqa: BLE001
                pass

    return restore


def _rename_publish_collection(coll, coll_name):
    """The collection form of the publish root (environments): the artist keeps
    a 'PUBLISH' COLLECTION holding the set's collections. For the saved copy we
    just rename it to the asset name — the linkable unit downstream expects —
    and rename it back after. No reparenting at all. Returns restore()."""
    original = coll.name
    clash = bpy.data.collections.get(coll_name)
    if clash is not None and clash is not coll:
        try:
            clash.name = coll_name + ".work"
        except Exception:  # noqa: BLE001 — library-linked: read-only name
            clash = None
    else:
        clash = None
    coll.name = coll_name
    if coll.name != coll_name:
        print(f"[Flumen] publish collection could not claim the name "
              f"'{coll_name}' (got '{coll.name}') — a linked collection may "
              f"own it.")

    def restore():
        try:
            coll.name = original
        except Exception:  # noqa: BLE001
            pass
        if clash is not None:
            try:
                clash.name = coll_name
            except Exception:  # noqa: BLE001
                pass

    return restore


# Stash between the shot publish dialog's invoke() and execute(): the current
# per-element hashes + the newest published anim version label.
_SHOT_PUBLISH = {}


def _prepare_shot_publish_anim(context, task):
    """Snapshot poses, hash each element's animation, compare to the last publish, and
    populate the publish dialog's per-element checkable list (changed/new pre-checked,
    unchanged unchecked). Also gathers each element's source step + newest published
    anim version so execute() can stamp the holders for the playblast HUD."""
    global _SHOT_PUBLISH
    _snapshot_poses(context)
    cur = _element_anim_hashes()

    # Last published hashes + the newest anim version per element (for dedup + the HUD).
    anims = _shell_json(["list-animations", "--task", task["id"], "--no-fetch"]) or []
    last, anim_vers = {}, {}
    for a in anims:                         # newest first
        for eid, h in (a.get("hashes") or {}).items():
            last.setdefault(eid, h)
        for eid in (a.get("elements") or {}):
            anim_vers.setdefault(eid, a.get("version", ""))
    last_label = anims[0]["version"] if anims else ""

    # Each element's source step (rig/model/camera), from the assembly resolution.
    steps = {}
    res = _shell_json(["resolve-assembly", "--task", task["id"], "--list"]) or {}
    for el in res.get("elements", []):
        steps[el["id"]] = ("camera" if el.get("kind") == "camera"
                           else el.get("source_step", ""))

    rows = context.window_manager.flumen_publish_items
    rows.clear()
    for eid in sorted(cur):
        it = rows.add()
        it.element_id = eid
        it.label = eid
        if eid not in last:
            it.status = "new"
        elif last[eid] != cur[eid]:
            it.status = "changed"
        else:
            it.status = "unchanged"
        it.ref = last_label if it.status == "unchanged" else ""
        it.enabled = it.status in ("new", "changed")
    _SHOT_PUBLISH = {"hashes": cur, "last_label": last_label,
                     "steps": steps, "anim_vers": anim_vers}


class FLUMEN_PublishItem(bpy.types.PropertyGroup):
    """One row in the shot publish dialog: an animated element + whether to publish
    its animation this version."""
    enabled: bpy.props.BoolProperty(name="Publish", default=True)
    element_id: bpy.props.StringProperty()
    label: bpy.props.StringProperty()
    status: bpy.props.StringProperty()      # changed | unchanged | new
    ref: bpy.props.StringProperty()         # the version it's unchanged against


class FLUMEN_OT_publish(bpy.types.Operator):
    bl_idname = "flumen.publish"
    bl_label = "Publish"
    bl_description = ("Run sanity checks, then write a versioned .blend + FBX into "
                      "this task's publish/ folder, upload, and set status to Review")

    _issues: list = []
    _server_note: str = ""

    def invoke(self, context, event):
        task = active_task()
        if not task or not task["work_dir"]:
            self.report({"ERROR"}, "No active task. Open this scene from the "
                                   "Workspace app's 'Open in Blender'.")
            return {"CANCELLED"}
        # Preflight the server BEFORE anything else: a publish that can't
        # upload should fail loudly now, not silently after the files are
        # written. Also catches wrong credentials / missing remote_root.
        ok, note = _preflight_server()
        self._server_note = note
        if not ok:
            _publog(f"publish blocked by preflight: {note}")
            self.report({"ERROR"}, f"Publish blocked — {note}  "
                                   f"(details: {PUBLISH_LOG})")
            return {"CANCELLED"}
        self._issues = _run_task_checks(task["step"], context, task.get("type"),
                                        task.get("entity", ""))
        if task.get("step") == "surface":
            global _EXISTING_LOOKS
            _EXISTING_LOOKS = _fetch_existing_looks(task["id"])
        if task.get("step") == "dressing":
            global _EXISTING_DRESSINGS
            _EXISTING_DRESSINGS = _fetch_existing_dressings(task["id"])
        if task.get("type") == "shot":
            _prepare_shot_publish_anim(context, task)
            context.window_manager.flumen_force_publish = False
        return context.window_manager.invoke_props_dialog(
            self, width=480, title="Publish", confirm_text="Publish")

    def draw(self, context):
        col = self.layout.column()
        col.prop(context.window_manager, "flumen_publish_desc", text="Description")
        task = active_task()
        is_env = (task or {}).get("entity", "").startswith("environments/")
        if task and task.get("step") == "model" and not is_env:
            col.prop(context.window_manager, "flumen_render_turntable")
        if task and task.get("step") == "model":
            col.prop(context.window_manager, "flumen_apply_modifiers")
        if task and task.get("step") == "surface":
            wm = context.window_manager
            col.prop(wm, "flumen_look_name", text="Look name")
            col.prop(wm, "flumen_render_turntable", text="Render look review")
        if task and task.get("step") == "dressing":
            col.prop(context.window_manager, "flumen_dressing_name",
                     text="Dressing name")
        if task and task.get("type") == "shot":
            rows = context.window_manager.flumen_publish_items
            if len(rows):
                box = col.box()
                box.label(text="Animation to publish (changed are pre-selected):")
                for it in rows:
                    row = box.row(align=True)
                    row.prop(it, "enabled", text="")
                    row.label(text=it.label, icon="ARMATURE_DATA")
                    tag = (f"unchanged (= {it.ref})" if it.status == "unchanged"
                           else it.status)
                    row.label(text=tag)
                col.prop(context.window_manager, "flumen_force_publish")
            col.prop(context.window_manager, "flumen_render_turntable",
                     text="Render playblast")
        col.separator()
        _draw_checks(col, self._issues)
        col.separator()
        if self._server_note:
            col.label(text=self._server_note, icon="URL")
        if checks.has_errors(self._issues):
            col.label(text="Errors must be fixed — publish is blocked.", icon="CANCEL")
        else:
            col.label(text="Ready to publish.", icon="CHECKMARK")

    def execute(self, context):
        task = active_task()
        if not task or not task["work_dir"]:
            self.report({"ERROR"}, "No active task.")
            return {"CANCELLED"}
        _publog(f"publish: task {task['id']} step {task.get('step')} "
                f"type {task.get('type')}", echo=False)

        # FIRST, before any publish scripting touches the scene: snapshot the
        # artist's session AS-IS into the work folder. Every publish then has
        # a work file at least as new as itself — publishing can never be the
        # only copy of two hours of work.
        work_saved = _save_work_version(task)
        if not work_saved:
            self.report({"WARNING"}, "Could not save a work version first — "
                                     "publishing anyway (see the pipeline log).")

        issues = _run_task_checks(task["step"], context, task.get("type"),
                                  task.get("entity", ""))
        if checks.has_errors(issues):
            errs = [m for lvl, m in issues if lvl == checks.ERROR]
            self.report({"ERROR"}, "Publish blocked: " + errs[0])
            print("[Flumen] publish blocked:\n  " + "\n  ".join(errs))
            return {"CANCELLED"}

        publish_dir = os.path.join(os.path.dirname(task["work_dir"]), "publish")
        os.makedirs(publish_dir, exist_ok=True)
        name = task["entity"].split("/")[-1]
        # Surface publishes a named look, dressing a named prop layout — each
        # versioned on its own track; other steps version by step.
        look_name = ""
        dressing_name = ""
        post_cmd = None
        if task["step"] == "surface":
            look_name = look_mod.normalize_look_name(
                context.window_manager.flumen_look_name)
            base = look_mod.look_base(name, look_name)
        elif task["step"] == "dressing":
            dressing_name = dressing_mod.normalize_dressing_name(
                context.window_manager.flumen_dressing_name)
            base = f"{name}_dressing_{dressing_name}"
        else:
            base = f"{name}_{task['step']}"
        # The server publish history is the single source of truth for versions.
        # If we can't reach it, abort rather than guess a number that could collide.
        version = _server_next_version(task["id"], base)
        if not version:
            self.report({"ERROR"}, "Couldn't reach the server to determine the next "
                        f"version — publish aborted. Check your connection and "
                        f"retry (details: {PUBLISH_LOG}).")
            return {"CANCELLED"}
        pub_path = os.path.join(publish_dir, f"{base}_v{version:03d}.blend")
        _publog(f"publish: {base}_v{version:03d} -> {pub_path}", echo=False)

        texture_files = []
        if task["step"] == "surface":
            # A look = the materials only (no geometry) + an assignment map + safe
            # external textures, so downstream can re-apply it onto the character.
            materials, amap = _collect_look(context)
            textures_dir = os.path.join(publish_dir, "textures",
                                        f"{base}_v{version:03d}")
            written, tex_entries, restore = _materialize_look_textures(
                textures_dir, materials)
            try:
                # Write ONLY the materials; RELATIVE_ALL forces every texture path
                # relative to the look .blend ('//textures/…') so it resolves on any
                # machine. (Plain 'RELATIVE' only remaps already-relative paths and
                # would leave our absolute publish paths absolute — dead on Windows.)
                bpy.data.libraries.write(pub_path, materials,
                                         path_remap="RELATIVE_ALL", fake_user=True)
            finally:
                restore()      # leave the artist's working session untouched
            manifest = look_mod.build_look_manifest(
                look_name, version, amap, tex_entries)
            manifest_path = pub_path[:-6] + ".manifest.json"
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh, indent=2)
            files = [pub_path, manifest_path]
            texture_files = written
            kind = f"look '{look_name}': {len(materials)} material(s), " \
                   f"{len(written)} texture file(s)"
        elif task["step"] == "dressing":
            # A dressing = the instance manifest (env + prop placements referencing
            # published assets) + the working scene. Anything the artist modeled
            # LOCALLY in the scene (quick props, kitbash, shaded inline) becomes
            # the dressing's "extras": gathered into a collection inside the
            # published .blend so Build shot can link it — no pre-publish needed.
            env = dressing_mod.collect_environment(bpy.data.collections)
            if not env or not env.get("asset"):
                self.report({"ERROR"}, "No environment loaded — run 'Load "
                                       "environment' first.")
                return {"CANCELLED"}
            props = dressing_mod.collect_prop_instances(bpy.data.objects)
            unmanaged = dressing_mod.unmanaged_prop_holders(
                bpy.data.collections, bpy.data.objects)
            if unmanaged:
                self.report({"WARNING"},
                            f"{len(unmanaged)} prop holder(s) without a prop_root "
                            f"empty won't be in the manifest (use Add prop): "
                            + ", ".join(unmanaged[:3]))
            extras = dressing_mod.collect_local_extras(bpy.data.objects)
            extras_coll_name = f"{base}_extras" if extras else ""
            restore_review = _unlink_review_camera(context)
            extras_coll = None
            try:
                if extras:
                    # Collections may hold an object many times over — an ADD
                    # link is enough; the artist's layout is untouched and the
                    # temp collection is removed after the copy is written.
                    extras_coll = bpy.data.collections.new(extras_coll_name)
                    context.scene.collection.children.link(extras_coll)
                    for o in extras:
                        try:
                            extras_coll.objects.link(o)
                        except Exception:  # noqa: BLE001
                            pass
                try:
                    bpy.ops.file.make_paths_relative()
                except Exception:  # noqa: BLE001
                    pass
                bpy.ops.wm.save_as_mainfile(filepath=pub_path, copy=True)
            finally:
                restore_review()
                if extras_coll is not None:
                    try:
                        context.scene.collection.children.unlink(extras_coll)
                        bpy.data.collections.remove(extras_coll)
                    except Exception:  # noqa: BLE001
                        pass
            workfile_rel = _project_rel(pub_path)
            manifest = {
                "dressing": dressing_name, "version": version,
                "environment": env, "workfile_rel": workfile_rel,
                "props": props,
            }
            if extras:
                manifest["extras"] = {"collection": extras_coll_name,
                                      "count": len(extras)}
                kind_extras = f" + {len(extras)} local extra(s)"
            else:
                kind_extras = ""
            # Local extras may carry inline shading — normalize their textures
            # into the sidecar folder (headless pass, no scene stripping).
            post_script = os.path.join(os.path.dirname(__file__),
                                       "blender_publish_post.py")
            post_cmd = [bpy.app.binary_path, "-b", pub_path,
                        "--python", post_script, "--", "--textures-only"]
            manifest_path = pub_path[:-6] + ".manifest.json"
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh, indent=2)
            files = [pub_path, manifest_path]
            kind = f"dressing '{dressing_name}': {len(props)} prop(s){kind_extras}"
        elif task.get("type") == "shot":
            # Publish only the elements the artist checked in the dialog (changed/new
            # are pre-checked). If there are animated elements but none are selected
            # (nothing changed) -> block: no new version, no duplicate data. "Force
            # publish" overrides — camera/layout tweaks and playblast re-renders
            # deserve a new version even with identical animation.
            rows = context.window_manager.flumen_publish_items
            chosen = {it.element_id for it in rows if it.enabled}
            if len(rows) and not chosen \
                    and not context.window_manager.flumen_force_publish:
                last = _SHOT_PUBLISH.get("last_label", "")
                self.report({"ERROR"}, "No animation changes"
                            + (f" since {last}" if last else "")
                            + " — nothing to publish (or tick Force publish).")
                return {"CANCELLED"}
            # Stamp every element holder for the playblast HUD: its step (rig/model/
            # camera) and the anim version playing — the newest published version, or
            # THIS version for the elements being published now. Done here (not just at
            # Build shot) so it's complete regardless of when the shot was assembled.
            steps = _SHOT_PUBLISH.get("steps", {})
            anim_vers = _SHOT_PUBLISH.get("anim_vers", {})
            this_ver = f"v{version:03d}"
            for coll in bpy.data.collections:
                if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
                    continue
                eid = coll.name[len(ELEMENT_HOLDER_PREFIX):]
                if steps.get(eid):
                    coll["flumen_step"] = steps[eid]
                if eid in chosen:
                    coll["flumen_anim"] = this_ver
                elif anim_vers.get(eid):
                    coll["flumen_anim"] = anim_vers[eid]
            # Save the assembled scene (linked rigs + camera + animation) as the
            # versioned publish — no collection wrap, no FBX.
            restore_review = _unlink_review_camera(context)
            try:
                try:
                    bpy.ops.file.make_paths_relative()
                except Exception:  # noqa: BLE001
                    pass
                bpy.ops.wm.save_as_mainfile(filepath=pub_path, copy=True)
            finally:
                restore_review()
            files = [pub_path]
            kind = ".blend (shot)"
            # Publish only the CHOSEN elements' animation as editable Actions + a
            # manifest (with content hashes for dedup), in publish/anim/ so it's never
            # an openable workfile. Rides texture_files (preserves the subpath).
            actions, elem_actions = (_collect_element_animation(only_ids=chosen)
                                     if chosen else (set(), {}))
            if actions:
                anim_dir = os.path.join(publish_dir, "anim")
                os.makedirs(anim_dir, exist_ok=True)
                anim_path = os.path.join(anim_dir, f"{base}_v{version:03d}_anim.blend")
                bpy.data.libraries.write(anim_path, actions, fake_user=True)
                hashes = _SHOT_PUBLISH.get("hashes") or _element_anim_hashes()
                # Which publish each element linked at capture time — consumers
                # use it to refuse stale object-placement keys after a model
                # restructure (renamed pieces would take the wrong keys).
                contents = {}
                for eid in elem_actions:
                    h = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid)
                    if h is not None:
                        contents[eid] = _element_loaded_file(h)
                manifest = anim_mod.build_anim_manifest(version, elem_actions,
                                                        hashes, contents)
                anim_manifest_path = anim_mod.anim_manifest_path(anim_path)
                with open(anim_manifest_path, "w") as fh:
                    json.dump(manifest, fh, indent=2)
                texture_files += [anim_path, anim_manifest_path]
                kind += f" + anim ({len(actions)} action(s))"
        else:
            # Wrap the publish root in a collection named after the asset so a
            # downstream shot can LINK it as one unit (clean library overrides), and
            # relativize texture paths so a linked rig/model resolves its maps on any
            # machine (the same absolute-path bug fixed for look textures). We mutate
            # the live session only to write the copy, then restore it.
            # Two root forms (checks accept both): the PUBLISH empty with the
            # asset parented under it, or a PUBLISH collection holding the
            # asset's collections (environments). Empty wins when both exist.
            loc = bpy.data.objects.get(publish_locator_name())
            pub_coll = (bpy.data.collections.get(publish_locator_name())
                        if loc is None else None)
            if pub_coll is not None:
                restore_pub = _rename_publish_collection(pub_coll, name)
            else:
                restore_pub = _wrap_publish_in_collection(context, name, loc)
            try:
                try:
                    bpy.ops.file.make_paths_relative()
                except Exception:  # noqa: BLE001 — unsaved / cross-drive textures
                    pass
                # relative_remap (default True) re-bases '//' paths to pub_path.
                bpy.ops.wm.save_as_mainfile(filepath=pub_path, copy=True)
            finally:
                restore_pub()
            files = [pub_path]
            kind = ".blend"
            # FBX rides along for interchange — except rigs: Blender rigs
            # (bone shapes, drivers, constraints) don't survive FBX, and shots
            # consume the rig by LINKING the .blend anyway.
            if task["step"] != "rig":
                fbx_path = pub_path[:-6] + ".fbx"   # .blend -> .fbx
                # Export only the geometry under the publish root, if present.
                root_objs = ([loc, *_descendants(loc)] if loc
                             else list(pub_coll.all_objects) if pub_coll
                             else [])
                use_sel = False
                if root_objs:
                    try:
                        bpy.ops.object.mode_set(mode="OBJECT")
                    except Exception:  # noqa: BLE001
                        pass
                    bpy.ops.object.select_all(action="DESELECT")
                    for d in root_objs:
                        d.select_set(True)
                    use_sel = True
                if _export_fbx(fbx_path, use_selection=use_sel):
                    files.append(fbx_path)
                kind = ".blend + FBX"
            # Post-process the publish COPY headless: strip everything outside
            # the wrapped collection (clean file, not just a clean link target)
            # and optionally bake the modifier stack. Work file untouched.
            post_script = os.path.join(os.path.dirname(__file__),
                                       "blender_publish_post.py")
            post_cmd = [bpy.app.binary_path, "-b", pub_path,
                        "--python", post_script, "--", "--collection", name]
            if context.window_manager.flumen_apply_modifiers:
                post_cmd.append("--apply-modifiers")
                kind += ", modifiers baked"

        pub_args = ["publish", "--local", *files, "--task", task["id"],
                    "--status", "review",
                    "--description", context.window_manager.flumen_publish_desc]
        for t in texture_files:
            pub_args += ["--texture", t]
        if post_cmd:
            # The post-process extracts the publish's textures into a sidecar
            # folder (phase 0, before the upload phase reads it) — ship it,
            # skipping files the server already has.
            pub_args += ["--textures-dir",
                         os.path.join(publish_dir, "textures")]
        pub_cmd, td = _toolkit_cmd(pub_args)
        if pub_cmd is None:
            _publog("publish: toolkit not available — files saved locally, "
                    "nothing uploaded")
            self.report({"WARNING"},
                        f"Saved {len(files)} file(s) to publish/, but the toolkit "
                        f"wasn't found to upload — push via the Workspace app.")
            return {"FINISHED"}

        context.window_manager.flumen_publish_desc = ""  # reset for next publish

        # Hand the (slow) upload to a modal operator so Blender stays responsive
        # and shows a live progress bar instead of freezing. The post-upload
        # background render is kicked off when the upload finishes (see the modal
        # operator), preserving the previous ordering.
        warns = sum(1 for lvl, _ in issues if lvl == checks.WARNING)
        suffix = f" ({warns} warning(s))" if warns else ""
        _PENDING_UPLOAD.clear()
        _PENDING_UPLOAD.update({
            "cmd": pub_cmd, "cwd": td, "n_files": len(files),
            "post_cmd": post_cmd,
            "success": (f"Published {base}_v{version:03d} ({kind}); "
                        f"task → Review.{suffix}"
                        + (f"  Work saved: {os.path.basename(work_saved)}."
                           if work_saved else "")),
            # Turntables are asset-on-a-pedestal reviews — meaningless for a
            # whole environment, so env model publishes never render one.
            "render": (bool(context.window_manager.flumen_render_turntable)
                       and not (task["step"] == "model"
                                and task["entity"].startswith("environments/"))),
            "step": task.get("step"), "ttype": task.get("type"),
            "task_id": task["id"], "pub_path": pub_path, "look_name": look_name,
        })
        if task.get("step") == "surface":
            # WYSIWYG review: whatever the artist hid in the surface scene stays
            # hidden in the look turntable (which renders the PUBLISHED model
            # headless and would otherwise bring everything back).
            hidden = _scene_hidden_names(context)
            if hidden:
                _PENDING_UPLOAD["extra_args"] = ["--hide", "||".join(hidden)]
        bpy.ops.flumen.publish_upload('INVOKE_DEFAULT')
        return {"FINISHED"}


def _scene_hidden_names(context):
    """Mesh objects the artist hid in the working scene — by ANY toggle: the
    outliner eye (hide_get), the monitor (hide_viewport) or the camera
    (hide_render). Scene-only state that a headless review render can't see."""
    names = set()
    for o in context.scene.objects:
        if o.type != "MESH":
            continue
        try:
            eye = o.hide_get()
        except Exception:  # noqa: BLE001 — not in the active view layer
            eye = False
        if eye or o.hide_viewport or o.hide_render:
            names.add(o.name)
    return sorted(names)


# Handoff from FLUMEN_OT_publish to the modal uploader (the codebase's established
# pattern for passing rich data into an operator).
_PENDING_UPLOAD: dict = {}

_PROGRESS_PREFIX = "FLUMEN_PROGRESS"


def _parse_progress(line):
    """Parse a 'FLUMEN_PROGRESS <pct> <eta> <msg>' line -> (pct, eta|None, msg),
    or None. Mirrors flumen.progress (the toolkit runs in a separate Python, so
    the add-on can't import it)."""
    if not line or not line.startswith(_PROGRESS_PREFIX):
        return None
    rest = line[len(_PROGRESS_PREFIX):].strip().split(" ", 2)
    try:
        pct = int(rest[0])
    except (IndexError, ValueError):
        return None
    eta = None
    if len(rest) > 1 and rest[1]:
        try:
            eta = float(rest[1])
        except ValueError:
            eta = None
    return pct, eta, (rest[2] if len(rest) > 2 else "")


def _human_eta(eta):
    if eta is None:
        return ""
    return f"~{int(eta)}s left" if eta < 90 else f"~{int(round(eta / 60))}m left"


class FLUMEN_OT_publish_upload(bpy.types.Operator):
    """Run the publish upload — then the review render (turntable/look/playblast) —
    as background subprocesses, showing a live progress bar (Blender's progress
    cursor + a status-bar message with %, ETA) for BOTH phases, so the UI never
    freezes and the artist always sees what's happening."""
    bl_idname = "flumen.publish_upload"
    bl_label = "Publishing…"

    def invoke(self, context, event):
        self._data = dict(_PENDING_UPLOAD)
        if not self._data.get("cmd") and not self._data.get("render_only"):
            self.report({"ERROR"}, "Nothing to upload.")
            return {"CANCELLED"}
        wm = context.window_manager
        wm.progress_begin(0, 100)
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        # Render-only jobs (Render turntable): no upload, straight to phase 2.
        if self._data.get("render_only"):
            plan = self._render_plan()
            if plan:
                cmd, cwd, label, note = plan
                self._note = note
                if self._begin(context, cmd, cwd, label, "render"):
                    return {"RUNNING_MODAL"}
            return self._teardown(context, cancelled=True,
                                  msg="Could not start the render — launch from "
                                      "the Workspace app.")
        # Phase 0 (optional): headless clean/bake of the publish copy. Then the
        # upload; then the background render.
        if self._data.get("post_cmd"):
            if self._begin(context, self._data["post_cmd"], self._data["cwd"],
                           "Preparing publish", "post"):
                return {"RUNNING_MODAL"}
            return self._teardown(context, cancelled=True,
                                  msg="Could not start the publish post-process.")
        if not self._begin(context, self._data["cmd"], self._data["cwd"],
                           "Publishing", "upload"):
            return self._teardown(context, cancelled=True,
                                  msg="Could not start upload.")
        return {"RUNNING_MODAL"}

    def _begin(self, context, cmd, cwd, label, phase):
        """Start a subprocess + a daemon reader thread feeding a queue. Returns
        False if the process couldn't be launched."""
        import queue
        import threading
        _publog(f"{phase}: {' '.join(str(c) for c in cmd)} (cwd {cwd})",
                echo=False)
        try:
            # encoding/errors pinned: with bare text=True Windows decodes the
            # pipe as cp1252, and one non-decodable byte in the toolkit's
            # output kills the reader thread — after which the subprocess
            # blocks forever on a full pipe, mid-upload.
            self._proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1, **_no_window())
        except Exception as exc:  # noqa: BLE001
            _publog(f"could not start {phase}: {exc}")
            return False
        self._queue = queue.Queue()

        def _reader(proc, q):
            try:
                for line in proc.stdout:
                    q.put(line.rstrip("\n"))
            except Exception:  # noqa: BLE001
                pass
            q.put(None)  # EOF sentinel
        self._thread = threading.Thread(
            target=_reader, args=(self._proc, self._queue), daemon=True)
        self._thread.start()
        self._label, self._phase, self._pct, self._eta = label, phase, 0, None
        self._status(context, f"{label}… starting")
        return True

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {"PASS_THROUGH"}
        eof = False
        while True:
            try:
                line = self._queue.get_nowait()
            except Exception:  # noqa: BLE001 — queue.Empty
                break
            if line is None:
                eof = True
                break
            print(line)              # full toolkit output -> blender.log
            if not line.startswith(_PROGRESS_PREFIX):
                _publog("  " + line, echo=False)   # and -> publish.log
            parsed = _parse_progress(line)
            if parsed:
                self._pct, self._eta, _ = parsed
                context.window_manager.progress_update(self._pct)
                eta = _human_eta(self._eta)
                self._status(context,
                             f"{self._label}… {self._pct}%" + (f" · {eta}" if eta else ""))
            elif self._phase == "render":
                # Frames are done but the toolkit is still encoding/uploading the
                # clip — keep the status meaningful instead of stuck at 100%.
                low = line.lower()
                if "encoding" in low:
                    self._status(context, f"{self._label}… encoding video")
                elif "published" in low or "uploading" in low:
                    self._status(context, f"{self._label}… uploading")
        if eof:
            return self._phase_done(context)
        return {"PASS_THROUGH"}

    def _phase_done(self, context):
        # The reader thread saw EOF, but the process may not be reaped yet —
        # poll() can still say None here (seen on Windows). None used to be
        # treated as SUCCESS, which let a failed upload look published and
        # even run the turntable phase. Get the real exit code.
        rc = self._proc.poll()
        if rc is None:
            try:
                rc = self._proc.wait(timeout=15)
            except Exception:  # noqa: BLE001 — still running: output ended early
                _publog(f"phase '{self._phase}': output ended but the process "
                        f"never exited — killing it and treating as failure")
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                rc = -1
        _publog(f"phase '{self._phase}' finished (rc {rc})", echo=False)
        if self._phase == "post":
            if rc != 0:
                return self._teardown(
                    context, cancelled=True,
                    msg="Publish aborted — the clean/bake post-process failed "
                        "(see blender.log). Retry, or without 'Apply modifiers'.")
            context.window_manager.progress_update(0)
            if self._begin(context, self._data["cmd"], self._data["cwd"],
                           "Publishing", "upload"):
                return {"PASS_THROUGH"}
            return self._teardown(context, cancelled=True,
                                  msg="Could not start upload.")
        if self._phase == "upload":
            if rc != 0:
                return self._teardown(
                    context, cancelled=True,
                    msg="Publish upload failed — the files did NOT reach the "
                        "server. See the log for the exact error.")
            plan = self._render_plan()
            if plan:
                cmd, cwd, label, note = plan
                self._note = note
                context.window_manager.progress_update(0)
                if self._begin(context, cmd, cwd, label, "render"):
                    return {"PASS_THROUGH"}      # phase 2 now running
            # No render (or it wouldn't start): we're done after the upload.
            return self._teardown(context, msg=self._data.get("success", "Published."))
        # Phase 2 (render) finished — publish already succeeded regardless.
        if rc != 0:
            _publog(f"review render failed (rc {rc}) — publish itself succeeded")
        tail = (getattr(self, "_note", "") if rc == 0
                else "  (review render failed — see blender.log)")
        return self._teardown(context, msg=self._data.get("success", "Published.") + tail)

    def _status(self, context, text):
        try:
            context.workspace.status_text_set(text)
        except Exception:  # noqa: BLE001
            pass

    def _teardown(self, context, msg, cancelled=False):
        wm = context.window_manager
        try:
            wm.event_timer_remove(self._timer)
        except Exception:  # noqa: BLE001
            pass
        wm.progress_end()
        self._status(context, None)  # clear the status bar
        _publog(("publish FAILED: " if cancelled else "publish done: ") + msg,
                echo=False)
        self.report({"ERROR"} if cancelled else {"INFO"}, msg)
        if cancelled:
            # Reports from a modal operator don't pop up like normal ones — an
            # artist watching the viewport can miss the failure entirely. Show
            # an unmissable dialog with where to read the details.
            import textwrap
            lines = textwrap.wrap(msg, 64) + [
                "Details: Flumen > Show pipeline log,",
                f"or {PUBLISH_LOG}"]

            def _draw(popup, _ctx):
                for chunk in lines:
                    popup.layout.label(text=chunk)
            try:
                wm.popup_menu(_draw, title="Publish failed", icon="ERROR")
            except Exception:  # noqa: BLE001 — headless/background session
                pass
        return {"CANCELLED"} if cancelled else {"FINISHED"}

    def _render_plan(self):
        """Build the review-render command to run as phase 2, or None. Returns
        (cmd, cwd, status_label, done_note)."""
        d = self._data
        if not d.get("render"):
            return None
        if d.get("step") == "model":
            cmd = ["turntable", "--model", d["pub_path"], "--task", d["task_id"]]
            label, note = "Rendering turntable", "  Turntable published → dailies."
        elif d.get("step") == "surface":
            cmd = ["look-review", "--task", d["task_id"], "--look", d["look_name"]]
            label, note = "Rendering look review", "  Look review published → dailies."
        elif d.get("ttype") == "shot":
            cmd = ["playblast", "--shot-file", d["pub_path"], "--task", d["task_id"]]
            label, note = "Rendering playblast", "  Playblast published → dailies."
        else:
            return None
        cmd += d.get("extra_args") or []
        if "--preview" in (d.get("extra_args") or []):
            label = "Rendering preview"
            note = "  Opened in your video player — nothing uploaded."
        full, td = _toolkit_cmd(cmd)
        if not full:
            _publog("review render skipped — toolkit not available to run "
                    + " ".join(cmd))
            return None
        return full, td, label, note


_EXISTING_LOOKS = []   # this asset's published look names, for the publish dropdown


def look_name_search(self, context, edit_text):
    """Suggest already-published look names (so a re-publish reuses a variant) while
    still letting the artist type a brand-new name."""
    et = (edit_text or "").lower()
    return [n for n in _EXISTING_LOOKS if et in n.lower()] or list(_EXISTING_LOOKS)


def _fetch_existing_looks(task_id):
    cmd, td = _toolkit_cmd(["list-looks", "--task", task_id])
    if cmd is None:
        return []
    try:
        out = subprocess.check_output(cmd, cwd=td, text=True, **_no_window())
        return [l["look"] for l in json.loads(out.splitlines()[-1])]
    except Exception:  # noqa: BLE001
        return []


_EXISTING_DRESSINGS = []   # this environment's published dressing names


def dressing_name_search(self, context, edit_text):
    """Suggest already-published dressing names (re-publish versions up) while
    still letting the artist type a brand-new name."""
    et = (edit_text or "").lower()
    return ([n for n in _EXISTING_DRESSINGS if et in n.lower()]
            or list(_EXISTING_DRESSINGS))


def _fetch_existing_dressings(task_id):
    rows = _shell_json(["list-dressings", "--task", task_id]) or []
    return [d["dressing"] for d in rows if d.get("dressing")]


_HDRI_ITEMS = []   # kept referenced so Blender's EnumProperty doesn't GC the strings


def lookdev_hdri_items(self, context):
    """HDRIs available for a look review: the project default, an explicit neutral,
    and each .exr/.hdr under 05_library/hdri."""
    global _HDRI_ITEMS
    items = [("", "Project default", "Use the project's configured HDRI"),
             ("none", "None (neutral grey)", "No HDRI — neutral studio lighting")]
    root = os.environ.get("FLUMEN_PROJECT_ROOT")
    if root:
        d = os.path.join(root, "05_library", "hdri")
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if os.path.splitext(f)[1].lower() in (".exr", ".hdr"):
                    items.append((f, f, "Light the look review with this HDRI"))
    _HDRI_ITEMS = items
    return _HDRI_ITEMS


class FLUMEN_OT_load_model(bpy.types.Operator):
    bl_idname = "flumen.load_model"
    bl_label = "Load published model"
    bl_description = ("Append the latest published model geometry for this asset "
                      "into the scene, under the publish locator, ready to shade")

    def execute(self, context):
        task = active_task()
        if not task or task.get("type") != "asset" or not task.get("entity"):
            self.report({"ERROR"}, "No active asset task — open a surface/rig task "
                                   "from the Workspace app.")
            return {"CANCELLED"}
        # The Workspace app may have pre-downloaded the model publish; else fetch it.
        model_blend = os.environ.get("FLUMEN_MODEL_PUBLISH")
        if not model_blend or not os.path.isfile(model_blend):
            model_blend = self._fetch_model(task)
        if not model_blend or not os.path.isfile(model_blend):
            self.report({"ERROR"}, "No published model found for this asset — "
                                   "publish the model step first.")
            return {"CANCELLED"}

        added = self._append_objects(context, model_blend)
        if not added:
            self.report({"ERROR"}, "Published model file held no objects.")
            return {"CANCELLED"}
        self._parent_under_locator(context, added)
        self.report({"INFO"}, f"Loaded {len(added)} object(s) from "
                              f"{os.path.basename(model_blend)} — shade away.")
        return {"FINISHED"}

    def _fetch_model(self, task):
        cmd, td = _toolkit_cmd(
            ["fetch-publish", "--task", task["id"], "--step", "model"])
        if cmd is None:
            return None
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True, **_no_window()).strip()
            return out.splitlines()[-1] if out else None
        except Exception:  # noqa: BLE001
            return None

    def _append_objects(self, context, blend_path):
        name = publish_locator_name()
        with bpy.data.libraries.load(blend_path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        appended = [o for o in dst.objects if o is not None]
        # The published .blend is the modeler's whole work scene — it carries the
        # PUBLISH locator's geometry PLUS scene clutter (helper cubes, cameras,
        # lights, line-art). The locator defines exactly what was published, so we
        # bring in ONLY its subtree and drop the rest — the pipeline must never
        # pull random objects into a downstream file.
        locator = next((o for o in appended
                        if getattr(o, "type", "") == "EMPTY"
                        and (o.name == name or o.name.split(".")[0] == name)), None)
        if locator is not None:
            keep = {locator, *locator.children_recursive}
        else:
            # No locator (shouldn't happen — publish requires one): fall back to
            # geometry only, never cameras/lights/grease-pencil.
            keep = {o for o in appended
                    if getattr(o, "type", "") in ("MESH", "EMPTY")}
        extras = [o for o in appended if o not in keep]
        for o in extras:
            data = getattr(o, "data", None)
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:  # noqa: BLE001
                pass
            _purge_orphan_data(data)   # don't let dropped data ride into the publish

        kept = [o for o in appended if o in keep]
        coll = context.scene.collection.objects
        for o in kept:
            if o.name not in coll:
                try:
                    coll.link(o)
                except RuntimeError:
                    pass
        return kept

    def _parent_under_locator(self, context, objs):
        name = publish_locator_name()
        added = set(objs)
        loc = bpy.data.objects.get(name)
        # A published model carries its OWN publish locator with the geometry
        # already parented under it. Reuse that as the scene locator (and merge any
        # duplicate) instead of re-rooting — never parent the locator to itself.
        appended_locs = [o for o in objs
                         if getattr(o, "type", "") == "EMPTY"
                         and (o.name == name or o.name.split(".")[0] == name)]
        if loc is None and appended_locs:
            loc = appended_locs.pop(0)
            try:
                loc.name = name           # claim the canonical name
            except Exception:  # noqa: BLE001
                pass
        if loc is None:
            loc = bpy.data.objects.new(name, None)
            loc.empty_display_type = "PLAIN_AXES"
            loc.empty_display_size = 0.5
            context.scene.collection.objects.link(loc)
        for dup in appended_locs:
            if dup is loc:
                continue
            for child in list(dup.children):
                child.parent = loc
            bpy.data.objects.remove(dup, do_unlink=True)
            added.discard(dup)
        for o in objs:
            # Re-root only the model's top-level geometry, preserving its internal
            # hierarchy; skip the locator itself and any non-geometry extras.
            if o is loc or o not in added:
                continue
            if getattr(o, "type", "") not in ("MESH", "EMPTY"):
                continue
            if o.parent is not None and o.parent in added:
                continue
            o.parent = loc


class FLUMEN_OT_preview_turntable(bpy.types.Operator):
    bl_idname = "flumen.preview_turntable"
    bl_label = "Preview Turntable Framing"
    bl_description = ("Open the turntable template in a new Blender window through "
                      "the camera (no render) to check framing — save the file first")

    def execute(self, context):
        path = bpy.data.filepath
        if not path:
            self.report({"ERROR"}, "Save the file first, then preview.")
            return {"CANCELLED"}
        # Always save: custom-property writes (the framing override lives on the
        # PUBLISH locator as raw ID props) do NOT flag bpy.data.is_dirty, so a
        # conditional save would silently skip them and the preview would read a
        # stale file — showing the old scale no matter what you change.
        bpy.ops.wm.save_mainfile()
        task = active_task()
        tid = task["id"] if task else "preview"
        cmd, td = _toolkit_cmd(["turntable", "--preview", "--model", path, "--task", tid])
        if cmd is None:
            self.report({"ERROR"}, "Toolkit not available — launch from the Workspace app.")
            return {"CANCELLED"}
        try:
            subprocess.Popen(cmd, cwd=td, **_no_window())   # non-blocking: keep working here
            self.report({"INFO"}, "Opening turntable preview… (close that window when done)")
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not start preview: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class FLUMEN_OT_render_look_turntable(bpy.types.Operator):
    bl_idname = "flumen.render_turntable"
    bl_label = "Render turntable"
    bl_description = ("Re-render this look's review turntable from its latest "
                      "published version — no new publish, no texture upload. "
                      "Objects hidden in this scene stay hidden; framing can be "
                      "overridden per render")

    override: bpy.props.BoolProperty(
        name="Override framing", default=False,
        description="Use a custom fit/zoom for this render instead of the "
                    "project / per-asset setting")
    fit_mode: bpy.props.EnumProperty(
        name="Fit", default="box",
        items=[("box", "Box — fit whole bounding box", ""),
               ("height", "Height — fill vertically", ""),
               ("width", "Width — fit widest horizontal", "")])
    fit_scale: bpy.props.FloatProperty(
        name="Zoom", default=1.0, min=0.05, max=5.0, soft_min=0.2, soft_max=2.0,
        description="<1 = smaller / more margin, >1 = bigger")

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("step") != "surface":
            self.report({"ERROR"}, "Open a surface (shading) task first — this "
                                   "re-renders a published look's turntable.")
            return {"CANCELLED"}
        global _EXISTING_LOOKS
        _EXISTING_LOOKS = _fetch_existing_looks(task["id"])
        if not _EXISTING_LOOKS:
            self.report({"ERROR"}, "No published look yet — publish once first "
                                   "(the turntable renders published textures).")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=380, title="Render turntable", confirm_text="Render")

    def draw(self, context):
        col = self.layout.column()
        col.prop(context.window_manager, "flumen_look_name", text="Look")
        col.prop(self, "override")
        sub = col.column()
        sub.enabled = self.override
        sub.prop(self, "fit_mode")
        sub.prop(self, "fit_scale", slider=True)
        hidden = _scene_hidden_names(context)
        if hidden:
            col.separator()
            col.label(text=f"{len(hidden)} hidden object(s) will stay hidden.",
                      icon="HIDE_ON")

    def execute(self, context):
        task = active_task()
        if not task:
            return {"CANCELLED"}
        look_name = look_mod.normalize_look_name(
            context.window_manager.flumen_look_name)
        if look_name not in _EXISTING_LOOKS:
            self.report({"ERROR"}, f"No published look named '{look_name}' — "
                                   f"pick one of: {', '.join(_EXISTING_LOOKS)}.")
            return {"CANCELLED"}
        extra = ["--turntable-only"]
        hidden = _scene_hidden_names(context)
        if hidden:
            extra += ["--hide", "||".join(hidden)]
        if self.override:
            extra += ["--fit-mode", self.fit_mode,
                      "--fit-scale", f"{self.fit_scale:g}"]
        _PENDING_UPLOAD.clear()
        _PENDING_UPLOAD.update({
            "render_only": True, "render": True, "step": "surface",
            "ttype": task.get("type"), "task_id": task["id"],
            "look_name": look_name, "extra_args": extra,
            "success": f"Turntable of look '{look_name}' rendered.",
        })
        bpy.ops.flumen.publish_upload('INVOKE_DEFAULT')
        return {"FINISHED"}


class FLUMEN_OT_preview_playblast(bpy.types.Operator):
    bl_idname = "flumen.preview_playblast"
    bl_label = "Preview playblast"
    bl_description = ("Render a playblast of THIS scene exactly as your "
                      "viewport shows it and open it in your video player — "
                      "nothing is published or uploaded. Same renderer and "
                      "WYSIWYG visibility as the dailies playblast")

    def execute(self, context):
        task = active_task()
        if not task or task.get("type") != "shot":
            self.report({"ERROR"}, "Open a shot task from the Workspace app.")
            return {"CANCELLED"}
        # Snapshot the session AS-IS (unsaved edits included) into an
        # untracked sibling of work/: inside the project tree so linked
        # libraries keep resolving, outside work/ so it never rides a sync,
        # a publish or the work-version counter.
        prev_dir = os.path.join(os.path.dirname(task["work_dir"]), ".preview")
        tmp = os.path.join(prev_dir, "pb_preview.blend")
        try:
            os.makedirs(prev_dir, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=tmp, copy=True)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not snapshot the scene: {exc}")
            return {"CANCELLED"}
        _publog(f"playblast preview: snapshot -> {tmp}", echo=False)
        _PENDING_UPLOAD.clear()
        _PENDING_UPLOAD.update({
            "render_only": True, "render": True,
            "step": task.get("step"), "ttype": "shot",
            "task_id": task["id"], "pub_path": tmp,
            "extra_args": ["--preview"],
            "success": "Playblast preview rendered.",
        })
        bpy.ops.flumen.publish_upload('INVOKE_DEFAULT')
        return {"FINISHED"}


# --- set-dressing workspace ---------------------------------------------------


class FLUMEN_OT_build_dressing(bpy.types.Operator):
    bl_idname = "flumen.build_dressing"
    bl_label = "Load environment"
    bl_description = ("Link the environment's published model (library override) "
                      "under an environment__ holder, ready for set-dressing")

    def execute(self, context):
        task = active_task()
        if not task or task.get("type") != "asset" or task.get("step") != "dressing":
            self.report({"ERROR"}, "No active dressing task — open the "
                                   "environment's dressing task from the Workspace app.")
            return {"CANCELLED"}
        leaf = (task.get("entity") or "").split("/")[-1]
        holder_name = dressing_mod.ENV_HOLDER_PREFIX + leaf
        if bpy.data.collections.get(holder_name) is not None:
            self.report({"INFO"}, "Environment already loaded.")
            return {"FINISHED"}

        blend = os.environ.get("FLUMEN_MODEL_PUBLISH")
        if not blend or not os.path.isfile(blend):
            blend = _fetch_publish_path(task["id"], "model")
        if not blend or not os.path.isfile(blend):
            self.report({"ERROR"}, "No published model for this environment — "
                                   "publish the model step first.")
            return {"CANCELLED"}

        holder = _named_holder(context, holder_name)
        override, err = _link_collection_override(context, blend, leaf, holder)
        if err:
            self.report({"ERROR"}, f"Could not load the environment: {err}")
            return {"CANCELLED"}
        holder["flumen_env_asset"] = task.get("entity", "")
        holder["flumen_env_step"] = "model"
        holder["flumen_env_blend_rel"] = _project_rel(blend)
        self.report({"INFO"}, f"Environment loaded from {os.path.basename(blend)} "
                              f"— add props and publish a dressing.")
        return {"FINISHED"}


# 'Add prop' dropdown items — cached (Blender enum-callback GC pitfall, same as
# _STEP_ENUM_CACHE) and refreshed on each invoke.
_PROP_CHOICES: list[tuple] = [("__none__", "(no published assets)", "")]


def _prop_enum_items(self, context):
    return _PROP_CHOICES


class FLUMEN_OT_add_prop(bpy.types.Operator):
    bl_idname = "flumen.add_prop"
    bl_label = "Add prop…"
    bl_description = ("Place a published asset into the dressing: linked + "
                      "overridable, parented under a prop_root__ empty that "
                      "carries the transform the manifest publishes")

    prop_choice: bpy.props.EnumProperty(name="Asset", items=_prop_enum_items)

    def invoke(self, context, event):
        global _PROP_CHOICES
        rows = _shell_json(["list-asset-publishes", "--step", "model"]) or []
        items = [(json.dumps(r), r["entity"], r["blend_rel"]) for r in rows]
        _PROP_CHOICES = items or [("__none__", "(no published assets)", "")]
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        self.layout.prop(self, "prop_choice")

    def execute(self, context):
        if self.prop_choice == "__none__":
            self.report({"ERROR"}, "No published assets to place.")
            return {"CANCELLED"}
        row = json.loads(self.prop_choice)
        entity, step = row["entity"], row.get("step", "model")
        leaf = entity.split("/")[-1]
        task_id = f"asset-{entity.replace('/', '_')}-{step}"

        blend = _fetch_publish_path(task_id, step)
        if not blend or not os.path.isfile(blend):
            self.report({"ERROR"}, f"Could not fetch the {step} publish of {entity}.")
            return {"CANCELLED"}

        existing = {o.get("flumen_prop_id") or
                    o.name[len(dressing_mod.PROP_ROOT_PREFIX):]
                    for o in bpy.data.objects
                    if o.name.startswith(dressing_mod.PROP_ROOT_PREFIX)}
        pid = dressing_mod.prop_id_for(leaf, existing)

        holder = _named_holder(context, dressing_mod.PROP_HOLDER_PREFIX + pid)
        override, err = _link_collection_override(context, blend, leaf, holder)
        if err:
            self.report({"ERROR"}, f"Could not place {entity}: {err}")
            return {"CANCELLED"}

        # The LOCAL empty that owns the placement: artists move THIS. Its world
        # matrix is what the dressing manifest records — never override data.
        root = bpy.data.objects.new(dressing_mod.PROP_ROOT_PREFIX + pid, None)
        root.empty_display_type = "PLAIN_AXES"
        root.empty_display_size = 0.5
        root["flumen_prop_id"] = pid
        root["flumen_prop_asset"] = entity
        root["flumen_prop_step"] = step
        root["flumen_prop_blend_rel"] = row.get("blend_rel") or _project_rel(blend)
        root["flumen_prop_collection"] = leaf
        holder.objects.link(root)
        root.location = context.scene.cursor.location
        for o in override.all_objects:
            if o.parent is None and o is not root:
                o.parent = root
        self.report({"INFO"}, f"Placed {entity} as {root.name} — move the empty, "
                              f"then publish the dressing.")
        return {"FINISHED"}


# --- review camera --------------------------------------------------------------
# A local dolly rig for framing review renders. Lives in its own collection and is
# EXCLUDED from every publish: the model post-process strips it (outside the
# PUBLISH locator), and dressing/shot publishes unlink it around the save.
REVIEW_CAM_COLL = "review_camera"


def _unlink_review_camera(context):
    """Temporarily remove the review-camera collection from the scene (and hand
    the scene camera back to a non-review camera) so a saved publish copy never
    carries it. Returns a restore() callable."""
    coll = bpy.data.collections.get(REVIEW_CAM_COLL)
    if coll is None:
        return lambda: None
    sc = context.scene.collection
    scene = context.scene
    was_linked = coll.name in sc.children
    prior_cam = scene.camera
    review_cams = {o for o in coll.all_objects if o.type == "CAMERA"}
    if scene.camera in review_cams:
        scene.camera = next((o for o in scene.objects
                             if o.type == "CAMERA" and o not in review_cams), None)
    if was_linked:
        try:
            sc.children.unlink(coll)
        except Exception:  # noqa: BLE001
            pass

    def restore():
        if was_linked:
            try:
                sc.children.link(coll)
            except Exception:  # noqa: BLE001
                pass
        scene.camera = prior_cam
    return restore


def _add_review_headlight(context, holder, cam):
    """A shadowless sun parented to the review camera — it lights whatever the
    camera looks at, like a DCC viewport headlight. No shadows means walls and
    ceilings can't block it (interiors light up too), and a sun adds ~zero render
    cost. Only added when the scene has no render-enabled lights of its own
    (emissive materials don't count — an unlit set renders black without this).
    Returns True if the headlight was added."""
    if cam is None:
        return False
    if any(o.name.startswith("REVIEW_headlight") for o in holder.all_objects):
        return False
    review = set(holder.all_objects)
    for o in context.scene.objects:
        if o.type == "LIGHT" and o not in review and not o.hide_render:
            return False
    data = bpy.data.lights.new("REVIEW_headlight", "SUN")
    data.energy = 3.0        # W/m² — tweak the light if the still is too hot/dim
    try:
        data.use_shadow = False
    except AttributeError:   # older Blender: shadow flag lives elsewhere; harmless
        pass
    light = bpy.data.objects.new("REVIEW_headlight", data)
    holder.objects.link(light)
    light.parent = cam       # identity transform: shines exactly where cam looks
    return True


def _viewport_matrix(context):
    """World matrix of the user's current 3D-viewport view, or None. Lets the
    review camera spawn already framed on what the artist is looking at."""
    try:
        for area in context.window.screen.areas:
            if area.type == "VIEW_3D":
                r3d = area.spaces.active.region_3d
                if r3d is not None:
                    return r3d.view_matrix.inverted()
    except Exception:  # noqa: BLE001
        pass
    return None


class FLUMEN_OT_add_review_camera(bpy.types.Operator):
    bl_idname = "flumen.add_review_camera"
    bl_label = "Add review camera"
    bl_description = ("Add a free camera for review renders, framed on your "
                      "current view (plus a camera headlight if the scene has "
                      "no lights). A plain camera — move it with G/R or lock it "
                      "to the view (N ▸ View ▸ Camera to View). Never published "
                      "at any stage; use 'Render review still' when framed")

    def execute(self, context):
        existing = bpy.data.collections.get(REVIEW_CAM_COLL)
        if existing is not None:
            cam = next((o for o in existing.all_objects if o.type == "CAMERA"),
                       None)
            if _add_review_headlight(context, existing, cam):
                self.report({"INFO"}, "Review camera already there — added a "
                                      "headlight (the scene has no lights).")
            else:
                self.report({"INFO"}, "Review camera already in the scene.")
            return {"FINISHED"}
        holder = _named_holder(context, REVIEW_CAM_COLL)
        # A plain, unconstrained camera on purpose: a rigged camera fights
        # 'Lock Camera to View' (constraints snap it back every update) and a
        # review still needs no animatable rig.
        data = bpy.data.cameras.new("REVIEW_Camera")
        cam = bpy.data.objects.new("REVIEW_Camera", data)
        holder.objects.link(cam)
        view = _viewport_matrix(context)
        if view is not None:
            cam.matrix_world = view
        context.scene.camera = cam
        lit = _add_review_headlight(context, holder, cam)
        self.report({"INFO"}, "Review camera added on your current view"
                              + (", headlight on — the scene has no lights"
                                 if lit else "")
                              + " — tweak framing, then 'Render review still'.")
        return {"FINISHED"}


# In-flight review render: {out, task_id, prior} while the F12-style render job
# runs; the render_complete/render_cancel handlers below consume it. The render
# is INVOKE_DEFAULT so the normal Render window (progress bar, Esc to cancel)
# shows — a blocking exec render would freeze the UI with no feedback.
_REVIEW_PENDING = None


def _review_render_finish(cancelled):
    """One-shot epilogue for the review render: restore camera/output settings,
    then (on success) hand the PNG to `flumen review-still` in the background."""
    global _REVIEW_PENDING
    pending, _REVIEW_PENDING = _REVIEW_PENDING, None
    for handlers, fn in ((bpy.app.handlers.render_complete, _on_review_complete),
                         (bpy.app.handlers.render_cancel, _on_review_cancel)):
        try:
            handlers.remove(fn)
        except ValueError:
            pass
    if pending is None:
        return
    scene = bpy.context.scene
    r = scene.render
    out = pending["out"]
    if not cancelled and not os.path.isfile(out):
        # write_still can lag the completion handler — save the result ourselves.
        try:
            bpy.data.images["Render Result"].save_render(filepath=out)
        except Exception as exc:  # noqa: BLE001
            print("[Flumen] review: could not save the render:", exc)
    cam_p, fp_p, fmt_p, media_p = pending["prior"]
    scene.camera, r.filepath = cam_p, fp_p
    if media_p is not None:               # restore media BEFORE format — the
        try:                              # format enum is filtered by it
            r.image_settings.media_type = media_p
        except (AttributeError, TypeError):
            pass
    try:
        r.image_settings.file_format = fmt_p
    except TypeError:
        pass
    if cancelled:
        print("[Flumen] review render cancelled — nothing uploaded.")
        return
    if not os.path.isfile(out):
        print("[Flumen] review render produced no image — nothing uploaded.")
        return
    cmd, td = _toolkit_cmd(["review-still", "--task", pending["task_id"],
                            "--file", out])
    if cmd is None:
        print(f"[Flumen] review rendered to {out}, but the toolkit isn't "
              f"available to upload.")
        return
    try:
        subprocess.Popen(cmd, cwd=td, **_no_window())
        print(f"[Flumen] review: uploading {os.path.basename(out)} "
              f"to dailies in background.")
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] review: upload failed to start:", exc)


def _on_review_complete(scene, *args):
    _review_render_finish(cancelled=False)


def _on_review_cancel(scene, *args):
    _review_render_finish(cancelled=True)


class FLUMEN_OT_render_review(bpy.types.Operator):
    bl_idname = "flumen.render_review"
    bl_label = "Render review still"
    bl_description = ("Render the current frame through the review camera "
                      "(progress in the render window) and upload it to "
                      "07_dailies (with the usual notification)")

    def execute(self, context):
        import tempfile
        import time as _time
        global _REVIEW_PENDING
        task = active_task()
        if not task:
            self.report({"ERROR"}, "No active task — open from the Workspace app.")
            return {"CANCELLED"}
        coll = bpy.data.collections.get(REVIEW_CAM_COLL)
        cam = next((o for o in coll.all_objects if o.type == "CAMERA"),
                   None) if coll else None
        if cam is None:
            self.report({"ERROR"}, "No review camera — run 'Add review camera' "
                                   "first.")
            return {"CANCELLED"}
        if _REVIEW_PENDING is not None:
            self.report({"WARNING"}, "A review render is already in progress.")
            return {"CANCELLED"}

        leaf = task.get("entity", "review").split("/")[-1]
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        out = os.path.join(tempfile.gettempdir(),
                           f"{leaf}_{task.get('step', '')}_review_{stamp}.png")
        scene = context.scene
        r = scene.render
        prior = (scene.camera, r.filepath, r.image_settings.file_format,
                 getattr(r.image_settings, "media_type", None))
        scene.camera = cam
        # media_type filters the file_format enum (Blender 4.4+/5.x): a scene
        # set to VIDEO output only offers FFMPEG until flipped back to IMAGE.
        try:
            r.image_settings.media_type = "IMAGE"
        except (AttributeError, TypeError):
            pass
        r.image_settings.file_format = "PNG"
        r.filepath = out
        _REVIEW_PENDING = {"out": out, "task_id": task["id"], "prior": prior}
        bpy.app.handlers.render_complete.append(_on_review_complete)
        bpy.app.handlers.render_cancel.append(_on_review_cancel)
        try:
            res = bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
        except Exception as exc:  # noqa: BLE001
            _review_render_finish(cancelled=True)
            self.report({"ERROR"}, f"Could not start the render: {exc}")
            return {"CANCELLED"}
        if "CANCELLED" in res:
            _review_render_finish(cancelled=True)
            self.report({"ERROR"}, "Could not start the render.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Rendering review still — watch the render window; "
                              "the upload starts when it finishes.")
        return {"FINISHED"}


class FLUMEN_OT_cycle_format(bpy.types.Operator):
    bl_idname = "flumen.cycle_format"
    bl_label = "Preview format"
    bl_description = ("Cycle the scene resolution through the project's "
                      "delivery formats (e.g. 16:9 ⇄ 9:16) so you can compose "
                      "the shot for every crop through the same camera")

    def execute(self, context):
        try:
            root = settings_io.find_project_root(_pref_local_root())
            settings = settings_io.load_settings(root) if root else {}
        except Exception:  # noqa: BLE001
            settings = {}
        formats = [f for f in (settings.get("formats") or [])
                   if f.get("name") and f.get("resolution_x")
                   and f.get("resolution_y")]
        if len(formats) < 2:
            self.report({"INFO"}, "No delivery formats configured for this "
                                  "project (project_settings 'formats').")
            return {"CANCELLED"}
        r = context.scene.render
        idx = next((i for i, f in enumerate(formats)
                    if (int(f["resolution_x"]), int(f["resolution_y"]))
                    == (r.resolution_x, r.resolution_y)), -1)
        nxt = formats[(idx + 1) % len(formats)]
        r.resolution_x = int(nxt["resolution_x"])
        r.resolution_y = int(nxt["resolution_y"])
        # Match the render's nesting (see blender_playblast): a format narrower
        # than the primary previews as the centered slice of it — same vertical
        # FOV — by locking the camera's vertical sensor size. The original
        # sensor state is stashed on the camera data and restored on the
        # primary, where a safe-area box marks the narrowest format's crop.
        cam = context.scene.camera.data if context.scene.camera else None
        bx, by = (int(formats[0]["resolution_x"]),
                  int(formats[0]["resolution_y"]))
        if cam is not None and bx >= by:
            if "flumen_sensor_fit" not in cam:
                cam["flumen_sensor_fit"] = cam.sensor_fit
                cam["flumen_sensor_height"] = cam.sensor_height
            narrower = (int(nxt["resolution_x"]) / int(nxt["resolution_y"])
                        < bx / by - 1e-6)
            if narrower and cam["flumen_sensor_fit"] != "VERTICAL":
                cam.sensor_fit = "VERTICAL"
                cam.sensor_height = cam.sensor_width * (by / bx)
            else:
                cam.sensor_fit = str(cam["flumen_sensor_fit"])
                cam.sensor_height = float(cam["flumen_sensor_height"])
            # Crop guide on the primary: dashed box where the narrowest
            # format's slice sits inside this frame.
            slices = [f for f in formats
                      if int(f["resolution_x"]) / int(f["resolution_y"])
                      < bx / by - 1e-6]
            if slices and nxt is formats[0]:
                fx = min(int(f["resolution_x"]) / int(f["resolution_y"])
                         for f in slices) * by / bx
                try:
                    cam.show_safe_areas = True
                    context.scene.safe_areas.title = (1.0 - fx, 0.0)
                except Exception:  # noqa: BLE001
                    pass
        self.report({"INFO"}, f"Previewing {nxt['name']} "
                              f"({r.resolution_x}x{r.resolution_y}) — the "
                              f"playblast renders every format regardless.")
        return {"FINISHED"}


def _resolve_assembly(task, list_only=False, only=None, picks=None):
    """Module-level assembly resolve (build_shot has a method version). Returns
    the parsed JSON or None."""
    args = ["resolve-assembly", "--task", task["id"]]
    if list_only:
        args.append("--list")
    for eid in only or []:
        args += ["--only", eid]
    for eid, st in (picks or {}).items():
        args += ["--pick", f"{eid}={st}"]
    cmd, td = _toolkit_cmd(args)
    if cmd is None:
        return None
    try:
        out = subprocess.check_output(cmd, cwd=td, text=True,
                                      **_no_window()).strip()
        return json.loads(out.splitlines()[-1]) if out else {}
    except Exception as exc:  # noqa: BLE001
        _publog(f"resolve-assembly failed: {exc}")
        return None


def _headless_build_shot(context, task):
    """Build every resolvable element into the scene (no dialog): link/import,
    stamp, apply the element's look and published animation, place the camera,
    set the frame range. Additive — an element already in the scene is left
    alone. For the headless 'Cache shot' path. Returns the count built."""
    data = _resolve_assembly(task)
    if not data:
        return 0
    elements = data.get("elements") or []
    anim_elements = ((data.get("anim") or {}).get("elements")) or {}
    built = 0
    n = len(elements)
    print(f"[Flumen] cache: building {n} element(s) from the published shot…",
          flush=True)
    for i, el in enumerate(elements, 1):
        eid = str(el.get("id", ""))
        if bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid) is not None:
            continue                              # already present — additive
        loader = _ELEMENT_LOADERS.get(el.get("kind"))
        if loader is None:
            continue
        print(f"[Flumen] cache:   [{i}/{n}] building {eid} "
              f"({el.get('kind', 'asset')})…", flush=True)
        try:
            holder, err = loader(context, el)
        except Exception as exc:  # noqa: BLE001
            holder, err = None, str(exc)
        if not holder:
            _publog(f"headless build: {eid} failed: {err}")
            continue
        built += 1
        holder["flumen_step"] = ("camera" if el.get("kind") == "camera"
                                 else el.get("source_step", ""))
        holder["flumen_asset"] = el.get("asset", "")
        dressing = el.get("dressing")
        if isinstance(dressing, dict) and dressing.get("props"):
            _apply_dressing_props(context, holder, el)
        ld = el.get("look_data")
        if isinstance(ld, dict) and ld.get("blend_local"):
            try:
                _apply_element_look(holder, ld)
                holder["flumen_look"] = (f"{ld.get('name', '')} "
                                         f"v{int(ld.get('version', 0)):03d}")
            except Exception as exc:  # noqa: BLE001
                _publog(f"headless build: look on {eid} failed: {exc}")
        ael = anim_elements.get(eid)
        if ael and ael.get("blend_local") and ael.get("objects"):
            try:
                _apply_element_animation(holder, ael["blend_local"],
                                        ael["objects"],
                                        content=ael.get("content", ""))
                holder["flumen_anim"] = ael.get("version", "")
            except Exception as exc:  # noqa: BLE001
                _publog(f"headless build: anim on {eid} failed: {exc}")
    try:
        bpy.ops.file.make_paths_relative()
    except Exception:  # noqa: BLE001
        pass
    # Set the shot's frame range from the resolve (the interactive dialog seeds
    # _BUILD_FRAME_RANGE in invoke(); headless has no dialog, so set it here).
    # Without this the scene stays at 1-250 and a cache bakes frames BEFORE the
    # animation starts (keyed at 1001+) — static geometry.
    fs, fe = data.get("frame_start"), data.get("frame_end")
    if fs and fe:
        try:
            scene = context.scene
            scene.frame_start, scene.frame_end = int(fs), int(fe)
            scene.frame_set(int(fs))
        except Exception:  # noqa: BLE001
            pass
    else:
        _apply_build_frame_range(context)
    return built


def _cache_shot_elements(context, task, only=None):
    """Bake rigged+animated elements to alembic and publish them. `only` limits
    to those element ids (None = all candidates). Records the anim version each
    was baked from. Returns (published_pairs, failed)."""
    import tempfile
    scene = context.scene
    fs, fe = int(scene.frame_start), int(scene.frame_end)
    tmp = tempfile.mkdtemp(prefix="flumen_cache_")
    pairs, failed, anim_of = [], [], {}
    prev_active = context.view_layer.objects.active
    todo = [(eid, h) for eid, h in _cache_candidates()
            if only is None or eid in only]
    print(f"[Flumen] cache: baking {len(todo)} element(s) to Alembic over "
          f"frames {fs}-{fe} — this evaluates every frame, it can take a bit.",
          flush=True)
    for i, (eid, holder) in enumerate(todo, 1):
        anim_of[eid] = str(holder.get("flumen_anim", "") or "")
        meshes = [o for o in holder.all_objects
                  if getattr(o, "type", "") == "MESH"]
        if not meshes:
            failed.append((eid, "no meshes"))
            continue
        print(f"[Flumen] cache:   [{i}/{len(todo)}] baking {eid} "
              f"({len(meshes)} mesh(es))…", flush=True)
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:  # noqa: BLE001
            pass
        bpy.ops.object.select_all(action="DESELECT")
        for o in meshes:
            try:
                o.select_set(True)
            except Exception:  # noqa: BLE001
                pass
        context.view_layer.objects.active = meshes[0]
        path = os.path.join(tmp, f"{eid}.abc")
        try:
            bpy.ops.wm.alembic_export(
                filepath=path, selected=True, start=fs, end=fe,
                flatten=True, uvs=True, packuv=True, normals=True,
                face_sets=True, evaluation_mode="RENDER")
        except Exception as exc:  # noqa: BLE001
            failed.append((eid, str(exc)))
            continue
        if os.path.isfile(path):
            pairs.append((eid, path))
        else:
            failed.append((eid, "export produced no file"))
    context.view_layer.objects.active = prev_active
    if not pairs:
        return [], failed
    total_mb = sum(os.path.getsize(p) for _e, p in pairs) / 1e6
    print(f"[Flumen] cache: uploading {len(pairs)} cache(s) ({total_mb:.0f} MB) "
          f"to the server…", flush=True)
    args = ["publish-cache", "--task", task["id"]]
    for eid, path in pairs:
        args += ["--cache", f"{eid}={path}"]
        if anim_of.get(eid):
            args += ["--anim", f"{eid}={anim_of[eid]}"]
    cmd, td = _toolkit_cmd(args)
    if cmd is None:
        return [], failed + [("*", "toolkit not available to publish")]
    _publog(f"cache-shot: {' '.join(str(c) for c in cmd)}", echo=False)
    p = subprocess.run(cmd, cwd=td, text=True, capture_output=True,
                       **_no_window())
    for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
        _publog("  " + line, echo=False)
    if p.returncode != 0:
        return [], failed + [("*", "publish-cache failed")]
    return [(eid, "published") for eid, _ in pairs], failed


def headless_build_and_cache():
    """Entry point for the Workspace app's 'Cache shot' right-click: build the
    shot (if needed) and cache it, headless. Prints a FLUMEN result line and
    returns an exit code."""
    ctx = bpy.context
    task = active_task()
    if not task or task.get("type") != "shot":
        print("[Flumen] cache: no active shot task.")
        return 1
    # Build from PUBLISHED data on a clean scene (no default cube/camera/light,
    # no animator work file) — caching is always of reviewed, published anim.
    try:
        scaffold_empty_scene()
    except Exception:  # noqa: BLE001
        pass
    n = _headless_build_shot(ctx, task)
    _publog(f"cache: built {n} element(s) before caching", echo=True)
    # Restrict to the elements the artist ticked in the Workspace dialog.
    only_env = os.environ.get("FLUMEN_CACHE_ONLY", "")
    only = set(x for x in only_env.split(",") if x) if only_env else None
    pairs, failed = _cache_shot_elements(ctx, task, only=only)
    if not pairs:
        print("[Flumen] cache: nothing cached — "
              + "; ".join(f"{e}: {m}" for e, m in failed))
        return 1
    print(f"[Flumen] cache: published {len(pairs)} element(s): "
          + ", ".join(e for e, _ in pairs))
    return 0


def _cache_candidates():
    """Element holders to alembic-cache: rigged AND animated. An armature with
    an action (its animation) makes it a deforming character; environments
    (backdrops) and camera holders are skipped, and a static model with no
    armature has nothing to bake. Returns [(element_id, holder)]."""
    out = []
    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        if str(coll.get("flumen_asset", "")).startswith("environments/"):
            continue
        if str(coll.get("flumen_step", "")) == "camera":
            continue                              # the camera rig is not cached
        arms = [o for o in coll.all_objects
                if getattr(o, "type", "") == "ARMATURE"]
        animated = any(_action_fcurves(a) for a in arms)
        if arms and animated:
            out.append((coll.name[len(ELEMENT_HOLDER_PREFIX):], coll))
    return out


class FLUMEN_OT_cache_shot(bpy.types.Operator):
    bl_idname = "flumen.cache_shot"
    bl_label = "Cache shot (Alembic)"
    bl_description = ("Bake each rigged, animated character in this shot to its "
                      "own Alembic cache over the shot frame range and publish "
                      "them — the inputs a Lighting shot build imports. Camera, "
                      "environment and un-animated models are skipped")

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "shot":
            self.report({"ERROR"}, "Open a shot task from the Workspace app.")
            return {"CANCELLED"}
        self._cands = _cache_candidates()
        if not self._cands:
            self.report({"WARNING"}, "No rigged, animated elements to cache — "
                                     "Build shot and load animation first.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=420, title="Cache shot", confirm_text="Cache")

    def draw(self, context):
        col = self.layout.column()
        fs = int(context.scene.frame_start)
        fe = int(context.scene.frame_end)
        col.label(text=f"Bake to Alembic over frames {fs}–{fe}:")
        box = col.box()
        for eid, _ in self._cands:
            box.label(text=eid, icon="OUTLINER_OB_ARMATURE")
        col.label(text="Published per element; Lighting Build shot imports "
                       "the latest.", icon="INFO")

    def execute(self, context):
        task = active_task()
        if not task:
            return {"CANCELLED"}
        try:
            pairs, failed = _cache_shot_elements(context, task)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Cache failed: {exc}")
            return {"CANCELLED"}
        if not pairs:
            self.report({"ERROR"}, "Nothing cached — "
                        + "; ".join(f"{e}: {m}" for e, m in failed))
            return {"CANCELLED"}
        note = f"; skipped {len(failed)}" if failed else ""
        self.report({"INFO"}, f"Cached + published {len(pairs)} element(s){note}.")
        return {"FINISHED"}


CLASSES = (
    FLUMEN_OT_apply_project_settings,
    FLUMEN_OT_verify_ocio,
    FLUMEN_OT_pull_settings,
    FLUMEN_OT_add_locator,
    FLUMEN_OT_add_publish_collection,
    FLUMEN_OT_save_to_task,
    FLUMEN_OT_check,
    FLUMEN_PublishItem,             # PropertyGroup — register before the operator
    FLUMEN_OT_publish,
    FLUMEN_OT_build_dressing,
    FLUMEN_OT_add_prop,
    FLUMEN_OT_auto_fix,
    FLUMEN_OT_test_connection,
    FLUMEN_OT_show_log,
    FLUMEN_OT_add_review_camera,
    FLUMEN_OT_render_review,
    FLUMEN_OT_cycle_format,
    FLUMEN_OT_preview_playblast,
    FLUMEN_OT_publish_upload,
    FLUMEN_OT_load_model,
    FLUMEN_OT_apply_look,
    FLUMEN_AssemblyItem,            # PropertyGroup — register before the operator
    FLUMEN_OT_build_shot,
    FLUMEN_AnimItem,                # PropertyGroup — register before the operator
    FLUMEN_OT_load_animation,
    FLUMEN_OT_cache_shot,
    FLUMEN_OT_add_lights,
    FLUMEN_OT_publish_lights,
    FLUMEN_OT_load_lights,
    FLUMEN_OT_turntable_framing,
    FLUMEN_OT_preview_turntable,
    FLUMEN_OT_render_look_turntable,
)
