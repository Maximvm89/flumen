"""Blender operators for the Legami pipeline addon."""

import os
import subprocess

import bpy

from . import settings_io
from . import checks


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
    cmd, td = _toolkit_cmd(["next-version", "--task", task_id])
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
        self._issues = checks.run_checks(step, context.scene,
                                         list(context.scene.objects),
                                         publish_locator_name())
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        _draw_checks(self.layout, self._issues)
        if checks.has_errors(self._issues):
            self.layout.label(text="Errors would block a publish.", icon="CANCEL")

    def execute(self, context):
        return {"FINISHED"}  # informational only


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
        self._issues = checks.run_checks(task["step"], context.scene,
                                         list(context.scene.objects),
                                         publish_locator_name())
        return context.window_manager.invoke_props_dialog(
            self, width=480, title="Publish", confirm_text="Publish")

    def draw(self, context):
        col = self.layout.column()
        col.prop(context.window_manager, "legami_publish_desc", text="Description")
        task = active_task()
        if task and task.get("step") == "model":
            col.prop(context.window_manager, "legami_render_turntable")
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

        issues = checks.run_checks(task["step"], context.scene,
                                   list(context.scene.objects),
                                   publish_locator_name())
        if checks.has_errors(issues):
            errs = [m for lvl, m in issues if lvl == checks.ERROR]
            self.report({"ERROR"}, "Publish blocked: " + errs[0])
            print("[Legami] publish blocked:\n  " + "\n  ".join(errs))
            return {"CANCELLED"}

        publish_dir = os.path.join(os.path.dirname(task["work_dir"]), "publish")
        os.makedirs(publish_dir, exist_ok=True)
        name = task["entity"].split("/")[-1]
        base = f"{name}_{task['step']}"
        # The server publish history is the single source of truth for versions.
        # If we can't reach it, abort rather than guess a number that could collide.
        version = _server_next_version(task["id"], base)
        if not version:
            self.report({"ERROR"}, "Couldn't reach the server to determine the next "
                        "version — publish aborted. Check your connection and retry.")
            return {"CANCELLED"}
        pub_path = os.path.join(publish_dir, f"{base}_v{version:03d}.blend")

        bpy.ops.wm.save_as_mainfile(filepath=pub_path, copy=True)
        files = [pub_path]
        fbx_path = pub_path[:-6] + ".fbx"   # .blend -> .fbx
        # Export only the geometry under the publish locator, if present.
        loc = bpy.data.objects.get(publish_locator_name())
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

        pub_cmd, td = _toolkit_cmd(
            ["publish", "--local", *files, "--task", task["id"],
             "--status", "review",
             "--description", context.window_manager.legami_publish_desc])
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

        # Optionally kick off a turntable render in the BACKGROUND (non-blocking).
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

        warns = sum(1 for lvl, _ in issues if lvl == checks.WARNING)
        suffix = f" ({warns} warning(s))" if warns else ""
        self.report({"INFO"}, f"Published {base}_v{version:03d} (.blend + FBX); "
                              f"task → Review.{suffix}{tt_msg}")
        return {"FINISHED"}


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


CLASSES = (
    LEGAMI_OT_apply_project_settings,
    LEGAMI_OT_verify_ocio,
    LEGAMI_OT_pull_settings,
    LEGAMI_OT_add_locator,
    LEGAMI_OT_save_to_task,
    LEGAMI_OT_check,
    LEGAMI_OT_publish,
    LEGAMI_OT_turntable_framing,
    LEGAMI_OT_preview_turntable,
)
