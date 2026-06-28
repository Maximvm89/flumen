"""Blender operators for the Legami pipeline addon."""

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


def _prefs():
    """Addon preferences if the addon was installed the normal way, else None
    (when auto-loaded for a session, settings come from env vars instead)."""
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _pref_local_root():
    p = _prefs()
    return getattr(p, "local_root", None) if p else None


def _toolkit_cmd(args):
    """Build the argv to invoke the animpipe toolkit, or None if unavailable.

    From source the launcher sets MODULE=animpipe and PY=python, so we run
    `python -m animpipe …`. When frozen, PY is animpipe.exe and MODULE is empty,
    so we call the executable directly."""
    py = os.environ.get("LEGAMI_TOOLKIT_PY")
    td = os.environ.get("LEGAMI_TOOLKIT_DIR")
    if not py or not td:
        return None, None
    mod = os.environ.get("LEGAMI_TOOLKIT_MODULE", "animpipe")
    prefix = [py] + (["-m", mod] if mod else [])
    return prefix + list(args), td


def _shell_toolkit(args, report):
    """Run an animpipe CLI command via the toolkit the launcher exposed."""
    cmd, td = _toolkit_cmd(args)
    if cmd is None:
        report({"ERROR"}, "Toolkit not available — launch from the Workspace app.")
        return False
    try:
        subprocess.check_call(cmd, cwd=td)
        return True
    except Exception as exc:  # noqa: BLE001
        report({"ERROR"}, f"Command failed: {exc}")
        return False


def _apply_one(report, label, fn):
    """Run a single setting application, collecting warnings instead of crashing."""
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 — we want to keep going
        report.append(f"  - skipped {label}: {exc}")
        return False


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


