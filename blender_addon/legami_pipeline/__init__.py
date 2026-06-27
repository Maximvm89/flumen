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

import os

import bpy

from . import prefs as _prefs
from . import operators as _ops
from . import ui as _ui

_ALL_CLASSES = (_prefs.LegamiPipelinePrefs, *_ops.CLASSES, *_ui.CLASSES)


def _surface_startup():
    """One-shot: scaffold a clean shading scene for a fresh surface task. Runs on
    a timer so the window/screen are ready before we switch workspaces."""
    try:
        _ops.scaffold_surface_scene()
    except Exception as exc:  # noqa: BLE001
        print("[Legami] surface scene setup failed:", exc)
    return None   # don't repeat


def _color_startup():
    """One-shot: align the opened file's color management with the project OCIO."""
    try:
        _ops.apply_project_color()
    except Exception as exc:  # noqa: BLE001
        print("[Legami] color management setup skipped:", exc)
    return None   # don't repeat


def _addons_startup():
    """One-shot: enable the project's extra add-ons (e.g. Add Camera Rigs)."""
    try:
        _ops.enable_project_addons()
    except Exception as exc:  # noqa: BLE001
        print("[Legami] add-on enable skipped:", exc)
    return None   # don't repeat


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
    bpy.types.WindowManager.legami_look_name = bpy.props.StringProperty(
        name="Look", default="default",
        description="Name of this look variant (e.g. default, damaged) — pick an "
                    "existing one to publish a new version, or type a new name. Each "
                    "name versions independently and is selectable downstream",
        search=_ops.look_name_search)
    bpy.types.WindowManager.legami_lookdev_hdri = bpy.props.EnumProperty(
        name="Review HDRI", items=_ops.lookdev_hdri_items,
        description="HDRI to light the look review turntable (from 05_library/hdri)")
    # Add a "Legami" menu to the top menu bar (next to Help).
    bpy.types.TOPBAR_MT_editor_menus.append(_ui.draw_menu)
    # Fresh surface task: start from a clean, shading-ready scene.
    if os.environ.get("LEGAMI_NEW_SURFACE"):
        bpy.app.timers.register(_surface_startup, first_interval=0.1)
    # Launched from the Workspace app: align color management with the project
    # OCIO so default-config files (sRGB/AgX) stop warning. Self-heals on save.
    if os.environ.get("LEGAMI_PROJECT_ROOT"):
        bpy.app.timers.register(_color_startup, first_interval=0.2)
        # Make the project's extra add-ons available (install if needed + enable;
        # e.g. Add Camera Rigs for layout).
        bpy.app.timers.register(_addons_startup, first_interval=0.1)


def unregister():
    bpy.types.TOPBAR_MT_editor_menus.remove(_ui.draw_menu)
    del bpy.types.WindowManager.legami_publish_desc
    del bpy.types.WindowManager.legami_render_turntable
    del bpy.types.WindowManager.legami_look_name
    del bpy.types.WindowManager.legami_lookdev_hdri
    for cls in reversed(_ALL_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
