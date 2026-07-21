"""Lighting operators: the LIGHTS collection, publishing a shot's light rig, and
loading a published rig from another shot.

Extracted from operators.py; registration still flows through operators.CLASSES,
which imports the operator classes from here.
"""

import os
import subprocess

import bpy

from ._common import active_task, _toolkit_cmd, _no_window, _publog, _shell_json


LIGHTS_COLLECTION = "LIGHTS"


def _lights_collection(context, create=False):
    """The scene's LIGHTS collection — where the lighter's lamps live and what a
    lighting publish captures. Created + linked if `create`."""
    coll = bpy.data.collections.get(LIGHTS_COLLECTION)
    if coll is None and create:
        coll = bpy.data.collections.new(LIGHTS_COLLECTION)
        context.scene.collection.children.link(coll)
    elif coll is not None and create \
            and coll.name not in context.scene.collection.children:
        try:
            context.scene.collection.children.link(coll)
        except Exception:  # noqa: BLE001
            pass
    return coll


class FLUMEN_OT_add_lights(bpy.types.Operator):
    bl_idname = "flumen.add_lights"
    bl_label = "Add LIGHTS collection"
    bl_description = ("Create the LIGHTS collection — put your lamps in here; a "
                      "lighting publish captures exactly this, and it's what "
                      "loads into other shots")

    def execute(self, context):
        existed = bpy.data.collections.get(LIGHTS_COLLECTION) is not None
        _lights_collection(context, create=True)
        self.report({"INFO"}, "LIGHTS collection ready — add your lamps to it."
                    if not existed else "LIGHTS collection already present.")
        return {"FINISHED"}


class FLUMEN_OT_publish_lights(bpy.types.Operator):
    bl_idname = "flumen.publish_lights"
    bl_label = "Publish lights"
    bl_description = ("Publish this shot's light rig — the lamps in the LIGHTS "
                      "collection — as a versioned, reusable setup, and set the "
                      "task to Review")

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "shot":
            self.report({"ERROR"}, "Open a lighting shot task from the "
                                   "Workspace app.")
            return {"CANCELLED"}
        coll = _lights_collection(context)
        n = len([o for o in coll.all_objects if o.type == "LIGHT"]) if coll else 0
        if not n:
            self.report({"ERROR"}, "No lamps in a LIGHTS collection — add lights "
                                   "(Flumen ▸ Add LIGHTS collection) first.")
            return {"CANCELLED"}
        self._n = n
        return context.window_manager.invoke_props_dialog(
            self, width=380, title="Publish lights", confirm_text="Publish")

    def draw(self, context):
        col = self.layout.column()
        col.prop(context.window_manager, "flumen_publish_desc", text="Description")
        col.label(text=f"{self._n} lamp(s) from the LIGHTS collection.",
                  icon="LIGHT")

    def execute(self, context):
        task = active_task()
        if not task:
            return {"CANCELLED"}
        coll = _lights_collection(context)
        lights = [o for o in coll.all_objects if o.type == "LIGHT"] if coll else []
        if not lights:
            self.report({"ERROR"}, "No lamps to publish.")
            return {"CANCELLED"}
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(prefix="flumen_lights_"),
                           "lights.blend")
        try:
            bpy.data.libraries.write(tmp, set(lights), fake_user=True)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not write the light rig: {exc}")
            return {"CANCELLED"}
        desc = context.window_manager.flumen_publish_desc
        cmd, td = _toolkit_cmd(["publish-lights", "--task", task["id"],
                                "--local", tmp, "--status", "review",
                                "--description", desc])
        if cmd is None:
            self.report({"WARNING"}, f"Wrote the rig to {tmp}, but the toolkit "
                        f"wasn't found to publish it.")
            return {"FINISHED"}
        context.window_manager.flumen_publish_desc = ""
        p = subprocess.run(cmd, cwd=td, text=True, capture_output=True,
                           **_no_window())
        for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
            _publog("  " + line, echo=False)
        if p.returncode != 0:
            self.report({"ERROR"}, "Light publish failed — see the pipeline log.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Published {len(lights)} lamp(s); task → Review.")
        return {"FINISHED"}


_LIGHT_RIGS = []   # cached [{shot, version, rel, by}] for the Load-lights enum
# Blender only stores char* pointers for callback-generated enum items, so the
# label/id strings MUST be kept alive on the Python side or they get GC'd and the
# dropdown renders freed memory as garbage. Hold the last returned list here.
_LIGHT_RIG_ENUM = []


def _light_rig_items(self, context):
    global _LIGHT_RIG_ENUM
    items = []
    for i, r in enumerate(_LIGHT_RIGS):
        label = f"{r['shot']}  ·  v{r['version']:03d}"
        if r.get("by"):
            label += f"  ·  {r['by']}"
        items.append((str(i), label, r.get("rel", "")))
    _LIGHT_RIG_ENUM = items or [("-1", "(no published light rigs)", "")]
    return _LIGHT_RIG_ENUM


class FLUMEN_OT_load_lights(bpy.types.Operator):
    bl_idname = "flumen.load_lights"
    bl_label = "Load lights from another shot"
    bl_description = ("Append a published light rig from any shot into this "
                      "scene's LIGHTS collection — build a setup once, reuse it "
                      "and tweak per shot")

    rig: bpy.props.EnumProperty(name="Light rig", items=_light_rig_items)

    def invoke(self, context, event):
        global _LIGHT_RIGS
        _LIGHT_RIGS = _shell_json(["list-light-rigs"]) or []
        if not _LIGHT_RIGS:
            self.report({"WARNING"}, "No published light rigs in the project yet.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Append lights from a published shot rig:")
        col.prop(self, "rig")

    def execute(self, context):
        try:
            idx = int(self.rig)
        except (TypeError, ValueError):
            idx = -1
        if idx < 0 or idx >= len(_LIGHT_RIGS):
            self.report({"ERROR"}, "No light rig selected.")
            return {"CANCELLED"}
        r = _LIGHT_RIGS[idx]
        cmd, td = _toolkit_cmd(["fetch-lights", "--rel", r["rel"]])
        if cmd is None:
            self.report({"ERROR"}, "Toolkit not available.")
            return {"CANCELLED"}
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True,
                                          **_no_window()).strip()
            blend = out.splitlines()[-1] if out else ""
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not fetch the rig: {exc}")
            return {"CANCELLED"}
        if not blend or not os.path.isfile(blend):
            self.report({"ERROR"}, "Rig file not found after fetch.")
            return {"CANCELLED"}
        # Append the light objects (a COPY — editable per shot) into LIGHTS.
        target = _lights_collection(context, create=True)
        before = set(bpy.data.objects)
        with bpy.data.libraries.load(blend, link=False) as (src, dst):
            dst.objects = [n for n in src.objects]
        added = 0
        for o in dst.objects:
            if o is None or o.type != "LIGHT":
                continue
            if o.name not in target.objects:
                target.objects.link(o)
                added += 1
        # drop any non-light strays the append pulled in
        for o in bpy.data.objects:
            if o not in before and o.type != "LIGHT" and o.users == 0:
                bpy.data.objects.remove(o)
        self.report({"INFO"}, f"Loaded {added} lamp(s) from {r['shot']} "
                              f"v{r['version']:03d} into LIGHTS.")
        return {"FINISHED"}


CLASSES = (FLUMEN_OT_add_lights, FLUMEN_OT_publish_lights, FLUMEN_OT_load_lights)