class LEGAMI_OT_apply_project_settings(bpy.types.Operator):
    bl_idname = "legami.apply_project_settings"
    bl_label = "Apply Project Settings"
    bl_description = "Apply the project's standard color, render, units and output settings to this scene"
    bl_options = {"REGISTER", "UNDO"}

    apply_all_scenes: bpy.props.BoolProperty(
        name="All Scenes", default=False,
        description="Apply to every scene in this file, not just the active one")

    def execute(self, context):
        root = settings_io.find_project_root(_pref_local_root())
        if not root:
            self.report({"ERROR"}, "No project root. Launch via the Legami launcher, "
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
            print("[Legami] Project settings applied with warnings:")
            print("\n".join(warnings))
            print(f"[Legami] BLENDER_OCIO = {ocio}")
        else:
            self.report({"INFO"}, "Project settings applied.")
        return {"FINISHED"}


class LEGAMI_OT_verify_ocio(bpy.types.Operator):
    bl_idname = "legami.verify_ocio"
    bl_label = "Verify Color Config"
    bl_description = "Check that Blender loaded the project OCIO config and the project's color names exist"

    def execute(self, context):
        root = settings_io.find_project_root(_pref_local_root())
        env_ocio = os.environ.get("BLENDER_OCIO")
        expected = settings_io.ocio_path(root) if root else None

        msgs = []
        if not env_ocio:
            msgs.append("BLENDER_OCIO is NOT set — Blender is using its bundled config. "
                        "Launch via the Legami launcher.")
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
        print("[Legami] Verify color config:\n  " + "\n  ".join(msgs))
        return {"FINISHED"}


class LEGAMI_OT_pull_settings(bpy.types.Operator):
    bl_idname = "legami.pull_settings"
    bl_label = "Pull Latest From FTP"
    bl_description = "Re-sync the project config (OCIO + project_settings.json) from the FTP"

    def execute(self, context):
        if _shell_toolkit(["sync", "--remote", "02_pipeline"], self.report):
            self.report({"INFO"}, "Synced latest config. Now Apply Project Settings.")
            return {"FINISHED"}
        return {"CANCELLED"}


def active_task():
    """The task this Blender session was opened for (set by the Workspace app via
    env vars), or None if Blender was launched without a task context."""
    tid = os.environ.get("LEGAMI_TASK_ID")
    if not tid:
        return None
    return {
        "id": tid,
        "type": os.environ.get("LEGAMI_TASK_TYPE", ""),
        "entity": os.environ.get("LEGAMI_TASK_ENTITY", ""),
        "step": os.environ.get("LEGAMI_TASK_STEP", ""),
        "title": os.environ.get("LEGAMI_TASK_TITLE", ""),
        "work_dir": os.environ.get("LEGAMI_TASK_WORK_DIR", ""),
    }


class LEGAMI_OT_save_to_task(bpy.types.Operator):
    bl_idname = "legami.save_to_task"
    bl_label = "Save into task work folder"
    bl_description = ("Save the current .blend into this task's work/ folder with "
                      "an auto-incremented version")

    def execute(self, context):
        task = active_task()
        if not task or not task["work_dir"]:
            self.report({"ERROR"}, "No active task. Open this scene from the "
                                   "Workspace app's 'Open in Blender'.")
            return {"CANCELLED"}
        work_dir = task["work_dir"]
        os.makedirs(work_dir, exist_ok=True)
        base = f"{task['entity'].replace('/', '_')}_{task['step']}"
        existing = [f for f in os.listdir(work_dir)
                    if f.startswith(base) and f.endswith(".blend")]
        version = len(existing) + 1
        path = os.path.join(work_dir, f"{base}_v{version:03d}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        self.report({"INFO"}, f"Saved {os.path.basename(path)}")
        return {"FINISHED"}


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
    return [types.SimpleNamespace(name=img.name, is_missing=_image_missing(img))
            for img in _images_of_materials(materials)]


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


def _run_task_checks(step, context, ttype=None):
    """run_checks with surface texture state injected for the surface step, and the
    task type so shot publishes get the shot gate (camera + frame range)."""
    extra = _texture_check_records() if step == "surface" else None
    return checks.run_checks(step, context.scene, list(context.scene.objects),
                             publish_locator_name(), textures=extra, ttype=ttype)


class LEGAMI_OT_turntable_framing(bpy.types.Operator):
    bl_idname = "legami.turntable_framing"
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
            self.report({"ERROR"}, "Add a Publish Locator first (Legami ▸ Add Publish Locator).")
            return {"CANCELLED"}
        self.override = bool(loc.get("legami_tt_override", 0))
        m = loc.get("legami_tt_fit_mode")
        if m in ("box", "height", "width"):
            self.fit_mode = m
        sc = loc.get("legami_tt_fit_scale")
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
        loc["legami_tt_override"] = 1 if self.override else 0
        loc["legami_tt_fit_mode"] = self.fit_mode
        loc["legami_tt_fit_scale"] = float(self.fit_scale)
        state = (f"{self.fit_mode} @ {self.fit_scale:.2f}x" if self.override
                 else "project default")
        self.report({"INFO"}, f"Turntable framing → {state} (on {loc.name}).")
        return {"FINISHED"}


class LEGAMI_OT_add_locator(bpy.types.Operator):
    bl_idname = "legami.add_publish_locator"
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


def _server_next_version(task_id: str, base: str) -> int | None:
    """Authoritative next version from the task's server publish history (via the
    toolkit). None if the toolkit/server isn't reachable, so we fall back to local."""
    cmd, td = _toolkit_cmd(["next-version", "--task", task_id, "--base", base])
    if cmd is None:
        return None
    try:
        out = subprocess.check_output(cmd, cwd=td, text=True).strip()
        return int(out.splitlines()[-1])
    except Exception:  # noqa: BLE001
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
        print("[Legami] FBX export failed:", exc)
        return False


def _draw_checks(layout, issues):
    box = layout.box()
    box.label(text="Sanity checks:")
    if not issues:
        box.label(text="All checks passed.", icon="CHECKMARK")
        return
    for level, msg in issues:
        box.label(text=msg, icon="ERROR" if level == checks.ERROR else "INFO")


class LEGAMI_OT_check(bpy.types.Operator):
    bl_idname = "legami.run_checks"
    bl_label = "Run Sanity Checks"
    bl_description = "Run the pre-publish sanity checks for this task and show issues"

    _issues: list = []

    def invoke(self, context, event):
        task = active_task()
        step = task["step"] if task else ""
        self._issues = _run_task_checks(step, context)
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        _draw_checks(self.layout, self._issues)
        if checks.has_errors(self._issues):
            self.layout.label(text="Errors would block a publish.", icon="CANCEL")

    def execute(self, context):
        return {"FINISHED"}  # informational only


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
    coll = bpy.data.collections.new(coll_name)
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

    return restore


class LEGAMI_OT_publish(bpy.types.Operator):
    bl_idname = "legami.publish"
    bl_label = "Publish"
    bl_description = ("Run sanity checks, then write a versioned .blend + FBX into "
                      "this task's publish/ folder, upload, and set status to Review")

    _issues: list = []

    def invoke(self, context, event):
        task = active_task()
        if not task or not task["work_dir"]:
            self.report({"ERROR"}, "No active task. Open this scene from the "
                                   "Workspace app's 'Open in Blender'.")
            return {"CANCELLED"}
        self._issues = _run_task_checks(task["step"], context, task.get("type"))
        if task.get("step") == "surface":
            global _EXISTING_LOOKS
            _EXISTING_LOOKS = _fetch_existing_looks(task["id"])
        return context.window_manager.invoke_props_dialog(
            self, width=480, title="Publish", confirm_text="Publish")

    def draw(self, context):
        col = self.layout.column()
        col.prop(context.window_manager, "legami_publish_desc", text="Description")
        task = active_task()
        if task and task.get("step") == "model":
            col.prop(context.window_manager, "legami_render_turntable")
        if task and task.get("step") == "surface":
            wm = context.window_manager
            col.prop(wm, "legami_look_name", text="Look name")
            col.prop(wm, "legami_render_turntable", text="Render look review")
        if task and task.get("type") == "shot":
            col.prop(context.window_manager, "legami_render_turntable",
                     text="Render playblast")
        col.separator()
        _draw_checks(col, self._issues)
        col.separator()
        if checks.has_errors(self._issues):
            col.label(text="Errors must be fixed — publish is blocked.", icon="CANCEL")
        else:
            col.label(text="Ready to publish.", icon="CHECKMARK")

    def execute(self, context):
        task = active_task()
        if not task or not task["work_dir"]:
            self.report({"ERROR"}, "No active task.")
            return {"CANCELLED"}

        issues = _run_task_checks(task["step"], context, task.get("type"))
        if checks.has_errors(issues):
            errs = [m for lvl, m in issues if lvl == checks.ERROR]
            self.report({"ERROR"}, "Publish blocked: " + errs[0])
            print("[Legami] publish blocked:\n  " + "\n  ".join(errs))
            return {"CANCELLED"}

        publish_dir = os.path.join(os.path.dirname(task["work_dir"]), "publish")
        os.makedirs(publish_dir, exist_ok=True)
        name = task["entity"].split("/")[-1]
        # Surface publishes a named look, versioned on its own track; other steps
        # version by step.
        look_name = ""
        if task["step"] == "surface":
            look_name = look_mod.normalize_look_name(
                context.window_manager.legami_look_name)
            base = look_mod.look_base(name, look_name)
        else:
            base = f"{name}_{task['step']}"
        # The server publish history is the single source of truth for versions.
        # If we can't reach it, abort rather than guess a number that could collide.
        version = _server_next_version(task["id"], base)
        if not version:
            self.report({"ERROR"}, "Couldn't reach the server to determine the next "
                        "version — publish aborted. Check your connection and retry.")
            return {"CANCELLED"}
        pub_path = os.path.join(publish_dir, f"{base}_v{version:03d}.blend")

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
        elif task.get("type") == "shot":
            # Shot publish: save the assembled scene (linked rigs + camera +
            # animation) as the versioned publish — no collection wrap, no FBX.
            try:
                bpy.ops.file.make_paths_relative()
            except Exception:  # noqa: BLE001
                pass
            bpy.ops.wm.save_as_mainfile(filepath=pub_path, copy=True)
            files = [pub_path]
            kind = ".blend (shot)"
            # Also publish each element's animation as editable Actions + a manifest,
            # so Build shot can re-apply it onto freshly-linked rigs on a rebuild.
            # First snapshot any moved-but-unkeyed poses so static offsets aren't lost.
            _snapshot_poses(context)
            actions, elem_actions = _collect_element_animation()
            if actions:
                # The anim artifacts go in publish/anim/ (not beside the shot
                # publishes) so they're never offered as an openable workfile. They
                # ride the texture_files path, which preserves the publish/ subpath.
                anim_dir = os.path.join(publish_dir, "anim")
                os.makedirs(anim_dir, exist_ok=True)
                anim_path = os.path.join(anim_dir, f"{base}_v{version:03d}_anim.blend")
                bpy.data.libraries.write(anim_path, actions, fake_user=True)
                manifest = anim_mod.build_anim_manifest(version, elem_actions)
                anim_manifest_path = anim_mod.anim_manifest_path(anim_path)
                with open(anim_manifest_path, "w") as fh:
                    json.dump(manifest, fh, indent=2)
                texture_files += [anim_path, anim_manifest_path]
                kind += f" + anim ({len(actions)} action(s))"
        else:
            # Wrap the PUBLISH subtree in a collection named after the asset so a
            # downstream shot can LINK it as one unit (clean library overrides), and
            # relativize texture paths so a linked rig/model resolves its maps on any
            # machine (the same absolute-path bug fixed for look textures). We mutate
            # the live session only to write the copy, then restore it.
            loc = bpy.data.objects.get(publish_locator_name())
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
            fbx_path = pub_path[:-6] + ".fbx"   # .blend -> .fbx
            # Export only the geometry under the publish locator, if present.
            use_sel = False
            if loc:
                try:
                    bpy.ops.object.mode_set(mode="OBJECT")
                except Exception:  # noqa: BLE001
                    pass
                bpy.ops.object.select_all(action="DESELECT")
                loc.select_set(True)
                for d in _descendants(loc):
                    d.select_set(True)
                use_sel = True
            if _export_fbx(fbx_path, use_selection=use_sel):
                files.append(fbx_path)
            kind = ".blend + FBX"

        pub_args = ["publish", "--local", *files, "--task", task["id"],
                    "--status", "review",
                    "--description", context.window_manager.legami_publish_desc]
        for t in texture_files:
            pub_args += ["--texture", t]
        pub_cmd, td = _toolkit_cmd(pub_args)
        if pub_cmd is None:
            self.report({"WARNING"},
                        f"Saved {len(files)} file(s) to publish/, but the toolkit "
                        f"wasn't found to upload — push via the Workspace app.")
            return {"FINISHED"}

        try:
            subprocess.check_call(pub_cmd, cwd=td)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Saved locally but upload failed: {exc}")
            return {"CANCELLED"}

        context.window_manager.legami_publish_desc = ""  # reset for next publish

        # Kick off review media in the BACKGROUND (non-blocking).
        tt_msg = ""
        if (task.get("step") == "model"
                and context.window_manager.legami_render_turntable):
            try:
                tt_cmd, _ = _toolkit_cmd(
                    ["turntable", "--model", pub_path, "--task", task["id"]])
                subprocess.Popen(tt_cmd, cwd=td)
                tt_msg = " Turntable rendering in background → dailies."
            except Exception as exc:  # noqa: BLE001
                print("[Legami] could not start turntable:", exc)
        elif (task.get("step") == "surface"
                and context.window_manager.legami_render_turntable):
            try:
                lr_cmd, _ = _toolkit_cmd(
                    ["look-review", "--task", task["id"], "--look", look_name])
                subprocess.Popen(lr_cmd, cwd=td)
                tt_msg = " Look review (turntable + texture sheet) rendering → dailies."
            except Exception as exc:  # noqa: BLE001
                print("[Legami] could not start look review:", exc)
        elif (task.get("type") == "shot"
                and context.window_manager.legami_render_turntable):
            try:
                pb_cmd, _ = _toolkit_cmd(
                    ["playblast", "--shot-file", pub_path, "--task", task["id"]])
                subprocess.Popen(pb_cmd, cwd=td)
                tt_msg = " Playblast rendering in background → dailies."
            except Exception as exc:  # noqa: BLE001
                print("[Legami] could not start playblast:", exc)

        warns = sum(1 for lvl, _ in issues if lvl == checks.WARNING)
        suffix = f" ({warns} warning(s))" if warns else ""
        self.report({"INFO"}, f"Published {base}_v{version:03d} ({kind}); "
                              f"task → Review.{suffix}{tt_msg}")
        return {"FINISHED"}


def _addon_module_by_leaf(leaf):
    import addon_utils
    for m in addon_utils.modules():
        if m.__name__.rsplit(".", 1)[-1] == leaf:
            return m.__name__
    return None


def _install_shipped_extension(leaf):
    """Install a project‑shipped extension .zip (02_pipeline/blender_extensions/) so
    every artist gets the add‑on without manually downloading it. Returns the
    installed module name, or None if there's no zip / install failed."""
    import glob
    root = os.environ.get("LEGAMI_PROJECT_ROOT", "")
    ext_dir = os.path.join(root, "02_pipeline", "blender_extensions") if root else ""
    if not ext_dir or not os.path.isdir(ext_dir):
        return None
    zips = (glob.glob(os.path.join(ext_dir, leaf + ".zip"))
            or [z for z in glob.glob(os.path.join(ext_dir, "*.zip"))
                if leaf in os.path.basename(z)])
    if not zips:
        return None
    try:
        bpy.ops.extensions.package_install_files(
            filepath=zips[0], repo="user_default", enable_on_install=True)
        print("[Legami] installed add-on from", os.path.basename(zips[0]))
        return _addon_module_by_leaf(leaf)
    except Exception as exc:  # noqa: BLE001
        print(f"[Legami] install of '{leaf}' failed: {exc}")
        return None


def enable_project_addons():
    """Make the project's extra Blender add‑ons available — by default the
    'Add Camera Rigs' add‑on the layout step uses (Dolly / Crane rigs). Configurable
    via project_settings 'addons'. For each: enable it if installed; otherwise
    install it from the project‑shipped zip in 02_pipeline/blender_extensions/ (which
    syncs to every machine), then enable. Matching is by module leaf name, so bundled
    ('add_camera_rigs') and 4.2+ extension ('bl_ext.<repo>.add_camera_rigs') forms
    both work."""
    import addon_utils
    data = settings_io.load_settings(
        settings_io.find_project_root(_pref_local_root())) or {}
    wanted = data.get("addons")
    if wanted is None:
        wanted = ["add_camera_rigs"]
    for name in wanted or []:
        leaf = name.rsplit(".", 1)[-1]
        module = _addon_module_by_leaf(leaf) or _install_shipped_extension(leaf)
        if not module:
            print(f"[Legami] add-on '{name}' not available — ship its .zip in "
                  f"02_pipeline/blender_extensions/ or install it via Get Extensions.")
            continue
        try:
            addon_utils.enable(module, default_set=False)
            print("[Legami] add-on ready:", module)
        except Exception as exc:  # noqa: BLE001
            print(f"[Legami] could not enable {module}: {exc}")


def apply_project_color():
    """Set every scene's display device + view transform to the project's color
    management, so files authored with Blender's default names (sRGB/AgX/Standard)
    stop warning under the project's ACES OCIO config. Color only — leaves render,
    units and output untouched. Run at startup when launched from the Workspace app;
    the file's stored names self-heal on its next save."""
    root = settings_io.find_project_root(_pref_local_root())
    data = settings_io.load_settings(root) or {}
    cm = data.get("color_management") or {}
    if not cm.get("display_device") and not cm.get("view_transform"):
        return
    for scene in bpy.data.scenes:
        if cm.get("display_device"):
            try:
                scene.display_settings.display_device = cm["display_device"]
            except Exception:  # noqa: BLE001
                pass
        if cm.get("view_transform"):
            try:
                scene.view_settings.view_transform = cm["view_transform"]
            except Exception:  # noqa: BLE001
                pass
    print("[Legami] applied project color management to",
          len(bpy.data.scenes), "scene(s)")


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
        out = subprocess.check_output(cmd, cwd=td, text=True)
        return [l["look"] for l in json.loads(out.splitlines()[-1])]
    except Exception:  # noqa: BLE001
        return []


_HDRI_ITEMS = []   # kept referenced so Blender's EnumProperty doesn't GC the strings


def lookdev_hdri_items(self, context):
    """HDRIs available for a look review: the project default, an explicit neutral,
    and each .exr/.hdr under 05_library/hdri."""
    global _HDRI_ITEMS
    items = [("", "Project default", "Use the project's configured HDRI"),
             ("none", "None (neutral grey)", "No HDRI — neutral studio lighting")]
    root = os.environ.get("LEGAMI_PROJECT_ROOT")
    if root:
        d = os.path.join(root, "05_library", "hdri")
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if os.path.splitext(f)[1].lower() in (".exr", ".hdr"):
                    items.append((f, f, "Light the look review with this HDRI"))
    _HDRI_ITEMS = items
    return _HDRI_ITEMS


def scaffold_surface_scene():
    """Set up a fresh surface (look-dev) file: a clean scene (no default
    cube/camera/light) in the Shading workspace with material-preview viewports.
    Called once at startup when the Workspace app opens an empty surface task."""
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    # Land in the Shading workspace (shader editor + material-preview viewport).
    ws = bpy.data.workspaces.get("Shading")
    for win in bpy.context.window_manager.windows:
        if ws is not None and win.workspace is not ws:
            win.workspace = ws
    # Belt-and-braces: make any 3D viewport show materials, in case there's no
    # Shading workspace (custom startup file).
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                for space in area.spaces:
                    if space.type == "VIEW_3D":
                        space.shading.type = "MATERIAL"


def _purge_orphan_data(data):
    """Remove a now-unused data-block (mesh/light/camera/…) so objects dropped
    during a selective append don't linger as orphans and ride into the next
    publish. Best-effort: tries each id collection until one accepts it."""
    if data is None or getattr(data, "users", 1) != 0:
        return
    for attr in ("meshes", "lights", "cameras", "curves", "metaballs",
                 "lattices", "grease_pencils_v3", "grease_pencils", "volumes",
                 "armatures"):
        coll = getattr(bpy.data, attr, None)
        if coll is None:
            continue
        try:
            coll.remove(data)
            return
        except (TypeError, RuntimeError, ReferenceError):
            continue


class LEGAMI_OT_load_model(bpy.types.Operator):
    bl_idname = "legami.load_model"
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
        model_blend = os.environ.get("LEGAMI_MODEL_PUBLISH")
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
            out = subprocess.check_output(cmd, cwd=td, text=True).strip()
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


_LOOK_CHOICES = []   # cached list-looks result for the apply dropdown


def _apply_look_items(self, context):
    items = [(l["look"], f"{l['look']}  (v{l['version']:03d})", "")
             for l in _LOOK_CHOICES]
    return items or [("", "<no looks published>", "")]


class LEGAMI_OT_apply_look(bpy.types.Operator):
    bl_idname = "legami.apply_look"
    bl_label = "Apply look"
    bl_description = ("Fetch a published look for this character and assign its "
                      "materials onto the meshes by name")

    look: bpy.props.EnumProperty(name="Look", items=_apply_look_items)

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "asset" or not task.get("entity"):
            self.report({"ERROR"}, "No active asset task.")
            return {"CANCELLED"}
        global _LOOK_CHOICES
        _LOOK_CHOICES = self._list_looks(look_mod.surface_task_id(task["entity"]))
        if not _LOOK_CHOICES:
            self.report({"ERROR"}, "No looks published for this character yet — "
                                   "publish one from the surface task first.")
            return {"CANCELLED"}
        self.look = _LOOK_CHOICES[0]["look"]
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Apply a published look to this character:")
        col.prop(self, "look")

    def execute(self, context):
        task = active_task()
        if not task or not task.get("entity") or not self.look:
            self.report({"ERROR"}, "No look selected.")
            return {"CANCELLED"}
        sid = look_mod.surface_task_id(task["entity"])
        blend = self._fetch_look(sid, self.look)
        if not blend or not os.path.isfile(blend):
            self.report({"ERROR"}, "Couldn't fetch the look from the server.")
            return {"CANCELLED"}
        try:
            manifest = json.load(open(blend[:-6] + ".manifest.json"))
        except Exception:  # noqa: BLE001
            manifest = {}
        mats = self._append_materials(blend)
        assigned, missing = self._assign(manifest.get("assignments", {}), mats)
        self._dedupe_material_names(mats)
        msg = f"Applied look '{self.look}': {assigned} mesh(es)"
        if missing:
            msg += f", {missing} not found in scene"
        self.report({"INFO"}, msg)
        return {"FINISHED"}

    # --- helpers ---------------------------------------------------------
    def _list_looks(self, surface_id):
        cmd, td = _toolkit_cmd(["list-looks", "--task", surface_id])
        if cmd is None:
            return []
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True)
            return json.loads(out.splitlines()[-1])
        except Exception:  # noqa: BLE001
            return []

    def _fetch_look(self, surface_id, look):
        cmd, td = _toolkit_cmd(
            ["fetch-look", "--task", surface_id, "--look", look])
        if cmd is None:
            return None
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True).strip()
            return out.splitlines()[-1] if out else None
        except Exception:  # noqa: BLE001
            return None

    def _append_materials(self, blend):
        # If the look IS the currently-open file (e.g. you opened the look library
        # itself), Blender can't append from it — its materials are already here.
        if (bpy.data.filepath
                and os.path.abspath(blend) == os.path.abspath(bpy.data.filepath)):
            return {m.name: m for m in bpy.data.materials}
        # Map ORIGINAL name -> appended datablock, so a name clash with an existing
        # scene material (renamed to .001 on append) doesn't break assignment.
        names = []
        with bpy.data.libraries.load(blend, link=False) as (src, dst):
            names = list(src.materials)          # keep the name strings separate
            dst.materials = list(src.materials)  # Blender fills this list in place
        return {nm: mat for nm, mat in zip(names, dst.materials) if mat is not None}

    def _dedupe_material_names(self, mats):
        """If the clean model brought its own same-named material, the look's
        appended copy gets a '.001' suffix. Once we've reassigned, the model's copy
        is orphaned — drop it and let the look's material reclaim the clean name."""
        for orig_name, mat in mats.items():
            if mat.name == orig_name:
                continue
            old = bpy.data.materials.get(orig_name)
            if old is not None and old is not mat and old.users == 0:
                bpy.data.materials.remove(old)
                try:
                    mat.name = orig_name
                except Exception:  # noqa: BLE001
                    pass

    def _assign(self, assignments, mats):
        assigned = missing = 0
        for mesh_name, slot_mats in assignments.items():
            obj = bpy.data.objects.get(mesh_name)
            if obj is None or obj.type != "MESH":
                missing += 1
                continue
            me = obj.data
            for i, mname in enumerate(slot_mats):
                mat = mats.get(mname) if mname else None
                if i < len(me.materials):
                    me.materials[i] = mat
                else:
                    me.materials.append(mat)
            assigned += 1
        return assigned, missing


