"""Look (shader) apply operators and the build-time element-look helper.

`FLUMEN_OT_apply_look` assigns a published look onto a character's meshes (asset
task); `_apply_element_look` re-applies a published look onto a linked shot
element at Build-shot / Cache time. Pure look *publishing* math lives in look.py
(imported here as look_mod); this module is the bpy-facing operator side.

Extracted from operators.py; registration flows through operators.CLASSES.
"""

import json
import os
import subprocess

import bpy

from . import look as look_mod
from ._common import active_task, _toolkit_cmd, _no_window


_LOOK_CHOICES = []   # cached list-looks result for the apply dropdown
# Keep the returned enum items alive — Blender stores only char* pointers for
# callback-generated items, so a fresh local list gets GC'd and the dropdown
# renders freed memory as garbage. Same guard as lights._light_rig_items.
_LOOK_ENUM = []


def _apply_look_items(self, context):
    global _LOOK_ENUM
    _LOOK_ENUM = [(l["look"], f"{l['look']}  (v{l['version']:03d})", "")
                  for l in _LOOK_CHOICES] or [("", "<no looks published>", "")]
    return _LOOK_ENUM


class FLUMEN_OT_apply_look(bpy.types.Operator):
    bl_idname = "flumen.apply_look"
    bl_label = "Apply look"
    bl_description = ("Fetch a published look for this character and assign its "
                      "materials onto the meshes by name")

    look: bpy.props.EnumProperty(name="Look", items=_apply_look_items)

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "asset" or not task.get("entity"):
            self.report({"ERROR"}, "No active asset task.")
            return {"CANCELLED"}
        global _LOOK_CHOICES
        _LOOK_CHOICES = self._list_looks(look_mod.surface_task_id(task["entity"]))
        if not _LOOK_CHOICES:
            self.report({"ERROR"}, "No looks published for this character yet — "
                                   "publish one from the surface task first.")
            return {"CANCELLED"}
        self.look = _LOOK_CHOICES[0]["look"]
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Apply a published look to this character:")
        col.prop(self, "look")

    def execute(self, context):
        task = active_task()
        if not task or not task.get("entity") or not self.look:
            self.report({"ERROR"}, "No look selected.")
            return {"CANCELLED"}
        sid = look_mod.surface_task_id(task["entity"])
        blend = self._fetch_look(sid, self.look)
        if not blend or not os.path.isfile(blend):
            self.report({"ERROR"}, "Couldn't fetch the look from the server.")
            return {"CANCELLED"}
        try:
            manifest = json.load(open(blend[:-6] + ".manifest.json"))
        except Exception:  # noqa: BLE001
            manifest = {}
        mats = self._append_materials(blend)
        assigned, missing = self._assign(manifest.get("assignments", {}), mats)
        self._dedupe_material_names(mats)
        msg = f"Applied look '{self.look}': {assigned} mesh(es)"
        if missing:
            msg += f", {missing} not found in scene"
        self.report({"INFO"}, msg)
        return {"FINISHED"}

    # --- helpers ---------------------------------------------------------
    def _list_looks(self, surface_id):
        cmd, td = _toolkit_cmd(["list-looks", "--task", surface_id])
        if cmd is None:
            return []
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True, **_no_window())
            return json.loads(out.splitlines()[-1])
        except Exception:  # noqa: BLE001
            return []

    def _fetch_look(self, surface_id, look):
        cmd, td = _toolkit_cmd(
            ["fetch-look", "--task", surface_id, "--look", look])
        if cmd is None:
            return None
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True, **_no_window()).strip()
            return out.splitlines()[-1] if out else None
        except Exception:  # noqa: BLE001
            return None

    def _append_materials(self, blend):
        # If the look IS the currently-open file (e.g. you opened the look library
        # itself), Blender can't append from it — its materials are already here.
        if (bpy.data.filepath
                and os.path.abspath(blend) == os.path.abspath(bpy.data.filepath)):
            return {m.name: m for m in bpy.data.materials}
        # Map ORIGINAL name -> appended datablock, so a name clash with an existing
        # scene material (renamed to .001 on append) doesn't break assignment.
        names = []
        with bpy.data.libraries.load(blend, link=False) as (src, dst):
            names = list(src.materials)          # keep the name strings separate
            dst.materials = list(src.materials)  # Blender fills this list in place
        return {nm: mat for nm, mat in zip(names, dst.materials) if mat is not None}

    def _dedupe_material_names(self, mats):
        """If the clean model brought its own same-named material, the look's
        appended copy gets a '.001' suffix. Once we've reassigned, the model's copy
        is orphaned — drop it and let the look's material reclaim the clean name."""
        for orig_name, mat in mats.items():
            if mat.name == orig_name:
                continue
            old = bpy.data.materials.get(orig_name)
            if old is not None and old is not mat and old.users == 0:
                bpy.data.materials.remove(old)
                try:
                    mat.name = orig_name
                except Exception:  # noqa: BLE001
                    pass

    def _assign(self, assignments, mats):
        assigned = missing = 0
        for mesh_name, slot_mats in assignments.items():
            obj = bpy.data.objects.get(mesh_name)
            if obj is None or obj.type != "MESH":
                missing += 1
                continue
            me = obj.data
            for i, mname in enumerate(slot_mats):
                mat = mats.get(mname) if mname else None
                if i < len(me.materials):
                    me.materials[i] = mat
                else:
                    me.materials.append(mat)
            assigned += 1
        return assigned, missing


