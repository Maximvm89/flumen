"""Flumen menu in Blender's top menu bar (next to Help)."""

import os

import bpy

from . import operators as _ops


class FLUMEN_MT_menu(bpy.types.Menu):
    bl_label = "Flumen"
    bl_idname = "FLUMEN_MT_menu"

    def draw(self, context):
        layout = self.layout
        task = _ops.active_task()
        if task:
            layout.label(text=f"Task: {task['entity']}  ·  {task['step']}",
                         icon="OUTLINER_OB_ARMATURE")
            # Surface/rig depend on the published model — pull it in to work on.
            if task.get("step") in ("surface", "rig"):
                layout.operator("flumen.load_model", icon="IMPORT")
            # Set-dressing: link the environment, then place published props.
            if task.get("step") == "dressing":
                layout.operator("flumen.build_dressing", text="Load environment",
                                icon="WORLD")
                layout.operator("flumen.add_prop", text="Add prop…",
                                icon="OUTLINER_OB_GROUP_INSTANCE")
            # Re-apply a published look onto the character (rig and beyond).
            if task.get("type") == "asset" and task.get("step") != "model":
                layout.operator("flumen.apply_look", text="Apply look…",
                                icon="MATERIAL")
            # Shot layout: assemble the breakdown (link each element's rig, build
            # the shot camera). Additive — safe to re-run to pull in new elements.
            if task.get("type") == "shot" and task.get("step") == "layout":
                layout.operator("flumen.build_shot", text="Build shot",
                                icon="OUTLINER_OB_GROUP_INSTANCE")
            # Load published animation onto the shot's elements (pick a version each).
            if task.get("type") == "shot":
                layout.operator("flumen.load_animation", text="Load animation…",
                                icon="ANIM_DATA")
            layout.operator("flumen.add_review_camera", icon="VIEW_CAMERA")
            layout.operator("flumen.render_review", icon="RENDER_STILL")
            layout.operator("flumen.save_to_task", icon="FILE_TICK")
            layout.operator("flumen.run_checks", icon="CHECKMARK")
            layout.operator("flumen.auto_fix", icon="TOOL_SETTINGS")
            layout.operator("flumen.publish", text="Publish…", icon="EXPORT")
            layout.separator()
        else:
            layout.label(text="No active task (open from Workspace app)",
                         icon="INFO")
            layout.separator()

        # Asset/modelling tools (publish locator + turntable preview) — these don't
        # belong in a shot, so hide them when a shot task is open.
        if not (task and task.get("type") == "shot"):
            layout.operator("flumen.add_publish_locator", icon="EMPTY_AXIS")
            layout.operator("flumen.preview_turntable", icon="CAMERA_DATA")
            layout.separator()

        layout.operator("flumen.apply_project_settings", icon="CHECKMARK")
        layout.operator("flumen.verify_ocio", icon="COLOR")
        layout.separator()
        layout.operator("flumen.pull_settings", icon="IMPORT")
        layout.operator("flumen.show_log", icon="TEXT")

        ocio = os.environ.get("BLENDER_OCIO")
        layout.separator()
        layout.label(text="OCIO: " + ("loaded" if ocio else "NOT set — use launcher"),
                     icon="DOT" if ocio else "ERROR")


def draw_menu(self, context):
    self.layout.menu("FLUMEN_MT_menu")


class FLUMEN_PT_turntable(bpy.types.Panel):
    """Per-asset turntable framing, in the 3D-view sidebar (N) > Flumen."""
    bl_label = "Turntable"
    bl_idname = "FLUMEN_PT_turntable"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Flumen"

    @classmethod
    def poll(cls, context):
        # Turntable framing is per-asset — hide the panel in a shot context.
        task = _ops.active_task()
        return not (task and task.get("type") == "shot")

    def draw(self, context):
        layout = self.layout
        loc = _ops.active_publish_locator()
        if not loc:
            layout.label(text="No PUBLISH locator yet", icon="INFO")
            layout.operator("flumen.add_publish_locator", icon="EMPTY_AXIS")
            return
        if loc.get("flumen_tt_override"):
            mode = loc.get("flumen_tt_fit_mode", "box")
            scale = float(loc.get("flumen_tt_fit_scale", 1.0))
            layout.label(text=f"{mode} @ {scale:.2f}x", icon="CHECKMARK")
        else:
            layout.label(text="Using project default", icon="DOT")
        layout.operator("flumen.turntable_framing", text="Set Framing…", icon="MOD_LENGTH")
        layout.operator("flumen.preview_turntable", icon="CAMERA_DATA")


CLASSES = (FLUMEN_MT_menu, FLUMEN_PT_turntable)