class LEGAMI_OT_preview_turntable(bpy.types.Operator):
    bl_idname = "legami.preview_turntable"
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
            subprocess.Popen(cmd, cwd=td)   # non-blocking: keep working here
            self.report({"INFO"}, "Opening turntable preview… (close that window when done)")
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not start preview: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


ELEMENT_HOLDER_PREFIX = "element__"


def _element_holder(context, element_id):
    """The per-element scene collection (created if absent) that holds one element
    instance — unique per id, so two instances of the same asset never clash."""
    nm = ELEMENT_HOLDER_PREFIX + element_id
    holder = bpy.data.collections.get(nm)
    if holder is None:
        holder = bpy.data.collections.new(nm)
    if holder.name not in context.scene.collection.children:
        context.scene.collection.children.link(holder)
    return holder


def _link_asset_element(context, element):
    """LINK the asset's published collection and make a poseable library override,
    placed under the element's holder collection. Returns (holder, error)."""
    blend = element.get("blend_local")
    if not blend or not os.path.isfile(blend):
        return None, "publish not found locally"
    coll_name = element.get("collection") or ""
    holder = _element_holder(context, element["id"])

    # Link the named collection (fall back to the file's first collection for
    # pre-collection publishes).
    with bpy.data.libraries.load(blend, link=True, relative=True) as (src, dst):
        if coll_name and coll_name in src.collections:
            dst.collections = [coll_name]
        elif src.collections:
            dst.collections = [src.collections[0]]
        else:
            dst.collections = []
    linked = next((c for c in dst.collections if c is not None), None)
    if linked is None:
        return None, "no linkable collection (republish the rig/model)"

    # Build a full, editable override hierarchy so the rig is poseable.
    try:
        override = linked.override_hierarchy_create(
            context.scene, context.view_layer, do_fully_editable=True)
    except Exception as exc:  # noqa: BLE001
        return None, f"override failed: {exc}"

    # Relocate the override collection under the element holder.
    sc = context.scene.collection
    try:
        if override.name in sc.children:
            sc.children.unlink(override)
        if override.name not in holder.children:
            holder.children.link(override)
    except Exception:  # noqa: BLE001
        pass
    return holder, None


