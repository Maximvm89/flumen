"""Legami Pipeline — Blender addon.

First tool: project initialization. Pulls the project's standard settings from
the studio FTP (via the launcher's sync, or the in-addon Pull button) and applies
identical color management, render, units and output settings to every artist's
scene. Per-user SFTP login lives in the addon preferences for future publish tools.

Install: Edit > Preferences > Add-ons > Install... and pick the zipped
'legami_pipeline' folder. Then open the sidebar in the 3D view (press N) >
'Legami' tab.
"""

bl_info = {
    "name": "Legami Pipeline",
    "author": "Legami Pipeline",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "Top bar > Legami menu",
    "description": "Project init: pull and apply standard project settings + OCIO.",
    "category": "Pipeline",
}

import bpy

from . import prefs as _prefs
from . import operators as _ops
from . import ui as _ui

_ALL_CLASSES = (_prefs.LegamiPipelinePrefs, *_ops.CLASSES, *_ui.CLASSES)


def register():
    for cls in _ALL_CLASSES:
        bpy.utils.register_class(cls)
    # Description typed in the publish dialog (persists across re-opens).
    bpy.types.WindowManager.legami_publish_desc = bpy.props.StringProperty(
        name="Description", default="",
        description="What changed in this publish (recorded in the task history)")
    bpy.types.WindowManager.legami_render_turntable = bpy.props.BoolProperty(
        name="Render turntable", default=True,
        description="After publishing a model, render a turntable video to dailies")
    bpy.types.WindowManager.legami_upload_syncsketch = bpy.props.BoolProperty(
        name="Upload to SyncSketch", default=True,
        description="Also push the turntable to SyncSketch for review")
    # Add a "Legami" menu to the top menu bar (next to Help).
    bpy.types.TOPBAR_MT_editor_menus.append(_ui.draw_menu)


def unregister():
    bpy.types.TOPBAR_MT_editor_menus.remove(_ui.draw_menu)
    del bpy.types.WindowManager.legami_publish_desc
    del bpy.types.WindowManager.legami_render_turntable
    del bpy.types.WindowManager.legami_upload_syncsketch
    for cls in reversed(_ALL_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
