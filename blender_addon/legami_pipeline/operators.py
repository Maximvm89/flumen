"""Blender operators for the Legami pipeline addon."""

from __future__ import annotations

import os
import subprocess
import sys

import bpy

from . import settings_io


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


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
        prefs = _prefs()
        root = settings_io.find_project_root(prefs.local_root)
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
        prefs = _prefs()
        root = settings_io.find_project_root(prefs.local_root)
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


class LEGAMI_OT_install_deps(bpy.types.Operator):
    bl_idname = "legami.install_deps"
    bl_label = "Install Dependencies (paramiko)"
    bl_description = "Install paramiko into Blender's user modules for FTP operations (needs internet)"

    def execute(self, context):
        target = bpy.utils.user_resource("SCRIPTS", path="modules", create=True)
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip"])
        except Exception:  # noqa: BLE001 — ensurepip may already be present
            pass
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "--upgrade", "paramiko", "--target", target])
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"pip install failed: {exc}")
            return {"CANCELLED"}
        if target not in sys.path:
            sys.path.append(target)
        self.report({"INFO"}, "paramiko installed. FTP features are now available.")
        return {"FINISHED"}


class LEGAMI_OT_pull_settings(bpy.types.Operator):
    bl_idname = "legami.pull_settings"
    bl_label = "Pull Latest From FTP"
    bl_description = "Download the latest project_settings.json and OCIO config from the FTP using your login"

    def execute(self, context):
        prefs = _prefs()
        try:
            import paramiko  # noqa: F401
        except ImportError:
            self.report({"ERROR"},
                        "paramiko not installed. Use 'Install Dependencies' first.")
            return {"CANCELLED"}

        if not (prefs.sftp_host and prefs.sftp_user):
            self.report({"ERROR"}, "Set SFTP host and user in addon preferences.")
            return {"CANCELLED"}

        root = settings_io.find_project_root(prefs.local_root)
        if not root:
            self.report({"ERROR"}, "Set Local Project Root in addon preferences.")
            return {"CANCELLED"}

        import paramiko
        try:
            t = paramiko.Transport((prefs.sftp_host, int(prefs.sftp_port or 22)))
            t.connect(username=prefs.sftp_user, password=prefs.sftp_password or None)
            sftp = paramiko.SFTPClient.from_transport(t)
            remote_pipeline = prefs.remote_root.rstrip("/") + "/02_pipeline"
            _download_dir(sftp, remote_pipeline, os.path.join(root, "02_pipeline"))
            sftp.close()
            t.close()
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"FTP sync failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, "Pulled latest pipeline config. Now Apply Project Settings.")
        return {"FINISHED"}


def _download_dir(sftp, remote_dir, local_dir):
    import stat as _stat
    os.makedirs(local_dir, exist_ok=True)
    for entry in sftp.listdir_attr(remote_dir):
        rpath = remote_dir + "/" + entry.filename
        lpath = os.path.join(local_dir, entry.filename)
        if _stat.S_ISDIR(entry.st_mode):
            _download_dir(sftp, rpath, lpath)
        else:
            sftp.get(rpath, lpath)


CLASSES = (
    LEGAMI_OT_apply_project_settings,
    LEGAMI_OT_verify_ocio,
    LEGAMI_OT_install_deps,
    LEGAMI_OT_pull_settings,
)