def _animated_paths(obj):
    """The set of data-paths that already have an F-curve on obj's action (handles
    both legacy and Blender 4.4+ slotted actions)."""
    ad = getattr(obj, "animation_data", None)
    act = getattr(ad, "action", None) if ad else None
    if not act:
        return set()
    paths = {fc.data_path for fc in getattr(act, "fcurves", []) or []}   # legacy
    for layer in getattr(act, "layers", []) or []:                      # slotted
        for strip in getattr(layer, "strips", []) or []:
            try:
                slot = ad.action_slot
                cbag = strip.channelbag(slot) if slot else None
            except Exception:  # noqa: BLE001
                cbag = None
            if cbag:
                paths.update(fc.data_path for fc in cbag.fcurves)
    return paths


def _snapshot_poses(context):
    """Before publishing, key every MOVED but un-keyed pose bone (and rig object) at
    the shot's start frame, so static offsets the artist changed without keyframing
    are captured in the Action and survive a rebuild. Channels that are already
    animated are left untouched. Returns the number of channels keyed."""
    scene = context.scene
    start = int(getattr(scene, "frame_start", 1001))
    prev = scene.frame_current
    scene.frame_set(start)
    rest = {"location": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0),
            "rotation_euler": (0.0, 0.0, 0.0),
            "rotation_quaternion": (1.0, 0.0, 0.0, 0.0)}
    keyed = 0

    def snap(target, prefix, animated):
        nonlocal keyed
        rot = ("rotation_quaternion"
               if getattr(target, "rotation_mode", "XYZ") == "QUATERNION"
               else "rotation_euler")
        for ch in ("location", rot, "scale"):
            path = (prefix + "." + ch) if prefix else ch
            if path in animated:                       # already animated — leave it
                continue
            if tuple(getattr(target, ch)) == rest[ch]:  # at rest — nothing to capture
                continue
            try:
                target.keyframe_insert(data_path=ch, frame=start)
                keyed += 1
            except Exception:  # noqa: BLE001
                pass

    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        for o in coll.all_objects:
            if getattr(o, "type", "") != "ARMATURE" or not getattr(o, "pose", None):
                continue
            o.animation_data_create()
            animated = _animated_paths(o)
            snap(o, "", animated)                       # the rig object itself
            for pb in o.pose.bones:
                snap(pb, 'pose.bones["%s"]' % pb.name, animated)
    scene.frame_set(prev)
    return keyed


