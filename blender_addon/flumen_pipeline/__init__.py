"""Flumen Pipeline — Blender addon.

First tool: project initialization. Pulls the project's standard settings from
the studio FTP (via the launcher's sync, or the in-addon Pull button) and applies
identical color management, render, units and output settings to every artist's
scene. Per-user SFTP login lives in the addon preferences for future publish tools.

Install: Edit > Preferences > Add-ons > Install... and pick the zipped
'flumen_pipeline' folder. Then open the sidebar in the 3D view (press N) >
'Flumen' tab.
"""

bl_info = {
    "name": "Flumen Pipeline",
    "author": "Flumen Pipeline",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "Top bar > Flumen menu",
    "description": "Project init: pull and apply standard project settings + OCIO.",
    "category": "Pipeline",
}

import os

import bpy

from . import prefs as _prefs
from . import operators as _ops
from . import ui as _ui

_ALL_CLASSES = (_prefs.FlumenPipelinePrefs, *_ops.CLASSES, *_ui.CLASSES)


def _surface_startup():
    """One-shot: scaffold a clean shading scene for a fresh surface task. Runs on
    a timer so the window/screen are ready before we switch workspaces."""
    try:
        _ops.scaffold_surface_scene()
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] surface scene setup failed:", exc)
    return None   # don't repeat


def _empty_startup():
    """One-shot: 'Start working (new scene)' begins with a truly empty scene
    (no default Cube/Camera/Light)."""
    try:
        _ops.scaffold_empty_scene()
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] empty scene setup skipped:", exc)
    return None   # don't repeat


def _color_startup():
    """One-shot: align the opened file's color management with the project OCIO."""
    try:
        _ops.apply_project_color()
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] color management setup skipped:", exc)
    return None   # don't repeat


def _addons_startup():
    """One-shot: enable the project's extra add-ons (e.g. Add Camera Rigs)."""
    try:
        _ops.enable_project_addons()
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] add-on enable skipped:", exc)
    return None   # don't repeat


def register():
    for cls in _ALL_CLASSES:
        bpy.utils.register_class(cls)
    # Description typed in the publish dialog (persists across re-opens).
    bpy.types.WindowManager.flumen_publish_desc = bpy.props.StringProperty(
        name="Description", default="",
        description="What changed in this publish (recorded in the task history)")
    bpy.types.WindowManager.flumen_render_turntable = bpy.props.BoolProperty(
        name="Render turntable", default=True,
        description="After publishing a model, render a turntable video to dailies")
    bpy.types.WindowManager.flumen_look_name = bpy.props.StringProperty(
        name="Look", default="default",
        description="Name of this look variant (e.g. default, damaged) — pick an "
                    "existing one to publish a new version, or type a new name. Each "
                    "name versions independently and is selectable downstream",
        search=_ops.look_name_search)
    bpy.types.WindowManager.flumen_lookdev_hdri = bpy.props.EnumProperty(
        name="Review HDRI", items=_ops.lookdev_hdri_items,
        description="HDRI to light the look review turntable (from 05_library/hdri)")
    bpy.types.WindowManager.flumen_apply_modifiers = bpy.props.BoolProperty(
        name="Apply modifiers in publish", default=False,
        description="Bake the modifier stack into the published copy (your work "
                    "file keeps them live). Armature-deformed meshes are skipped. "
                    "Recommended for heavy static environments")
    bpy.types.WindowManager.flumen_dressing_name = bpy.props.StringProperty(
        name="Dressing", default="default",
        description="Name of this set-dressing variant (e.g. night_market) — pick "
                    "an existing one to publish a new version, or type a new name. "
                    "Each name versions independently and is selectable per shot",
        search=_ops.dressing_name_search)
    # Per-element rows for the Build-shot dialog (operator-owned collections don't
    # reliably populate a props dialog, so they live on the WindowManager).
    bpy.types.WindowManager.flumen_build_items = bpy.props.CollectionProperty(
        type=_ops.FLUMEN_AssemblyItem)
    # Per-element rows for the Load-animation dialog.
    bpy.types.WindowManager.flumen_anim_items = bpy.props.CollectionProperty(
        type=_ops.FLUMEN_AnimItem)
    # Per-element rows for the shot publish dialog (which animation to publish).
    bpy.types.WindowManager.flumen_publish_items = bpy.props.CollectionProperty(
        type=_ops.FLUMEN_PublishItem)
    # Add a "Flumen" menu to the top menu bar (next to Help).
    bpy.types.TOPBAR_MT_editor_menus.append(_ui.draw_menu)
    # Fresh surface task: start from a clean, shading-ready scene.
    if os.environ.get("FLUMEN_NEW_SURFACE"):
        bpy.app.timers.register(_surface_startup, first_interval=0.1)
    # 'Start working (new scene)': drop the default Cube/Camera/Light.
    elif os.environ.get("FLUMEN_NEW_SCENE"):
        bpy.app.timers.register(_empty_startup, first_interval=0.1)
    # Launched from the Workspace app: align color management with the project
    # OCIO so default-config files (sRGB/AgX) stop warning. Self-heals on save.
    if os.environ.get("FLUMEN_PROJECT_ROOT"):
        bpy.app.timers.register(_color_startup, first_interval=0.2)
        # Make the project's extra add-ons available (install if needed + enable;
        # e.g. Add Camera Rigs for layout).
        bpy.app.timers.register(_addons_startup, first_interval=0.1)


def unregister():
    bpy.types.TOPBAR_MT_editor_menus.remove(_ui.draw_menu)
    del bpy.types.WindowManager.flumen_publish_desc
    del bpy.types.WindowManager.flumen_render_turntable
    del bpy.types.WindowManager.flumen_look_name
    del bpy.types.WindowManager.flumen_lookdev_hdri
    del bpy.types.WindowManager.flumen_apply_modifiers
    del bpy.types.WindowManager.flumen_dressing_name
    del bpy.types.WindowManager.flumen_build_items
    del bpy.types.WindowManager.flumen_anim_items
    del bpy.types.WindowManager.flumen_publish_items
    for cls in reversed(_ALL_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