def _activate_base_image(mat):
    """Make the base-color image the material's ACTIVE node. Workbench's
    TEXTURE mode (solid viewports + our playblasts) displays the active image
    node — if the surface artist's last click before publishing was the
    Material Output, the whole character draws flat grey. Saved click-state
    should never decide what dailies look like."""
    nt = getattr(mat, "node_tree", None)
    if nt is None:
        return
    target = None
    for nd in nt.nodes:
        if nd.type != "BSDF_PRINCIPLED":
            continue
        inp = nd.inputs.get("Base Color")
        if inp and inp.links and inp.links[0].from_node.type == "TEX_IMAGE":
            target = inp.links[0].from_node
        break
    if target is None:
        target = next((n for n in nt.nodes
                       if n.type == "TEX_IMAGE" and n.image is not None), None)
    if target is not None:
        try:
            nt.nodes.active = target
        except Exception:  # noqa: BLE001 — linked/read-only tree
            pass


import re as _re
# A trailing Blender collision suffix ('.001') OR the underscore form ('_001')
# that Alembic writes — the .abc format can't store dots in names, so a cache's
# 2nd instance 'Skeleton_Base.001' comes back as 'Skeleton_Base_001'.
_SUFFIX_RE = _re.compile(r"[._]\d{3,}$")


def _base_name(name):
    """Strip a trailing collision suffix (dot or underscore form) so a cache
    instance's suffixed mesh matches the look manifest's clean name."""
    return _SUFFIX_RE.sub("", name)


def _match_meshes_by_name(manifest_names, meshes):
    """Map each look-manifest mesh name to a holder mesh object, robust to
    Blender's per-instance collision suffixes (both '.001' and Alembic's '_001').
    Exact name first; then pair the remaining same-BASE names in sorted order, so
    a 2nd instance's cache meshes ('Skeleton_Base_001') map to the manifest's
    'Skeleton_Base' even though the plain name differs. Returns {mesh_name: obj}."""
    from collections import defaultdict
    by_name = {o.name: o for o in meshes}
    result, used, remaining = {}, set(), []
    for mn in manifest_names:
        o = by_name.get(mn)
        if o is not None and o.name not in used:
            result[mn] = o
            used.add(o.name)
        else:
            remaining.append(mn)
    holder_by_base = defaultdict(list)
    for o in meshes:
        if o.name not in used:
            holder_by_base[_base_name(o.name)].append(o)
    for lst in holder_by_base.values():
        lst.sort(key=lambda o: o.name)
    man_by_base = defaultdict(list)
    for mn in remaining:
        man_by_base[_base_name(mn)].append(mn)
    for base, mns in man_by_base.items():
        objs = holder_by_base.get(base, [])
        for mn, o in zip(sorted(mns), objs):
            result[mn] = o
    return result


def _apply_element_look(holder, look_data):
    """Apply a published look onto a shot element's meshes — a LINKED rig/model or
    an imported ALEMBIC cache, and any number of instances. For linked (read-only)
    mesh data the materials go on OBJECT-level slots (override-safe); for local
    editable mesh data (alembic caches, locally-modeled geometry) they go straight
    onto the mesh material list (set/appended as needed). Matching pairs the look
    manifest's clean mesh names to this scene's (possibly suffixed) objects via
    _match_meshes_by_name, so multiple instances each get the look. Returns how
    many meshes got the look."""
    blend = look_data.get("blend_local") or ""
    if not (blend and os.path.isfile(blend)):
        return 0
    try:
        manifest = json.load(open(blend[:-6] + ".manifest.json"))
    except Exception:  # noqa: BLE001
        manifest = {}
    assignments = manifest.get("assignments") or {}
    if not assignments:
        return 0
    names = []
    with bpy.data.libraries.load(blend, link=False) as (src, dst):
        names = list(src.materials)
        dst.materials = list(src.materials)
    mats = {nm: mat for nm, mat in zip(names, dst.materials) if mat is not None}
    for mat in mats.values():
        _activate_base_image(mat)          # Workbench draws the ACTIVE image
    meshes = [o for o in holder.all_objects if getattr(o, "type", "") == "MESH"]
    mapping = _match_meshes_by_name(list(assignments), meshes)
    assigned = 0
    for mesh_name, obj in mapping.items():
        slot_mats = assignments[mesh_name]
        me = getattr(obj, "data", None)
        # Local (editable) mesh data — imported alembic cache or local model —
        # takes materials directly; a linked mesh's slots are read-only, so use
        # object-level overrides instead.
        local = me is not None and getattr(me, "library", None) is None
        ok = False
        for i, mname in enumerate(slot_mats):
            mat = mats.get(mname) if mname else None
            if local:
                try:
                    if i < len(me.materials):
                        me.materials[i] = mat
                    else:
                        me.materials.append(mat)
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
            else:
                if i >= len(obj.material_slots):
                    break                      # linked mesh defines the slots
                slot = obj.material_slots[i]
                try:
                    if slot.link != "OBJECT":
                        slot.link = "OBJECT"   # object slots are override-safe
                    slot.material = mat
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
        assigned += bool(ok)
    return assigned


CLASSES = (FLUMEN_OT_apply_look,)