def _collect_element_animation():
    """Gather each element's animation: the Action on every animated object inside an
    'element__*' holder. Returns (set_of_actions, {element_id: {obj_name: action_name}})
    for libraries.write + the manifest."""
    actions = set()
    elem_actions = {}
    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        eid = coll.name[len(ELEMENT_HOLDER_PREFIX):]
        mapping = {}
        for o in coll.all_objects:
            ad = getattr(o, "animation_data", None)
            act = getattr(ad, "action", None) if ad else None
            if act is not None:
                actions.add(act)
                mapping[o.name] = act.name
        if mapping:
            elem_actions[eid] = mapping
    return actions, elem_actions


def _apply_element_animation(holder, anim_blend, action_map):
    """Append the published Actions and assign them onto this element's objects by
    name, so a freshly-built element comes back animated."""
    if not (anim_blend and action_map and os.path.isfile(anim_blend)):
        return 0
    want = set(action_map.values())
    with bpy.data.libraries.load(anim_blend, link=False) as (src, dst):
        req_names = [a for a in src.actions if a in want]
        dst.actions = list(req_names)     # a SEPARATE copy — Blender fills dst.actions
                                          # with datablocks on exit; req_names must stay
                                          # the name strings (else the lookup below
                                          # keys on datablocks and never matches).
    # Map the REQUESTED name -> loaded datablock by order. Don't key on the loaded
    # action's .name: appending when an orphan of the same name exists (e.g. after
    # deleting the element in place) forces a '.001' suffix that wouldn't match the
    # manifest name. Same zip pattern as look material append.
    loaded = {name: blk for name, blk in zip(req_names, dst.actions)
              if blk is not None}
    applied = 0
    for o in holder.all_objects:
        act = loaded.get(action_map.get(o.name, ""))
        if act is None:
            continue
        o.animation_data_create()
        o.animation_data.action = act
        # Blender 4.4+ slotted actions: a slot must be bound to drive the object. It
        # auto-binds when the object name matches the action's slot; force the first
        # slot otherwise. (No-op on older Blender without slots.)
        try:
            ad = o.animation_data
            if getattr(ad, "action_slot", None) is None:
                slots = getattr(act, "slots", None)
                if slots and len(slots):
                    ad.action_slot = slots[0]
        except Exception:  # noqa: BLE001
            pass
        applied += 1
    return applied


