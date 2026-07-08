"""Flumen menu in Blender's top menu bar (next to Help).

WHAT shows WHERE lives in menu_spec.py (declarative, per-context), and each
project can hide/re-gate actions via the "menu" block of project_settings.json
— see menu_spec's docstring. This module only draws.
"""

import os

import bpy

from . import operators as _ops
from . import menu_spec
from . import settings_io

# project_settings.json is read on every menu open — cache it by file mtime so
# the menu stays instant and still picks up an edited config without restarting.
_SETTINGS_CACHE = {"path": "", "mtime": None, "data": {}}


def _menu_settings() -> dict:
    root = settings_io.find_project_root()
    if not root:
        return {}
    path = settings_io.settings_path(root)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if (_SETTINGS_CACHE["path"], _SETTINGS_CACHE["mtime"]) != (path, mtime):
        try:
            data = settings_io.load_settings(root)
        except Exception:  # noqa: BLE001 — malformed config: fall back to defaults
            data = {}
        _SETTINGS_CACHE.update(path=path, mtime=mtime, data=data)
    return _SETTINGS_CACHE["data"]


class FLUMEN_MT_menu(bpy.types.Menu):
    bl_label = "Flumen"
    bl_idname = "FLUMEN_MT_menu"

    def draw(self, context):
        layout = self.layout
        task = _ops.active_task()
        if task:
            layout.label(text=f"Task: {task['entity']}  ·  {task['step']}",
                         icon="OUTLINER_OB_ARMATURE")
        else:
            layout.label(text="No active task (open from Workspace app)",
                         icon="INFO")

        entries = menu_spec.resolve_menu(menu_spec.task_ctx(task),
                                         _menu_settings())
        group = None
        for e in entries:
            if e["group"] != group:
                layout.separator()
                group = e["group"]
            kwargs = {"icon": e["icon"]} if e.get("icon") else {}
            if e.get("text"):
                kwargs["text"] = e["text"]
            layout.operator(e["op"], **kwargs)

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
