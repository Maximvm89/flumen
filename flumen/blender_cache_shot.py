"""Headless 'Cache shot' entry — run by Blender AFTER blender_bootstrap.py
(which registers the add-on). Runs at module level, i.e. once the file is
loaded and bpy.data is fully accessible (unlike register(), where it's
restricted, and unlike app.timers, which never fire in background -b mode).

Builds the shot from published data and bakes + publishes the caches for the
elements named in FLUMEN_CACHE_ONLY, then exits with the result code so the
Workspace app can report success/failure.
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
        print("[Flumen] cache: add-on not loaded — cannot cache.")
        return 2
    try:
        ops.enable_project_addons()      # camera rig etc., for the build
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] cache: add-on enable skipped:", exc)
    try:
        return ops.headless_build_and_cache()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("[Flumen] headless cache failed:", exc)
        return 1


sys.exit(main())