def _build_camera_rig(context, element):
    """Build a fresh Dolly camera rig (Add Camera Rigs add-on) named after the shot
    and place it under the element holder — the layout artist's camera to animate.
    Only the armature + camera go into the holder; the add-on's WGT-* bone shapes
    stay in its hidden Widgets collection (they're shapes, not controls)."""
    holder = _element_holder(context, element["id"])
    name = element.get("camera_name") or "shot_camera"
    before_objs = set(bpy.data.objects)
    before_colls = set(bpy.data.collections)
    try:
        bpy.ops.object.build_camera_rig(mode="DOLLY")
    except Exception as exc:  # noqa: BLE001 — add-on missing/disabled
        return None, f"camera-rig add-on unavailable ({exc})"
    new_objs = [o for o in bpy.data.objects if o not in before_objs]
    new_colls = [c for c in bpy.data.collections if c not in before_colls]
    if not new_objs:
        return None, "camera rig build produced nothing"
    rig = next((o for o in new_objs if o.type == "ARMATURE"), None)
    cam = next((o for o in new_objs if o.type == "CAMERA"), None)

    # Relocate ONLY the rig + camera into the holder. The bone-shape widgets
    # (WGT-*) are deliberately left in the add-on's hidden Widgets collection — they
    # are not controls and moving them does nothing.
    for o in (rig, cam):
        if o is None:
            continue
        for c in list(o.users_collection):
            try:
                c.objects.unlink(o)
            except Exception:  # noqa: BLE001
                pass
        try:
            holder.objects.link(o)
        except Exception:  # noqa: BLE001
            pass

    # Tuck the add-on's new widget collection under the holder and keep it hidden,
    # so it doesn't clutter the scene root or invite stray clicks.
    sc = context.scene.collection
    for c in new_colls:
        try:
            if c.name in sc.children:
                sc.children.unlink(c)
                holder.children.link(c)
            c.hide_viewport = True
        except Exception:  # noqa: BLE001
            pass

    if rig is not None:
        rig.name = name
        if cam is not None:
            cam.name = name + "_Camera"
    if cam is not None:
        context.scene.camera = cam
    return holder, None


