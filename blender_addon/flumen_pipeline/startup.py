"""Session startup + scene-scaffolding helpers.

The functions __init__.py runs (on a timer) when Blender is launched from the
Workspace app: enabling the project's extra add-ons, aligning colour management,
and scaffolding a clean scene for a new task. Plus _purge_orphan_data, used when
dropping stray data-blocks after a selective append.

Extracted from operators.py; the public names are re-imported there so
__init__'s `_ops.<name>` startup hooks keep resolving.
"""

import os

import bpy

from . import settings_io
from ._common import _pref_local_root


def _addon_module_by_leaf(leaf):
    import addon_utils
    for m in addon_utils.modules():
        if m.__name__.rsplit(".", 1)[-1] == leaf:
            return m.__name__
    return None


def _install_shipped_extension(leaf):
    """Install a project‑shipped extension .zip (02_pipeline/blender_extensions/) so
    every artist gets the add‑on without manually downloading it. Returns the
    installed module name, or None if there's no zip / install failed."""
    import glob
    root = os.environ.get("FLUMEN_PROJECT_ROOT", "")
    ext_dir = os.path.join(root, "02_pipeline", "blender_extensions") if root else ""
    if not ext_dir or not os.path.isdir(ext_dir):
        return None
    zips = (glob.glob(os.path.join(ext_dir, leaf + ".zip"))
            or [z for z in glob.glob(os.path.join(ext_dir, "*.zip"))
                if leaf in os.path.basename(z)])
    if not zips:
        return None
    try:
        bpy.ops.extensions.package_install_files(
            filepath=zips[0], repo="user_default", enable_on_install=True)
        print("[Flumen] installed add-on from", os.path.basename(zips[0]))
        return _addon_module_by_leaf(leaf)
    except Exception as exc:  # noqa: BLE001
        print(f"[Flumen] install of '{leaf}' failed: {exc}")
        return None


def enable_project_addons():
    """Make the project's extra Blender add‑ons available — by default the
    'Add Camera Rigs' add‑on the layout step uses (Dolly / Crane rigs). Configurable
    via project_settings 'addons'. For each: enable it if installed; otherwise
    install it from the project‑shipped zip in 02_pipeline/blender_extensions/ (which
    syncs to every machine), then enable. Matching is by module leaf name, so bundled
    ('add_camera_rigs') and 4.2+ extension ('bl_ext.<repo>.add_camera_rigs') forms
    both work."""
    import addon_utils
    data = settings_io.load_settings(
        settings_io.find_project_root(_pref_local_root())) or {}
    wanted = data.get("addons")
    if wanted is None:
        wanted = ["add_camera_rigs"]
    for name in wanted or []:
        leaf = name.rsplit(".", 1)[-1]
        module = _addon_module_by_leaf(leaf) or _install_shipped_extension(leaf)
        if not module:
            print(f"[Flumen] add-on '{name}' not available — ship its .zip in "
                  f"02_pipeline/blender_extensions/ or install it via Get Extensions.")
            continue
        try:
            addon_utils.enable(module, default_set=False)
            print("[Flumen] add-on ready:", module)
        except Exception as exc:  # noqa: BLE001
            print(f"[Flumen] could not enable {module}: {exc}")


def apply_project_color():
    """Set every scene's display device + view transform to the project's color
    management, so files authored with Blender's default names (sRGB/AgX/Standard)
    stop warning under the project's ACES OCIO config. Color only — leaves render,
    units and output untouched. Run at startup when launched from the Workspace app;
    the file's stored names self-heal on its next save."""
    root = settings_io.find_project_root(_pref_local_root())
    data = settings_io.load_settings(root) or {}
    cm = data.get("color_management") or {}
    if not cm.get("display_device") and not cm.get("view_transform"):
        return
    for scene in bpy.data.scenes:
        if cm.get("display_device"):
            try:
                scene.display_settings.display_device = cm["display_device"]
            except Exception:  # noqa: BLE001
                pass
        if cm.get("view_transform"):
            try:
                scene.view_settings.view_transform = cm["view_transform"]
            except Exception:  # noqa: BLE001
                pass
    print("[Flumen] applied project color management to",
          len(bpy.data.scenes), "scene(s)")


def scaffold_empty_scene():
    """'Start working (new scene)': drop Blender's startup objects (Cube,
    Camera, Light) so task work begins truly empty — a shot build or dressing
    session should never carry the default scene into a publish."""
    removed = 0
    for o in list(bpy.context.scene.objects):
        try:
            bpy.data.objects.remove(o, do_unlink=True)
            removed += 1
        except Exception:  # noqa: BLE001
            pass
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True,
                               do_recursive=True)
    except Exception:  # noqa: BLE001
        pass
    if removed:
        print(f"[Flumen] new-scene start: removed {removed} startup object(s).")


def scaffold_surface_scene():
    """Set up a fresh surface (look-dev) file: a clean scene (no default
    cube/camera/light) in the Shading workspace with material-preview viewports.
    Called once at startup when the Workspace app opens an empty surface task."""
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    # Land in the Shading workspace (shader editor + material-preview viewport).
    ws = bpy.data.workspaces.get("Shading")
    for win in bpy.context.window_manager.windows:
        if ws is not None and win.workspace is not ws:
            win.workspace = ws
    # Belt-and-braces: make any 3D viewport show materials, in case there's no
    # Shading workspace (custom startup file).
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                for space in area.spaces:
                    if space.type == "VIEW_3D":
                        space.shading.type = "MATERIAL"


def _purge_orphan_data(data):
    """Remove a now-unused data-block (mesh/light/camera/…) so objects dropped
    during a selective append don't linger as orphans and ride into the next
    publish. Best-effort: tries each id collection until one accepts it."""
    if data is None or getattr(data, "users", 1) != 0:
        return
    for attr in ("meshes", "lights", "cameras", "curves", "metaballs",
                 "lattices", "grease_pencils_v3", "grease_pencils", "volumes",
                 "armatures"):
        coll = getattr(bpy.data, attr, None)
        if coll is None:
            continue
        try:
            coll.remove(data)
            return
        except (TypeError, RuntimeError, ReferenceError):
            continue
