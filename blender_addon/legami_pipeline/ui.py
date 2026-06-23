"""Legami menu in Blender's top menu bar (next to Help)."""

import os

import bpy

from . import operators as _ops


class LEGAMI_MT_menu(bpy.types.Menu):
    bl_label = "Legami"
    bl_idname = "LEGAMI_MT_menu"

    def draw(self, context):
        layout = self.layout
        task = _ops.active_task()
        if task:
            layout.label(text=f"Task: {task['entity']}  ·  {task['step']}",
                         icon="OUTLINER_OB_ARMATURE")
            layout.operator("legami.save_to_task", icon="FILE_TICK")
            layout.operator("legami.run_checks", icon="CHECKMARK")
            layout.operator("legami.publish", text="Publish…", icon="EXPORT")
            layout.separator()
        else:
            layout.label(text="No active task (open from Workspace app)",
                         icon="INFO")
            layout.separator()

        layout.operator("legami.apply_project_settings", icon="CHECKMARK")
        layout.operator("legami.verify_ocio", icon="COLOR")
        layout.separator()
        layout.operator("legami.pull_settings", icon="IMPORT")

        ocio = os.environ.get("BLENDER_OCIO")
        layout.separator()
        layout.label(text="OCIO: " + ("loaded" if ocio else "NOT set — use launcher"),
                     icon="DOT" if ocio else "ERROR")


def draw_menu(self, context):
    self.layout.menu("LEGAMI_MT_menu")


CLASSES = (LEGAMI_MT_menu,)