def _load_camera_element(context, element):
    """The shot's own camera. If layout published one, APPEND it (editable shot
    data); otherwise build a fresh Dolly camera rig named after the shot."""
    blend = element.get("blend_local")
    if blend and os.path.isfile(blend):
        holder = _element_holder(context, element["id"])
        with bpy.data.libraries.load(blend, link=False) as (src, dst):
            dst.objects = list(src.objects)
        cam = None
        for o in dst.objects:
            if o is None:
                continue
            if o.name not in holder.objects:
                try:
                    holder.objects.link(o)
                except Exception:  # noqa: BLE001
                    pass
            if getattr(o, "type", "") == "CAMERA" and cam is None:
                cam = o
        if cam is not None:
            context.scene.camera = cam
        return holder, None
    return _build_camera_rig(context, element)


_ELEMENT_LOADERS = {
    "asset": _link_asset_element,
    "camera": _load_camera_element,
    # LATER (lighting round): "cache": _link_alembic_cache,
}


# Shot frame range captured by the Build-shot dialog's invoke(), applied in
# execute() so the timeline matches the shot even when nothing new is built.
_BUILD_FRAME_RANGE = {"start": None, "end": None}


def _apply_build_frame_range(context):
    """Set the scene timeline to the captured shot range. Returns a short message
    (e.g. 'timeline 1001-1100') or '' if no range was captured."""
    fs, fe = _BUILD_FRAME_RANGE.get("start"), _BUILD_FRAME_RANGE.get("end")
    if not fs or not fe:
        return ""
    sc = context.scene
    sc.frame_start, sc.frame_end = int(fs), int(fe)
    if not (int(fs) <= sc.frame_current <= int(fe)):
        sc.frame_current = int(fs)
    return f"timeline {int(fs)}-{int(fe)}"


def _element_detail(el, present):
    """One-line description of what an element will bring in, for the dialog."""
    if present:
        return "already in scene"
    kind = el.get("kind")
    if kind == "camera":
        return ("new Dolly camera rig" if el.get("load") == "create_rig"
                else "shot camera (published)")
    src = el.get("source_step") or "?"
    return f"link {src}"


# Dynamic per-row step dropdown. The enum items are derived from each row's
# steps_csv; we cache the built lists (keyed by the csv) so the strings stay alive
# — Blender crashes if an items callback returns lists it can garbage-collect.
_STEP_ENUM_CACHE = {}


def _step_enum_items(self, context):
    key = self.steps_csv or ""
    if key not in _STEP_ENUM_CACHE:
        steps = [s for s in key.split(",") if s] or ["model"]
        _STEP_ENUM_CACHE[key] = [
            (s, s.capitalize(), f"Bring in the {s} publish") for s in steps]
    return _STEP_ENUM_CACHE[key]


class LEGAMI_AssemblyItem(bpy.types.PropertyGroup):
    """One row in the Build-shot dialog: an element, which step to bring in, and
    whether to build it."""
    enabled: bpy.props.BoolProperty(name="Build", default=True)
    label: bpy.props.StringProperty()
    kind: bpy.props.StringProperty()
    detail: bpy.props.StringProperty()
    present: bpy.props.BoolProperty(default=False)
    steps_csv: bpy.props.StringProperty()    # available steps, comma-separated
    step: bpy.props.EnumProperty(name="Step", items=_step_enum_items,
                                 description="Which published step to bring in")
    payload: bpy.props.StringProperty()      # json of the resolved element


