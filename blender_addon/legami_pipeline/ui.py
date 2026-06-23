"""N-panel UI for the Legami pipeline addon (View3D > Sidebar > Legami)."""

from __future__ import annotations

import os

import bpy

from . import settings_io


class LEGAMI_PT_panel(bpy.types.Panel):
    bl_label = "Legami Pipeline"
    bl_idname = "LEGAMI_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Legami"

    def draw(self, context):
        layout = self.layout
        prefs = context.preferences.addons[__package__].preferences
        root = settings_io.find_project_root(prefs.local_root)

        box = layout.box()
        box.label(text="Project Setup", icon="TOOL_SETTINGS")
        if root and os.path.isfile(settings_io.settings_path(root)):
            box.label(text=f"Root: {os.path.basename(root)}", icon="CHECKMARK")
        elif root:
            box.label(text="settings file missing — pull from FTP", icon="ERROR")
        else:
            box.label(text="No project root — use launcher", icon="ERROR")

        col = box.column(align=True)
        col.scale_y = 1.3
        col.operator("legami.apply_project_settings", icon="CHECKMARK")
        col.operator("legami.verify_ocio", icon="COLOR")

        box = layout.box()
        box.label(text="Sync", icon="FILE_REFRESH")
        box.operator("legami.pull_settings", icon="IMPORT")

        ocio = os.environ.get("BLENDER_OCIO")
        sub = layout.box()
        sub.label(text="Status", icon="INFO")
        sub.label(text=f"OCIO: {'set' if ocio else 'NOT set (bundled)'}",
                  icon="DOT" if ocio else "ERROR")


CLASSES = (LEGAMI_PT_panel,)
