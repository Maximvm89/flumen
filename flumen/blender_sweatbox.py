"""Headless 'Sweatbox build' entry — run by Blender AFTER blender_bootstrap.py
(which registers the add-on). Runs at module level, once bpy.data is fully
accessible (unlike register(), and unlike app.timers which never fire in -b).

Builds the shot fresh from PUBLISHED data (latest rigs + latest animation +
environment with set-dressing) on a clean scene and SAVES it to
FLUMEN_SWEATBOX_OUT. The Workspace app then renders that copy with the
Material-Preview playblast (flumen.playblast run_playblast sweatbox=True). Two
processes on purpose: this one needs the add-on bootstrapped to build; the
render reuses the standard playblast plumbing on a plain .blend.
"""

import sys

import bpy


def _ops_module():
    """The add-on's operators module, whether loaded from source
    (flumen_pipeline) or as a 4.2+ extension (bl_ext.<repo>.flumen_pipeline)."""
    try:
        from flumen_pipeline import operators as ops
        return ops
    except Exception:  # noqa: BLE001
        for name, mod in list(sys.modules.items()):
            if name.endswith("flumen_pipeline.operators"):
                return mod
    return None


def main():
    ops = _ops_module()
    if ops is None:
        print("[Flumen] sweatbox: add-on not loaded — cannot build.")
        return 2
    try:
        ops.enable_project_addons()      # camera rig etc., for the build
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] sweatbox: add-on enable skipped:", exc)
    try:
        return ops.headless_build_and_save()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("[Flumen] headless sweatbox build failed:", exc)
        return 1


sys.exit(main())