class LEGAMI_OT_build_shot(bpy.types.Operator):
    bl_idname = "legami.build_shot"
    bl_label = "Build shot"
    bl_description = ("Bring this shot's breakdown into the scene: link each chosen "
                      "element's rig as a poseable override and build the shot "
                      "camera. Additive — elements already in the scene are left "
                      "untouched, so your posing/animation is never lost")

    # The per-element rows live on the WindowManager (legami_build_items) — an
    # operator-owned CollectionProperty doesn't reliably populate the props dialog.

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "shot" or not task.get("entity"):
            self.report({"ERROR"}, "No active shot task — open a shot's layout task "
                                   "from the Workspace app.")
            return {"CANCELLED"}
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save into the task first (Legami ▸ Save into "
                                   "task) — linked rigs need the shot file on disk "
                                   "to store relative paths.")
            return {"CANCELLED"}

        data = self._resolve(task, list_only=True)      # preview, no downloads
        if data is None:
            self.report({"ERROR"}, "Couldn't resolve the shot assembly — launch from "
                                   "the Workspace app and check your connection.")
            return {"CANCELLED"}
        _BUILD_FRAME_RANGE["start"] = data.get("frame_start")
        _BUILD_FRAME_RANGE["end"] = data.get("frame_end")
        listed = data.get("elements") or []
        if not listed:
            # No elements yet, but still set the shot's timeline from its range.
            msg = _apply_build_frame_range(context)
            self.report({"INFO"} if msg else {"WARNING"},
                        f"No elements yet — {msg}." if msg
                        else "Shot has no elements yet. Add them in the Workspace "
                             "app (right-click the shot ▸ Elements…).")
            return {"FINISHED"} if msg else {"CANCELLED"}

        existing = {c.name for c in bpy.data.collections}
        rows = context.window_manager.legami_build_items
        rows.clear()
        for el in listed:
            it = rows.add()
            it.payload = json.dumps(el)
            it.kind = el.get("kind", "asset")
            it.label = el.get("label") or el.get("id", "")
            it.present = ELEMENT_HOLDER_PREFIX + str(el.get("id", "")) in existing
            it.enabled = not it.present          # default: build the new ones
            it.detail = _element_detail(el, it.present)
            steps = el.get("available_steps") or []
            it.steps_csv = ",".join(steps)
            if steps and el.get("source_step") in steps:
                it.step = el["source_step"]      # default to the resolved step
        return context.window_manager.invoke_props_dialog(
            self, width=480, title="Build shot", confirm_text="Build")

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Bring these elements into the shot:")
        box = col.box()
        for it in context.window_manager.legami_build_items:
            row = box.row(align=True)
            cb = row.row()
            cb.enabled = not it.present          # present ones can't be re-built here
            cb.prop(it, "enabled", text="")
            icon = ("CHECKMARK" if it.present
                    else "OUTLINER_OB_CAMERA" if it.kind == "camera"
                    else "OUTLINER_OB_ARMATURE")
            row.label(text=it.label, icon=icon)
            if it.present:
                row.label(text="already in scene")
            elif it.kind == "camera":
                row.label(text=it.detail)
            else:
                # asset: a step dropdown (rig/model/…) so you control what's linked.
                sub = row.row()
                sub.enabled = it.enabled
                sub.prop(it, "step", text="")

    def execute(self, context):
        task = active_task()
        if not task:
            return {"CANCELLED"}
        chosen, picks, present_ct, deselected_ct = [], {}, 0, 0
        for it in context.window_manager.legami_build_items:
            if it.present:
                present_ct += 1
            elif it.enabled:
                eid = json.loads(it.payload)["id"]
                chosen.append(eid)
                if it.kind == "asset" and it.step:   # honour the chosen step
                    picks[eid] = it.step
            else:
                deselected_ct += 1

        # Always set the shot's timeline to its frame range, even if nothing new
        # is built (e.g. everything already present).
        tl_msg = _apply_build_frame_range(context)
        if not chosen:
            extra = f" — {tl_msg}" if tl_msg else ""
            self.report({"INFO"},
                        f"Nothing to build ({present_ct} already in scene){extra}.")
            return {"FINISHED"}

        # downloads only the chosen, at their chosen steps
        data = self._resolve(task, only=chosen, picks=picks)
        elements = (data or {}).get("elements")
        if not elements:
            self.report({"ERROR"}, "Couldn't fetch the selected elements — check "
                                   "your connection and retry.")
            return {"CANCELLED"}
        anim = (data or {}).get("anim") or {}
        anim_blend = anim.get("blend_local")
        anim_elements = anim.get("elements") or {}

        built, skipped, animated = [], [], 0
        for el in elements:
            loader = _ELEMENT_LOADERS.get(el.get("kind"))
            if loader is None:
                skipped.append((el, "unsupported kind"))
                continue
            try:
                holder, err = loader(context, el)
            except Exception as exc:  # noqa: BLE001 — one bad element never kills it
                holder, err = None, str(exc)
            (built if holder else skipped).append((el, err))
            # Re-apply this element's published animation onto the fresh rig.
            amap = anim_elements.get(el.get("id"))
            if holder and anim_blend and amap:
                try:
                    animated += _apply_element_animation(holder, anim_blend, amap)
                except Exception as exc:  # noqa: BLE001
                    print("[Legami] could not apply animation:", exc)

        # Store linked-library paths relative to the shot .blend (cross-machine).
        try:
            bpy.ops.file.make_paths_relative()
        except Exception:  # noqa: BLE001
            pass

        parts = [f"Built {len(built)} element(s)"]
        if animated:
            parts.append(f"re-applied animation to {animated} object(s)")
        if tl_msg:
            parts.append(tl_msg)
        if present_ct:
            parts.append(f"{present_ct} already in scene")
        if deselected_ct:
            parts.append(f"{deselected_ct} not selected")
        if skipped:
            parts.append("skipped " + ", ".join(
                f"{e.get('id', '?')} ({err})" for e, err in skipped))
        self.report({"INFO"} if built else {"WARNING"}, "; ".join(parts))
        return {"FINISHED"} if built else {"CANCELLED"}

    def _resolve(self, task, list_only=False, only=None, picks=None):
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
            out = subprocess.check_output(cmd, cwd=td, text=True).strip()
            return json.loads(out.splitlines()[-1]) if out else []
        except Exception:  # noqa: BLE001
            return None


CLASSES = (
    LEGAMI_OT_apply_project_settings,
    LEGAMI_OT_verify_ocio,
    LEGAMI_OT_pull_settings,
    LEGAMI_OT_add_locator,
    LEGAMI_OT_save_to_task,
    LEGAMI_OT_check,
    LEGAMI_OT_publish,
    LEGAMI_OT_load_model,
    LEGAMI_OT_apply_look,
    LEGAMI_AssemblyItem,            # PropertyGroup — register before the operator
    LEGAMI_OT_build_shot,
    LEGAMI_OT_turntable_framing,
    LEGAMI_OT_preview_turntable,
)
