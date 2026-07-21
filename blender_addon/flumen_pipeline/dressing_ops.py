"""Set-dressing authoring operators (the dressing task's tools): load an
environment's published model as an overridable holder, and place published
assets as props. Applying a published dressing at build time lives in
build_shot.py; this is the authoring side."""

import json
import os
import subprocess

import bpy

from . import dressing as dressing_mod
from .build_shot import (
    _fetch_publish_path, _link_collection_override, _named_holder, _project_rel)
from ._common import (
    _shell_json, active_task)


class FLUMEN_OT_build_dressing(bpy.types.Operator):
    bl_idname = "flumen.build_dressing"
    bl_label = "Load environment"
    bl_description = ("Link the environment's published model (library override) "
                      "under an environment__ holder, ready for set-dressing")

    def execute(self, context):
        task = active_task()
        if not task or task.get("type") != "asset" or task.get("step") != "dressing":
            self.report({"ERROR"}, "No active dressing task — open the "
                                   "environment's dressing task from the Workspace app.")
            return {"CANCELLED"}
        leaf = (task.get("entity") or "").split("/")[-1]
        holder_name = dressing_mod.ENV_HOLDER_PREFIX + leaf
        if bpy.data.collections.get(holder_name) is not None:
            self.report({"INFO"}, "Environment already loaded.")
            return {"FINISHED"}

        blend = os.environ.get("FLUMEN_MODEL_PUBLISH")
        if not blend or not os.path.isfile(blend):
            blend = _fetch_publish_path(task["id"], "model")
        if not blend or not os.path.isfile(blend):
            self.report({"ERROR"}, "No published model for this environment — "
                                   "publish the model step first.")
            return {"CANCELLED"}

        holder = _named_holder(context, holder_name)
        override, err = _link_collection_override(context, blend, leaf, holder)
        if err:
            self.report({"ERROR"}, f"Could not load the environment: {err}")
            return {"CANCELLED"}
        holder["flumen_env_asset"] = task.get("entity", "")
        holder["flumen_env_step"] = "model"
        holder["flumen_env_blend_rel"] = _project_rel(blend)
        self.report({"INFO"}, f"Environment loaded from {os.path.basename(blend)} "
                              f"— add props and publish a dressing.")
        return {"FINISHED"}

# 'Add prop' dropdown items — cached (Blender enum-callback GC pitfall, same as
# _STEP_ENUM_CACHE) and refreshed on each invoke.
_PROP_CHOICES: list[tuple] = [("__none__", "(no published assets)", "")]

def _prop_enum_items(self, context):
    return _PROP_CHOICES

class FLUMEN_OT_add_prop(bpy.types.Operator):
    bl_idname = "flumen.add_prop"
    bl_label = "Add prop…"
    bl_description = ("Place a published asset into the dressing: linked + "
                      "overridable, parented under a prop_root__ empty that "
                      "carries the transform the manifest publishes")

    prop_choice: bpy.props.EnumProperty(name="Asset", items=_prop_enum_items)

    def invoke(self, context, event):
        global _PROP_CHOICES
        rows = _shell_json(["list-asset-publishes", "--step", "model"]) or []
        items = [(json.dumps(r), r["entity"], r["blend_rel"]) for r in rows]
        _PROP_CHOICES = items or [("__none__", "(no published assets)", "")]
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        self.layout.prop(self, "prop_choice")

    def execute(self, context):
        if self.prop_choice == "__none__":
            self.report({"ERROR"}, "No published assets to place.")
            return {"CANCELLED"}
        row = json.loads(self.prop_choice)
        entity, step = row["entity"], row.get("step", "model")
        leaf = entity.split("/")[-1]
        task_id = f"asset-{entity.replace('/', '_')}-{step}"

        blend = _fetch_publish_path(task_id, step)
        if not blend or not os.path.isfile(blend):
            self.report({"ERROR"}, f"Could not fetch the {step} publish of {entity}.")
            return {"CANCELLED"}

        existing = {o.get("flumen_prop_id") or
                    o.name[len(dressing_mod.PROP_ROOT_PREFIX):]
                    for o in bpy.data.objects
                    if o.name.startswith(dressing_mod.PROP_ROOT_PREFIX)}
        pid = dressing_mod.prop_id_for(leaf, existing)

        holder = _named_holder(context, dressing_mod.PROP_HOLDER_PREFIX + pid)
        override, err = _link_collection_override(context, blend, leaf, holder)
        if err:
            self.report({"ERROR"}, f"Could not place {entity}: {err}")
            return {"CANCELLED"}

        # The LOCAL empty that owns the placement: artists move THIS. Its world
        # matrix is what the dressing manifest records — never override data.
        root = bpy.data.objects.new(dressing_mod.PROP_ROOT_PREFIX + pid, None)
        root.empty_display_type = "PLAIN_AXES"
        root.empty_display_size = 0.5
        root["flumen_prop_id"] = pid
        root["flumen_prop_asset"] = entity
        root["flumen_prop_step"] = step
        root["flumen_prop_blend_rel"] = row.get("blend_rel") or _project_rel(blend)
        root["flumen_prop_collection"] = leaf
        holder.objects.link(root)
        root.location = context.scene.cursor.location
        for o in override.all_objects:
            if o.parent is None and o is not root:
                o.parent = root
        self.report({"INFO"}, f"Placed {entity} as {root.name} — move the empty, "
                              f"then publish the dressing.")
        return {"FINISHED"}
