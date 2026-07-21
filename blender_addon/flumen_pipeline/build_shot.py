"""Build a shot: bring its breakdown into the scene.

The one coherent home for the Build-shot operation and everything it needs —
scene-assembly primitives (holders, library-override linking, alembic import),
element placement (matrix snapshot/restore), the per-kind element loaders and
camera-rig builders, applying published animation and set-dressing onto built
elements, and the Build-shot + Load-animation operators themselves.

The separate shot features import their scene-assembly helpers FROM here:
dressing_ops (authoring), review_camera, and cache_shot all build on this module;
none of them are imported back, so there are no cycles. Registration still flows
through operators.CLASSES, which imports the operator classes from here.
"""

import json
import os
import subprocess

import bpy

from . import settings_io
from . import checks
from . import textures
from . import look as look_mod
from . import anim as anim_mod
from . import dressing as dressing_mod
from ._common import (
    _prefs, _pref_local_root, _toolkit_cmd, PUBLISH_LOG, _publog, _no_window,
    _preflight_server, _shell_toolkit, _shell_json, _apply_one, active_task)
from .looks import _apply_element_look


ELEMENT_HOLDER_PREFIX = "element__"

def _element_holder(context, element_id):
    """The per-element scene collection (created if absent) that holds one element
    instance — unique per id, so two instances of the same asset never clash."""
    nm = ELEMENT_HOLDER_PREFIX + element_id
    holder = bpy.data.collections.get(nm)
    if holder is None:
        holder = bpy.data.collections.new(nm)
    if holder.name not in context.scene.collection.children:
        context.scene.collection.children.link(holder)
    return holder

def _missing_libraries():
    """Libraries whose .blend no longer exists on disk (e.g. local publishes
    cleaned away after Build shot linked them)."""
    out = set()
    for lib in bpy.data.libraries:
        try:
            if not os.path.isfile(bpy.path.abspath(lib.filepath)):
                out.add(lib)
        except Exception:  # noqa: BLE001
            pass
    return out

def _element_content_broken(holder, missing_libs=None):
    """True when an element holder's content can't load: its publish file is
    gone from disk (placeholder data) or the holder is simply empty."""
    if len(holder.all_objects) == 0 and len(holder.children) == 0:
        return True
    libs = _missing_libraries() if missing_libs is None else missing_libs
    if not libs:
        return False

    def _uses_missing(idblock):
        if getattr(idblock, "library", None) in libs:
            return True
        ov = getattr(idblock, "override_library", None)
        ref = getattr(ov, "reference", None) if ov else None
        return ref is not None and ref.library in libs

    return (any(_uses_missing(c) for c in holder.children_recursive)
            or any(_uses_missing(o) for o in holder.all_objects))

def _remove_collection_tree(coll):
    """Delete a collection, its sub-collections and their objects."""
    for sub in list(coll.children):
        _remove_collection_tree(sub)
    for o in list(coll.objects):
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        bpy.data.collections.remove(coll)
    except Exception:  # noqa: BLE001
        pass

def _clear_element_holder(holder):
    """Drop everything under an element holder (rebuild of broken content);
    the holder itself stays so the loaders reuse it."""
    for child in list(holder.children):
        _remove_collection_tree(child)
    for o in list(holder.objects):
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:  # noqa: BLE001
            pass

def _element_matrix_snapshot(holder):
    """The artist's placement, captured before an update clears the content:
    per-object local matrices plus which objects were ROOTS (parentless) — the
    roots carry the element's overall placement when an update switches steps
    (model -> rig) and no object name survives the swap."""
    return {"objects": {o.name: o.matrix_basis.copy()
                        for o in holder.all_objects},
            "roots": [o.name for o in holder.all_objects if o.parent is None],
            "file": _element_loaded_file(holder)}

def _rig_main_control(arm):
    """The rig's 'place me here' pose bone: a root-like name first (Rigify's
    'root'), else the first parentless pose bone. None on a boneless rig."""
    if not getattr(arm, "pose", None):
        return None
    for name in ("root", "Root", "ROOT", "main", "master", "global"):
        pb = arm.pose.bones.get(name)
        if pb is not None:
            return pb
    for pb in arm.pose.bones:
        if pb.parent is None:
            return pb
    return None

def _matrix_is_identity(m, eps=1e-6):
    for i in range(4):
        for j in range(4):
            if abs(m[i][j] - (1.0 if i == j else 0.0)) > eps:
                return False
    return True

def _element_matrix_restore(holder, snap):
    """Re-apply captured local matrices to same-named objects after a relink
    (base-name fallback absorbs .001 suffix drift between publishes). When
    NOTHING matches by name — the update switched steps, e.g. a model element
    upgraded to its freshly-published rig — the old ROOT transform (preferring
    the model's PUBLISH locator) is composed onto the new content's roots, so
    the element stays where the artist put it. Returns how many objects got
    their placement back."""
    objs = (snap or {}).get("objects") or {}
    if not objs:
        return 0

    # A publish-family switch (model -> rig) must NEVER restore by name: the
    # rig usually shares its mesh names with the model it was built from, so
    # stale model-space matrices (scale included) would land on meshes the
    # armature already drives — double transforms. Placement goes to the rig's
    # control instead.
    import re

    def _step_of(fname):
        m = re.search(r"_([a-z0-9]+)_v\d+\.blend$", fname or "")
        return m.group(1) if m else ""

    old_step = _step_of((snap or {}).get("file", ""))
    new_step = _step_of(_element_loaded_file(holder))
    cross_step = bool(old_step and new_step and old_step != new_step)

    restored = 0
    if not cross_step:
        by_base = {}
        for name in objs:
            by_base.setdefault(name.split(".")[0], name)
        for o in holder.all_objects:
            m = objs.get(o.name)
            if m is None:
                src = by_base.get(o.name.split(".")[0])
                m = objs.get(src) if src else None
            if m is not None:
                try:
                    o.matrix_basis = m
                    restored += 1
                except Exception:  # noqa: BLE001
                    pass
        if restored:
            return restored
    # Cross-step swap (or nothing matched): carry the placement via the old root.
    roots = (snap or {}).get("roots") or []
    root_m = None
    for name in roots:
        m = objs.get(name)
        if m is None:
            continue
        if name.split(".")[0].startswith("PUBLISH"):
            root_m = m                    # the model's wrap root — best signal
            break
        if root_m is None:
            root_m = m
    if root_m is None or _matrix_is_identity(root_m):
        return 0
    # New content is a RIG: the placement belongs on its MAIN CONTROL, the
    # channel animators actually use — location + facing only, never scale
    # (the model was often scaled to fit; the rig's proportions are its own),
    # and the rig's meshes are never touched (they follow the armature).
    from mathutils import Matrix
    loc, rot, _scale = root_m.decompose()
    place = Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
    arms = sorted((o for o in holder.all_objects
                   if getattr(o, "type", "") == "ARMATURE"
                   and getattr(o, "pose", None)),
                  key=lambda a: -len(a.pose.bones))
    if arms:
        arm = arms[0]                      # the rig (most bones), not helpers
        pb = _rig_main_control(arm)
        try:
            if pb is not None:
                pb.matrix = arm.matrix_world.inverted() @ place
                where = f"'{pb.name}' control"
            else:
                arm.matrix_basis = place @ arm.matrix_basis
                where = "armature object (no root-like control found)"
            print(f"[Flumen] model→rig update: placement (location+rotation, "
                  f"scale dropped) applied to the rig's {where}.")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"[Flumen] could not place the rig's control: {exc}")
            return 0
    # No armature (e.g. a model republished with renamed objects): carry the
    # full matrix onto the roots — scale is meaningful for placed models.
    for o in holder.all_objects:
        if o.parent is not None:
            continue
        try:
            o.matrix_basis = root_m @ o.matrix_basis
            restored += 1
        except Exception:  # noqa: BLE001
            pass
    if restored:
        print(f"[Flumen] element update switched publishes with no matching "
              f"object names — placement carried over via the old root "
              f"transform ({restored} root object(s) moved).")
    return restored

def _link_collection_override(context, blend_local, coll_name, holder):
    """LINK a named collection from a published .blend and make a fully-editable
    library override nested under `holder`. The core loader shared by shot
    elements, environment loading and set-dressing props.
    Returns (override_collection, error)."""
    if not blend_local or not os.path.isfile(blend_local):
        return None, "publish not found locally"
    # Candidate collections, best first: the exact name, then its dotted
    # variants (newest suffix first). Old publishes made before the name-clash
    # fix carry an EMPTY exact-named collection with the real content in
    # 'name.005' — the fallback below walks candidates until one has objects.
    with bpy.data.libraries.load(blend_local, link=False, assets_only=False) as (src, _):
        available = list(src.collections)
    candidates = []
    if coll_name:
        dotted = sorted((n for n in available
                         if n != coll_name and n.split(".")[0] == coll_name),
                        reverse=True)
        candidates = ([coll_name] if coll_name in available else []) + dotted
    if not candidates and available:
        candidates = [available[0]]     # pre-collection publishes: first one
    if not candidates:
        return None, "no linkable collection (republish the rig/model)"

    linked = None
    for cand in candidates:
        with bpy.data.libraries.load(blend_local, link=True,
                                     relative=True) as (src, dst):
            dst.collections = [cand]
        got = next((c for c in dst.collections if c is not None), None)
        if got is None:
            continue
        if len(got.all_objects) > 0:
            if cand != coll_name:
                print(f"[Flumen] '{coll_name}' is empty in this publish — "
                      f"linked '{cand}' instead (old name-clash publish; "
                      f"republish to clean it up).")
            linked = got
            break
        # empty candidate: unlink it again and try the next
        try:
            bpy.data.collections.remove(got)
        except Exception:  # noqa: BLE001
            pass
    if linked is None:
        return None, (f"collection '{coll_name}' has no content in this "
                      f"publish — republish the asset")

    # Build a full, editable override hierarchy so the content is poseable/movable.
    try:
        override = linked.override_hierarchy_create(
            context.scene, context.view_layer, do_fully_editable=True)
    except Exception as exc:  # noqa: BLE001
        return None, f"override failed: {exc}"

    # Relocate the override collection under the holder.
    sc = context.scene.collection
    try:
        if override.name in sc.children:
            sc.children.unlink(override)
        if override.name not in holder.children:
            holder.children.link(override)
    except Exception:  # noqa: BLE001
        pass
    return override, None

def _link_asset_element(context, element):
    """Bring an asset element into the shot. Lighting resolves a baked ALEMBIC
    cache (geometry per frame) — import it; otherwise LINK the published
    collection as a poseable library override. Placed under the element's
    holder. Returns (holder, error)."""
    if element.get("cache_local"):
        return _import_alembic_cache(context, element)
    holder = _element_holder(context, element["id"])
    override, err = _link_collection_override(
        context, element.get("blend_local"), element.get("collection") or "", holder)
    if err:
        return None, err
    return holder, None

def _import_alembic_cache(context, element):
    """Import an element's alembic cache into its holder and keep the cache
    connection live (a re-cache reloads via the CacheFile). The imported meshes
    keep their source object names, so the look re-applies by name like a
    linked rig. Returns (holder, error)."""
    path = element.get("cache_local") or ""
    if not os.path.isfile(path):
        return None, "cache file missing on disk"
    holder = _element_holder(context, element["id"])
    before = set(bpy.data.objects)
    try:
        bpy.ops.wm.alembic_import(filepath=path, as_background_job=False,
                                  set_frame_range=False)
    except Exception as exc:  # noqa: BLE001
        return None, f"alembic import failed: {exc}"
    new = [o for o in bpy.data.objects if o not in before]
    if not new:
        return None, "alembic imported no objects"
    scene_coll = context.scene.collection
    for o in new:
        for c in list(o.users_collection):
            if c is not holder:
                try:
                    c.objects.unlink(o)
                except Exception:  # noqa: BLE001
                    pass
        if o.name not in holder.objects:
            try:
                holder.objects.link(o)
            except Exception:  # noqa: BLE001
                pass
    return holder, None

def _named_holder(context, name):
    """A scene collection by exact name (created + linked if absent)."""
    holder = bpy.data.collections.get(name)
    if holder is None:
        holder = bpy.data.collections.new(name)
    if holder.name not in context.scene.collection.children:
        context.scene.collection.children.link(holder)
    return holder

def _fetch_publish_path(task_id, step):
    """Shell `fetch-publish` and return the downloaded local path, or None."""
    cmd, td = _toolkit_cmd(["fetch-publish", "--task", task_id, "--step", step])
    if cmd is None:
        return None
    try:
        out = subprocess.check_output(cmd, cwd=td, text=True, **_no_window()).strip()
        return out.splitlines()[-1] if out else None
    except Exception:  # noqa: BLE001
        return None

def _project_rel(path):
    return dressing_mod.rel_from_local(path, os.environ.get("FLUMEN_PROJECT_ROOT", ""))

def _apply_dressing_props(context, element_holder, element):
    """Place a resolved set-dressing's props under a shot element's holder: link
    each prop's published collection (override), create the placement empty at the
    manifest transform, parent the roots to it. Additive: a prop whose sub-holder
    already exists is skipped, so re-running Build shot never duplicates.
    Returns (built_count, skipped_count)."""
    import mathutils
    payload = element.get("dressing") or {}
    built = skipped = 0
    for p in payload.get("props") or []:
        pid = p.get("id") or "prop"
        sub_name = (dressing_mod.PROP_HOLDER_PREFIX
                    + f"{element.get('id', 'el')}__{pid}")
        if bpy.data.collections.get(sub_name) is not None:
            skipped += 1                       # additive rebuild: already placed
            continue
        sub = bpy.data.collections.new(sub_name)
        element_holder.children.link(sub)
        override, err = _link_collection_override(
            context, p.get("blend_local"), p.get("collection") or "", sub)
        if err:
            print(f"[Flumen] dressing prop '{pid}' failed: {err}")
            try:
                element_holder.children.unlink(sub)
                bpy.data.collections.remove(sub)
            except Exception:  # noqa: BLE001
                pass
            skipped += 1
            continue
        root = bpy.data.objects.new(
            p.get("object") or dressing_mod.PROP_ROOT_PREFIX + pid, None)
        root.empty_display_type = "PLAIN_AXES"
        root.empty_display_size = 0.5
        root["flumen_prop_id"] = pid
        root["flumen_prop_asset"] = p.get("asset", "")
        sub.objects.link(root)
        rows = p.get("matrix_world")
        if rows:
            try:
                root.matrix_world = mathutils.Matrix(rows)
            except Exception as exc:  # noqa: BLE001
                print(f"[Flumen] dressing prop '{pid}': bad matrix ({exc})")
        for o in override.all_objects:
            if o.parent is None and o is not root:
                o.parent = root
        built += 1
    # Local extras — geometry the dresser modeled directly in the dressing
    # scene, linked as one collection from the dressing publish. Transforms are
    # already world-space in that file, so no placement empty is needed.
    ex = payload.get("extras") or {}
    if ex.get("blend_local") and ex.get("collection"):
        sub_name = f"extras__{element.get('id', 'el')}"
        if bpy.data.collections.get(sub_name) is not None:
            skipped += 1                       # additive rebuild
        else:
            sub = bpy.data.collections.new(sub_name)
            element_holder.children.link(sub)
            _override, err = _link_collection_override(
                context, ex["blend_local"], ex["collection"], sub)
            if err:
                print(f"[Flumen] dressing extras failed: {err}")
                try:
                    element_holder.children.unlink(sub)
                    bpy.data.collections.remove(sub)
                except Exception:  # noqa: BLE001
                    pass
            else:
                built += 1
    return built, skipped

def _animated_paths(obj):
    """The set of data-paths that already have an F-curve on obj's action (handles
    both legacy and Blender 4.4+ slotted actions)."""
    ad = getattr(obj, "animation_data", None)
    act = getattr(ad, "action", None) if ad else None
    if not act:
        return set()
    paths = {fc.data_path for fc in getattr(act, "fcurves", []) or []}   # legacy
    for layer in getattr(act, "layers", []) or []:                      # slotted
        for strip in getattr(layer, "strips", []) or []:
            try:
                slot = ad.action_slot
                cbag = strip.channelbag(slot) if slot else None
            except Exception:  # noqa: BLE001
                cbag = None
            if cbag:
                paths.update(fc.data_path for fc in cbag.fcurves)
    return paths

def _rebind_action_and_key(obj, channel, frame):
    """Recover from keyframe_insert() silently returning False at object level.
    Two real-world causes, both seen in production layouts:
      * the object's action is LINKED from the asset's publish (e.g. an empty
        leftover action that shipped inside a model file) — not editable, so
        Blender refuses new keys. Replace it with a LOCAL copy (any motion it
        carries is preserved) and key into that.
      * the action has no bound slot (Blender 4.4+ slotted actions on a
        duplicated override) — it drives nothing; bind its slot, or drop the
        dead action so a fresh insert creates a properly-bound one."""
    ad = getattr(obj, "animation_data", None)
    if ad is None or ad.action is None:
        return False
    act = ad.action
    try:
        if (getattr(act, "library", None) is not None
                or not getattr(act, "is_editable", True)):
            local = act.copy()                 # editable local twin
            ad.action = local
            slots = getattr(local, "slots", None)
            if slots and len(slots) and getattr(ad, "action_slot", None) is None:
                ad.action_slot = slots[0]
        elif getattr(ad, "action_slot", None) is None:
            slots = getattr(act, "slots", None)
            if slots and len(slots):
                ad.action_slot = slots[0]      # its own duplicated slot
            else:
                ad.action = None               # dead action — start fresh
        else:
            return False                       # refused for some other reason
        return bool(obj.keyframe_insert(data_path=channel, frame=frame))
    except Exception:  # noqa: BLE001
        return False

def _snapshot_poses(context):
    """Before publishing, key every MOVED but un-keyed channel at the shot's
    start frame, so static offsets the artist changed without keyframing are
    captured in the Action and survive a rebuild:

      * pose bones + rig objects — moved when they differ from rest (identity),
      * every OTHER object in an element holder (meshes/empties of a
        model-linked element, the camera object) — moved when it differs from
        its LINKED REFERENCE, i.e. the transform its publish shipped with.
        Best effort for layouts built before rigs exist: a model placed
        somewhere specific hands that placement to the animation step's
        Build shot exactly like a posed rig does.

    Channels that are already animated are left untouched. Returns the number
    of channels keyed."""
    scene = context.scene
    start = int(getattr(scene, "frame_start", 1001))
    prev = scene.frame_current
    scene.frame_set(start)
    identity = {"location": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0),
                "rotation_euler": (0.0, 0.0, 0.0),
                "rotation_quaternion": (1.0, 0.0, 0.0, 0.0)}
    keyed = 0

    def snap(target, prefix, animated, rest):
        nonlocal keyed
        rot = ("rotation_quaternion"
               if getattr(target, "rotation_mode", "XYZ") == "QUATERNION"
               else "rotation_euler")
        for ch in ("location", rot, "scale"):
            path = (prefix + "." + ch) if prefix else ch
            if path in animated:                       # already animated — leave it
                continue
            base = rest.get(ch)
            cur = tuple(getattr(target, ch))
            if base is not None and len(cur) == len(base) and all(
                    abs(a - b) <= 1e-6 for a, b in zip(cur, base)):
                continue                               # at rest — nothing to capture
            try:
                ok = target.keyframe_insert(data_path=ch, frame=start)
            except Exception:  # noqa: BLE001 — read-only (pure-linked) object
                ok = False
            if not ok and not prefix:
                ok = _rebind_action_and_key(target, ch, start)
            if ok:
                keyed += 1

    def rest_of(obj):
        """The transform baseline 'unmoved' is measured against: the linked
        reference's values for an override (what the publish shipped), the
        identity for local objects (a fresh camera rig)."""
        ov = getattr(obj, "override_library", None)
        ref = getattr(ov, "reference", None) if ov else None
        if ref is None:
            return identity
        rot = ("rotation_quaternion"
               if getattr(ref, "rotation_mode", "XYZ") == "QUATERNION"
               else "rotation_euler")
        return {"location": tuple(ref.location), "scale": tuple(ref.scale),
                rot: tuple(getattr(ref, rot))}

    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        # Environments are structural backdrops placed as ONE unit — never
        # per-piece animated in a shot. Keying every set piece (tende, sofa,
        # bookshelf…) was creating hundreds of spurious placement 'overrides'.
        # Skip their non-rig objects entirely; the environment sits where its
        # publish/dressing puts it.
        is_env = str(coll.get("flumen_asset", "")).startswith("environments/")
        for o in coll.all_objects:
            if getattr(o, "type", "") == "ARMATURE" and getattr(o, "pose", None):
                o.animation_data_create()
                animated = _animated_paths(o)
                snap(o, "", animated, rest_of(o))       # the rig object itself
                for pb in o.pose.bones:
                    snap(pb, 'pose.bones["%s"]' % pb.name, animated, identity)
            elif is_env:
                continue                                # backdrop — no capture
            elif o.parent is None:
                # Plain object (model geometry root, empty, camera): the
                # element's PLACEMENT lives on its root. Children are the
                # published model's internal structure — they follow the root,
                # so keying them is redundant and bloats the anim publish.
                snap(o, "", _animated_paths(o), rest_of(o))
    scene.frame_set(prev)
    return keyed

def _collect_element_animation(only_ids=None):
    """Gather each element's animation: the Action on every animated object inside an
    'element__*' holder. Returns (set_of_actions, {element_id: {obj_name: action_name}})
    for libraries.write + the manifest. `only_ids` limits to those element ids."""
    actions = set()
    elem_actions = {}
    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        eid = coll.name[len(ELEMENT_HOLDER_PREFIX):]
        if only_ids is not None and eid not in only_ids:
            continue
        mapping = {}
        for o in coll.all_objects:
            ad = getattr(o, "animation_data", None)
            act = getattr(ad, "action", None) if ad else None
            if act is not None:
                actions.add(act)
                mapping[o.name] = act.name
        if mapping:
            elem_actions[eid] = mapping
    return actions, elem_actions

def _action_fcurves(obj):
    """Every F-curve of an object's active action (legacy + 4.4+ slotted channelbag)."""
    ad = getattr(obj, "animation_data", None)
    act = getattr(ad, "action", None) if ad else None
    if not act:
        return []
    fcs = list(getattr(act, "fcurves", []) or [])           # legacy
    for layer in getattr(act, "layers", []) or []:          # slotted
        for strip in getattr(layer, "strips", []) or []:
            try:
                slot = ad.action_slot
                cbag = strip.channelbag(slot) if slot else None
            except Exception:  # noqa: BLE001
                cbag = None
            if cbag:
                fcs.extend(cbag.fcurves)
    return fcs

def _element_anim_hashes(only_ids=None):
    """A deterministic content hash per element with animation: a sha1 of every
    object's F-curves (data_path#index = frame:value;…, rounded + sorted). Identical
    animation -> identical hash, so a publish can tell what actually changed."""
    import hashlib
    out = {}
    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        eid = coll.name[len(ELEMENT_HOLDER_PREFIX):]
        if only_ids is not None and eid not in only_ids:
            continue
        parts = []
        for o in coll.all_objects:
            for fc in _action_fcurves(o):
                kfs = ";".join(f"{k.co[0]:.4f}:{k.co[1]:.6f}"
                               for k in fc.keyframe_points)
                parts.append(f"{o.name}/{fc.data_path}#{fc.array_index}={kfs}")
        if parts:
            blob = "|".join(sorted(parts)).encode("utf-8")
            out[eid] = hashlib.sha1(blob).hexdigest()
    return out

def _stale_content_filter(holder, action_map, captured_content):
    """When the animation was captured against a DIFFERENT publish of this
    element (the manifest's 'contents' vs what the holder links now), object-
    level placement keys are meaningless — a restructured model reuses names
    for different pieces (a 'Door.003' key lands on the wrong door) and rest
    transforms moved. Keep only actions targeting ARMATURES (pose keys ride on
    stable bone names across rig versions); drop the rest. Returns
    (filtered_map, dropped_count). No captured content recorded -> no filter
    (pre-stamping publishes keep today's behavior)."""
    if not captured_content:
        return action_map, 0
    loaded = _element_loaded_file(holder)
    if not loaded or loaded == captured_content:
        return action_map, 0
    arm_bases = {o.name.split(".")[0] for o in holder.all_objects
                 if getattr(o, "type", "") == "ARMATURE"}
    kept = {k: v for k, v in action_map.items()
            if k.split(".")[0] in arm_bases}
    dropped = len(action_map) - len(kept)
    if dropped:
        print(f"[Flumen] '{holder.name}': animation was captured against "
              f"{captured_content}, scene links {loaded} — skipped "
              f"{dropped} object-placement action(s) (re-publish the layout "
              f"against the new version to restore placements).")
    return kept, dropped

def _apply_element_animation(holder, anim_blend, action_map, content=""):
    """Append the published Actions and assign them onto this element's objects by
    name, so a freshly-built element comes back animated. `content` = the
    publish the animation was captured against (stale-placement guard)."""
    if not (anim_blend and action_map and os.path.isfile(anim_blend)):
        return 0
    action_map, _dropped = _stale_content_filter(holder, action_map, content)
    if not action_map:
        return 0
    want = set(action_map.values())
    with bpy.data.libraries.load(anim_blend, link=False) as (src, dst):
        req_names = [a for a in src.actions if a in want]
        dst.actions = list(req_names)     # a SEPARATE copy — Blender fills dst.actions
                                          # with datablocks on exit; req_names must stay
                                          # the name strings (else the lookup below
                                          # keys on datablocks and never matches).
    # Map the REQUESTED name -> loaded datablock by order. Don't key on the loaded
    # action's .name: appending when an orphan of the same name exists (e.g. after
    # deleting the element in place) forces a '.001' suffix that wouldn't match the
    # manifest name. Same zip pattern as look material append.
    loaded = {name: blk for name, blk in zip(req_names, dst.actions)
              if blk is not None}
    # Exact names first, then a BASE-NAME fallback scoped to this holder:
    # model elements' object names carry scene-dependent .00N suffixes (every
    # model publish ships a 'PUBLISH' root empty — a layout with twelve model
    # elements numbers them by link order), so the layout's 'PUBLISH.003' is
    # a fresh animation scene's 'PUBLISH.001'. Within one holder the base
    # name is unambiguous; the fallback only fires when it's unique on BOTH
    # sides (the manifest and the scene).
    manifest_by_base = {}
    for name in action_map:
        b = name.split(".")[0]
        manifest_by_base[b] = None if b in manifest_by_base else name
    holder_objs = list(holder.all_objects)
    base_count = {}
    for o in holder_objs:
        b = o.name.split(".")[0]
        base_count[b] = base_count.get(b, 0) + 1
    applied = 0
    for o in holder_objs:
        key = o.name if o.name in action_map else None
        if key is None and base_count.get(o.name.split(".")[0]) == 1:
            key = manifest_by_base.get(o.name.split(".")[0])
        act = loaded.get(action_map.get(key, "")) if key else None
        if act is None:
            continue
        o.animation_data_create()
        o.animation_data.action = act
        # Blender 4.4+ slotted actions: a slot must be bound to drive the object. It
        # auto-binds when the object name matches the action's slot; force the first
        # slot otherwise. (No-op on older Blender without slots.)
        try:
            ad = o.animation_data
            if getattr(ad, "action_slot", None) is None:
                slots = getattr(act, "slots", None)
                if slots and len(slots):
                    ad.action_slot = slots[0]
        except Exception:  # noqa: BLE001
            pass
        applied += 1
    return applied

def _build_camera_rig(context, element):
    """Build a fresh Dolly camera rig for a shot element (see _spawn_dolly_rig)."""
    holder = _element_holder(context, element["id"])
    name = element.get("camera_name") or "shot_camera"
    _rig, _cam, err = _spawn_dolly_rig(context, holder, name)
    if err:
        return None, err
    return holder, None

def _spawn_dolly_rig(context, holder, name):
    """Build a Dolly camera rig (Add Camera Rigs add-on) into `holder` and make
    its camera the scene camera. Only the armature + camera go into the holder;
    the add-on's WGT-* bone shapes stay in its hidden Widgets collection (they're
    shapes, not controls). Returns (rig, cam, error)."""
    before_objs = set(bpy.data.objects)
    before_colls = set(bpy.data.collections)
    try:
        bpy.ops.object.build_camera_rig(mode="DOLLY")
    except Exception as exc:  # noqa: BLE001 — add-on missing/disabled
        return None, None, f"camera-rig add-on unavailable ({exc})"
    new_objs = [o for o in bpy.data.objects if o not in before_objs]
    new_colls = [c for c in bpy.data.collections if c not in before_colls]
    if not new_objs:
        return None, None, "camera rig build produced nothing"
    rig = next((o for o in new_objs if o.type == "ARMATURE"), None)
    cam = next((o for o in new_objs if o.type == "CAMERA"), None)

    # Relocate ONLY the rig + camera into the holder. The bone-shape widgets
    # (WGT-*) are deliberately left in the add-on's hidden Widgets collection — they
    # are not controls and moving them does nothing.
    for o in (rig, cam):
        if o is None:
            continue
        for c in list(o.users_collection):
            try:
                c.objects.unlink(o)
            except Exception:  # noqa: BLE001
                pass
        try:
            holder.objects.link(o)
        except Exception:  # noqa: BLE001
            pass

    # Tuck the add-on's new widget collection under the holder and keep it hidden,
    # so it doesn't clutter the scene root or invite stray clicks.
    sc = context.scene.collection
    for c in new_colls:
        try:
            if c.name in sc.children:
                sc.children.unlink(c)
                holder.children.link(c)
            c.hide_viewport = True
        except Exception:  # noqa: BLE001
            pass

    if rig is not None:
        rig.name = name
        if cam is not None:
            cam.name = name + "_Camera"
    if cam is not None:
        context.scene.camera = cam
    return rig, cam, None

def _load_camera_element(context, element):
    """The shot's own camera. If layout published one, APPEND it (editable shot
    data); otherwise build a fresh Dolly camera rig named after the shot."""
    blend = element.get("blend_local")
    if blend and os.path.isfile(blend):
        holder = _element_holder(context, element["id"])
        with bpy.data.libraries.load(blend, link=False) as (src, dst):
            dst.objects = list(src.objects)
        cam = None
        for o in dst.objects:
            if o is None:
                continue
            if o.name not in holder.objects:
                try:
                    holder.objects.link(o)
                except Exception:  # noqa: BLE001
                    pass
            if getattr(o, "type", "") == "CAMERA" and cam is None:
                cam = o
        if cam is not None:
            context.scene.camera = cam
        return holder, None
    return _build_camera_rig(context, element)

_ELEMENT_LOADERS = {
    "asset": _link_asset_element,
    "camera": _load_camera_element,
    # LATER (lighting round): "cache": _link_alembic_cache,
}

# Shot frame range captured by the Build-shot dialog's invoke(), applied in
# execute() so the timeline matches the shot even when nothing new is built.
_BUILD_FRAME_RANGE = {"start": None, "end": None}

def _scene_unloaded_ids(scene) -> set:
    """Element ids the artist deliberately UNLOADED from this scene (an
    optimised working view). Stored on the scene so it survives sessions;
    the shot breakdown on the server is untouched."""
    try:
        return set(json.loads(scene.get("flumen_unloaded", "") or "[]"))
    except Exception:  # noqa: BLE001
        return set()

def _set_scene_unloaded_ids(scene, ids) -> None:
    scene["flumen_unloaded"] = json.dumps(sorted(set(ids)))

def _apply_build_frame_range(context):
    """Set the scene timeline to the captured shot range. Returns a short message
    (e.g. 'timeline 1001-1100') or '' if no range was captured."""
    fs, fe = _BUILD_FRAME_RANGE.get("start"), _BUILD_FRAME_RANGE.get("end")
    if not fs or not fe:
        return ""
    sc = context.scene
    sc.frame_start, sc.frame_end = int(fs), int(fe)
    if not (int(fs) <= sc.frame_current <= int(fe)):
        sc.frame_current = int(fs)
    # Setting the range doesn't scroll the timeline — the artist would still be
    # LOOKING at the old 0-250 span. Frame every timeline/dope-sheet view.
    try:
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != "DOPESHEET_EDITOR":
                    continue
                region = next((r for r in area.regions if r.type == "WINDOW"),
                              None)
                if region is None:
                    continue
                with context.temp_override(window=window, area=area,
                                           region=region):
                    bpy.ops.action.view_all()
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] timeline view framing skipped:", exc)
    return f"timeline {int(fs)}-{int(fe)}"

def _element_detail(el, present):
    """One-line description of what an element will bring in, for the dialog."""
    if present:
        return "already in scene"
    kind = el.get("kind")
    if kind == "camera":
        return ("new Dolly camera rig" if el.get("load") == "create_rig"
                else "shot camera (published)")
    # Lighting: a resolved alembic cache is imported instead of the geometry.
    if el.get("cache_rel"):
        v = int(el.get("cache_version") or 0)
        return f"load cache v{v:03d}" if v else "load cache"
    src = el.get("source_step") or "?"
    detail = f"link {src}"
    d = el.get("dressing")
    if isinstance(d, dict) and d.get("name"):
        detail += f" + dressing '{d['name']}'"
    if el.get("dressing_error"):
        detail += f" (! {el['dressing_error']})"
    return detail

def _publish_version_label(name):
    """'orso_rig_v004.blend' -> 'v004'; '' when the name carries no version."""
    import re
    m = re.search(r"_v(\d+)\.blend$", os.path.basename(name or ""))
    return f"v{int(m.group(1)):03d}" if m else ""

def _element_loaded_file(holder):
    """Basename of the publish .blend an element's content links from, or ''
    for appended content (the camera rig) which has no library."""
    for o in holder.all_objects:
        lib = getattr(o, "library", None)
        if lib is None:
            ov = getattr(o, "override_library", None)
            ref = getattr(ov, "reference", None) if ov else None
            lib = getattr(ref, "library", None) if ref else None
        if lib is not None:
            try:
                return os.path.basename(bpy.path.abspath(lib.filepath))
            except Exception:  # noqa: BLE001
                return os.path.basename(lib.filepath or "")
    return ""

def _element_update_notes(el, holder, anim_meta):
    """(detail text, update_available) for a Build-shot row: compares what the
    scene HAS against what the server would deliver — the loaded publish vs the
    newest one, and the applied animation version vs the newest published one
    (which, on an animation task, is the layout's until the animator publishes).
    `anim_meta` is resolve-assembly's per-element anim info ({id: {version,…}})."""
    eid = str(el.get("id", ""))
    avail = (anim_meta.get(eid) or {}).get("version", "")
    ld = el.get("look_data") or {}
    look_avail = (f"{ld.get('name', '')} v{int(ld.get('version', 0)):03d}"
                  if ld else "")
    if holder is None:                       # not in the scene yet
        base = _element_detail(el, False)
        if look_avail:
            base += f"  ·  look {look_avail} will apply"
        if avail:
            base += f"  ·  anim {avail} will apply"
        return base, False
    notes, update = [], False
    if el.get("kind") != "camera" and el.get("blend_rel"):
        latest = os.path.basename(el["blend_rel"])
        loaded = _element_loaded_file(holder)
        if loaded and loaded != latest:
            import re
            lv = _publish_version_label(loaded)
            nv = _publish_version_label(latest) or latest
            step = el.get("source_step", "publish")
            # Name the loaded STEP too when it differs (model -> rig upgrade):
            # 'new rig v002 (scene has model v021)', not a bare version clash.
            m = re.search(r"_([a-z0-9]+)_v\d+\.blend$", loaded)
            lstep = m.group(1) if m and m.group(1) != step else ""
            scene_txt = f"{lstep} {lv}".strip() if lv else loaded
            notes.append(f"new {step} {nv}"
                         + (f" (scene has {scene_txt})" if scene_txt else ""))
            update = True
        elif loaded:
            v = _publish_version_label(loaded)
            step = el.get("source_step", "")
            notes.append(f"{step} {v} ✓".strip())
    if look_avail:
        cur_look = str(holder.get("flumen_look", "") or "")
        if cur_look == look_avail:
            notes.append(f"look {look_avail} ✓")
        elif cur_look:
            notes.append(f"new look {look_avail} (scene has {cur_look})")
            update = True
        else:
            notes.append(f"look {look_avail} available")
            update = True
    applied = str(holder.get("flumen_anim", "") or "")
    if avail:
        if applied == avail:
            notes.append(f"anim {avail} ✓")
        elif applied:
            notes.append(f"new anim {avail} (scene has {applied})")
            update = True
        else:
            notes.append(f"anim {avail} available")
            update = True
    return ("  ·  ".join(notes) if notes else "already in scene"), update

# Dynamic per-row step dropdown. The enum items are derived from each row's
# steps_csv; we cache the built lists (keyed by the csv) so the strings stay alive
# — Blender crashes if an items callback returns lists it can garbage-collect.
_STEP_ENUM_CACHE = {}

def _step_enum_items(self, context):
    key = self.steps_csv or ""
    if key not in _STEP_ENUM_CACHE:
        steps = [s for s in key.split(",") if s] or ["model"]
        _STEP_ENUM_CACHE[key] = [
            (s, s.capitalize(), f"Bring in the {s} publish") for s in steps]
    return _STEP_ENUM_CACHE[key]

class FLUMEN_AssemblyItem(bpy.types.PropertyGroup):
    """One row in the Build-shot dialog: an element, which step to bring in, and
    whether to build it."""
    enabled: bpy.props.BoolProperty(name="Build", default=True)
    label: bpy.props.StringProperty()
    kind: bpy.props.StringProperty()
    detail: bpy.props.StringProperty()
    present: bpy.props.BoolProperty(default=False)
    broken: bpy.props.BoolProperty(default=False)   # in scene but content missing
    update: bpy.props.BoolProperty(default=False)   # newer publish/anim available
    unload: bpy.props.BoolProperty(
        name="Unload", default=False,
        description="Remove this element from THIS scene (an optimised view — "
                    "the shot breakdown is untouched; tick it again in a later "
                    "Build shot to load it back)")
    steps_csv: bpy.props.StringProperty()    # available steps, comma-separated
    step: bpy.props.EnumProperty(name="Step", items=_step_enum_items,
                                 description="Which published step to bring in")
    payload: bpy.props.StringProperty()      # json of the resolved element

class FLUMEN_OT_build_shot(bpy.types.Operator):
    bl_idname = "flumen.build_shot"
    bl_label = "Build shot"
    bl_description = ("Bring this shot's breakdown into the scene: link each chosen "
                      "element's rig as a poseable override and build the shot "
                      "camera. Additive — elements already in the scene are left "
                      "untouched, so your posing/animation is never lost")

    # The per-element rows live on the WindowManager (flumen_build_items) — an
    # operator-owned CollectionProperty doesn't reliably populate the props dialog.

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "shot" or not task.get("entity"):
            self.report({"ERROR"}, "No active shot task — open a shot's layout task "
                                   "from the Workspace app.")
            return {"CANCELLED"}
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save into the task first (Flumen ▸ Save into "
                                   "task) — linked rigs need the shot file on disk "
                                   "to store relative paths.")
            return {"CANCELLED"}

        data = self._resolve(task, list_only=True)      # preview, no downloads
        if data is None:
            self.report({"ERROR"}, "Couldn't resolve the shot assembly — launch from "
                                   "the Workspace app and check your connection.")
            return {"CANCELLED"}
        _BUILD_FRAME_RANGE["start"] = data.get("frame_start")
        _BUILD_FRAME_RANGE["end"] = data.get("frame_end")
        listed = data.get("elements") or []
        if not listed:
            # No elements yet, but still set the shot's timeline from its range.
            msg = _apply_build_frame_range(context)
            self.report({"INFO"} if msg else {"WARNING"},
                        f"No elements yet — {msg}." if msg
                        else "Shot has no elements yet. Add them in the Workspace "
                             "app (right-click the shot ▸ Elements…).")
            return {"FINISHED"} if msg else {"CANCELLED"}

        missing_libs = _missing_libraries()
        anim_meta = ((data.get("anim") or {}).get("elements")) or {}
        unloaded = _scene_unloaded_ids(context.scene)
        rows = context.window_manager.flumen_build_items
        rows.clear()
        for el in listed:
            it = rows.add()
            it.payload = json.dumps(el)
            it.kind = el.get("kind", "asset")
            it.label = el.get("label") or el.get("id", "")
            eid = str(el.get("id", ""))
            holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid)
            it.present = holder is not None
            it.unload = False
            # In scene but its publish is gone from disk (e.g. local files
            # cleaned): offer a rebuild, pre-checked.
            it.broken = (holder is not None
                         and _element_content_broken(holder, missing_libs))
            it.detail, it.update = _element_update_notes(el, holder, anim_meta)
            # Updates arrive PRE-TICKED: opening Build shot and clicking Build
            # brings every element to the newest publish + animation. Untick a
            # row to keep what's in the scene (e.g. unpublished local anim on
            # that element — an update re-applies the newest PUBLISHED one).
            it.enabled = (not it.present) or it.broken or it.update
            # Deliberately unloaded from this scene: stays out until the
            # artist opts back in — never silently rebuilt by a routine Build.
            if not it.present and eid in unloaded:
                it.enabled = False
                it.detail = "unloaded from this scene — tick to load it back"
            steps = el.get("available_steps") or []
            it.steps_csv = ",".join(steps)
            if steps and el.get("source_step") in steps:
                it.step = el["source_step"]      # default to the resolved step
        return context.window_manager.invoke_props_dialog(
            self, width=620, title="Build shot", confirm_text="Build")

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Bring these elements into the shot:")
        items = context.window_manager.flumen_build_items
        n_up = sum(1 for it in items if it.update and it.present)
        if n_up:
            col.label(text=f"{n_up} element(s) have a newer publish or "
                           f"animation — pre-ticked to update. Untick to "
                           f"keep what's in the scene.", icon="FILE_REFRESH")
        col.prop(context.window_manager, "flumen_build_apply_anim")
        box = col.box()
        for it in items:
            row = box.row(align=True)
            cb = row.row()
            # Any asset element can be re-ticked to UPDATE to the latest
            # publish (placement is captured and re-applied). A healthy camera
            # stays locked — rebuilding it would lose the camera move — EXCEPT
            # when a newer published animation exists to bring it back.
            cb.enabled = (it.broken or not it.present or it.kind != "camera"
                          or it.update) and not it.unload
            cb.prop(it, "enabled", text="")
            icon = ("TRASH" if it.unload and it.present
                    else "ERROR" if it.broken
                    else "FILE_REFRESH" if it.present and it.update
                    else "CHECKMARK" if it.present
                    else "OUTLINER_OB_CAMERA" if it.kind == "camera"
                    else "OUTLINER_OB_ARMATURE")
            row.label(text=it.label, icon=icon)
            if it.unload and it.present:
                row.label(text="will be UNLOADED from this scene")
            elif it.broken:
                row.label(text="missing on disk — rebuild")
            elif it.present and it.enabled and it.kind != "camera":
                # updating: what's new, and which step to bring back in
                row.label(text=it.detail)
                sub = row.row()
                sub.prop(it, "step", text="")
            elif it.present:
                # in scene: version state (publish + anim), up to date or behind
                row.label(text=it.detail)
            elif it.kind == "camera":
                row.label(text=it.detail)
            else:
                # asset not in scene yet: what will come in (incl. which anim
                # applies) + a step dropdown (rig/model/…) to control the link.
                row.label(text=it.detail)
                sub = row.row()
                sub.enabled = it.enabled
                sub.prop(it, "step", text="")
            if it.present:
                # the unload toggle: build an optimised view by dropping what
                # this scene doesn't need (breakdown untouched, reversible)
                tr = row.row()
                tr.prop(it, "unload", text="", icon="TRASH")

    def execute(self, context):
        task = active_task()
        if not task:
            return {"CANCELLED"}
        chosen, picks, rebuild, update = [], {}, set(), set()
        unloads = []
        present_ct, deselected_ct = 0, 0
        for it in context.window_manager.flumen_build_items:
            if it.unload and it.present:
                unloads.append(json.loads(it.payload)["id"])
            elif it.present and not it.enabled:
                present_ct += 1
            elif it.enabled:
                eid = json.loads(it.payload)["id"]
                chosen.append(eid)
                if it.present:
                    # ticked while in scene: repair if broken, else update to
                    # the latest publish (placement preserved)
                    (rebuild if it.broken else update).add(eid)
                if it.kind == "asset" and it.step:   # honour the chosen step
                    picks[eid] = it.step
            else:
                deselected_ct += 1

        # Unloads happen FIRST (and independently of any building): drop the
        # holder trees, purge what they alone kept alive, and remember the
        # choice on the scene so later Builds don't silently re-add them.
        unloaded_ids = _scene_unloaded_ids(context.scene)
        removed_els = 0
        for eid in unloads:
            holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + str(eid))
            if holder is not None:
                _remove_collection_tree(holder)
                removed_els += 1
            unloaded_ids.add(str(eid))
        if removed_els:
            try:
                for _ in range(3):
                    bpy.data.orphans_purge(do_local_ids=True,
                                           do_linked_ids=True,
                                           do_recursive=True)
            except Exception:  # noqa: BLE001
                pass
            # orphans_purge drops the library's CONTENT but leaves the empty
            # library entry behind — sweep those so the unloaded publish is
            # genuinely out of the file (memory + Missing File checks).
            for lib in list(bpy.data.libraries):
                try:
                    if not lib.users_id:
                        bpy.data.libraries.remove(lib)
                except Exception:  # noqa: BLE001
                    pass
        unloaded_ids -= {str(e) for e in chosen}       # loading back opts in
        _set_scene_unloaded_ids(context.scene, unloaded_ids)

        # Always set the shot's timeline to its frame range, even if nothing new
        # is built (e.g. everything already present).
        tl_msg = _apply_build_frame_range(context)
        if not chosen:
            bits = [f"{present_ct} already in scene"]
            if removed_els:
                bits.insert(0, f"unloaded {removed_els} element(s)")
            if tl_msg:
                bits.append(tl_msg)
            self.report({"INFO"}, ("Nothing to build (" if not removed_els
                                   else "Done: ") + "; ".join(bits)
                        + ("" if removed_els else ")") + ".")
            return {"FINISHED"}

        # downloads only the chosen, at their chosen steps
        missing_before = _missing_libraries()
        data = self._resolve(task, only=chosen, picks=picks)
        elements = (data or {}).get("elements")
        if not elements:
            self.report({"ERROR"}, "Couldn't fetch the selected elements — check "
                                   "your connection and retry.")
            return {"CANCELLED"}
        # Repair, gentlest first: the resolve just re-downloaded the publishes.
        # If a previously-missing library file is back on disk, reloading it
        # heals the existing links in place — animation and posing survive.
        healed_libs = 0
        for lib in missing_before:
            try:
                if os.path.isfile(bpy.path.abspath(lib.filepath)):
                    lib.reload()
                    healed_libs += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[Flumen] library reload failed ({lib.filepath}): {exc}")
        # Per-element animation: each element resolves to its own newest version.
        anim_elements = ((data or {}).get("anim") or {}).get("elements") or {}
        if not context.window_manager.flumen_build_apply_anim:
            # Clean import: publish defaults only — no placements, no camera
            # move. The reset path after a restructure, before re-placing.
            anim_elements = {}
            print("[Flumen] build: published animation NOT applied "
                  "(clean import requested).")

        built, skipped, repaired, animated, dressed = [], [], [], 0, 0
        looked = 0
        snapshots, placement_kept = {}, 0
        for el in elements:
            eid = str(el.get("id", ""))
            if eid in rebuild or eid in update:
                holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid)
                if holder is not None:
                    if eid in rebuild and not _element_content_broken(holder):
                        repaired.append(el)   # reload healed it — keep as-is
                        continue
                    # Update / hard rebuild: remember where the artist PLACED
                    # everything, clear the old content, relink the latest
                    # publish, then put it back where it was.
                    snapshots[eid] = _element_matrix_snapshot(holder)
                    _clear_element_holder(holder)
            loader = _ELEMENT_LOADERS.get(el.get("kind"))
            if loader is None:
                skipped.append((el, "unsupported kind"))
                continue
            try:
                holder, err = loader(context, el)
            except Exception as exc:  # noqa: BLE001 — one bad element never kills it
                holder, err = None, str(exc)
            (built if holder else skipped).append((el, err))
            if holder and eid in snapshots:
                placement_kept += _element_matrix_restore(holder,
                                                          snapshots[eid])
            if holder:
                # Stamp the holder so the playblast HUD can show what's in the shot.
                holder["flumen_step"] = ("camera" if el.get("kind") == "camera"
                                         else el.get("source_step", ""))
                # And the asset entity — the publish snapshot uses it to skip
                # per-piece placement keys on environments (placed as a unit).
                holder["flumen_asset"] = el.get("asset", "")
            # Environment element with a set-dressing: link each manifest prop
            # under the holder and place it at its published transform.
            dressing = el.get("dressing")
            if holder and isinstance(dressing, dict) and dressing.get("props"):
                d_built, d_skipped = _apply_dressing_props(context, holder, el)
                if d_built:
                    holder["flumen_dressing"] = (f"{dressing.get('name', '')} "
                                                 f"v{dressing.get('version', 0):03d}")
                    dressed += d_built
                if d_skipped:
                    print(f"[Flumen] dressing: {d_skipped} prop(s) skipped "
                          f"(already present or failed) on {el.get('id')}")
            if el.get("dressing_error"):
                print(f"[Flumen] dressing warning ({el.get('id')}): "
                      f"{el['dressing_error']}")
            # The element's look, applied at build time: shading comes from the
            # look publish, never from what the geometry publish carried.
            ld = el.get("look_data")
            if holder and isinstance(ld, dict) and ld.get("blend_local"):
                try:
                    n_look = _apply_element_look(holder, ld)
                except Exception as exc:  # noqa: BLE001
                    print("[Flumen] could not apply look:", exc)
                    n_look = 0
                if n_look:
                    holder["flumen_look"] = (f"{ld.get('name', '')} "
                                             f"v{int(ld.get('version', 0)):03d}")
                    looked += 1
            if el.get("look_error"):
                print(f"[Flumen] look warning ({el.get('id')}): "
                      f"{el['look_error']}")
            # Re-apply this element's published animation (its own newest version).
            ael = anim_elements.get(el.get("id"))
            if holder and ael and ael.get("blend_local") and ael.get("objects"):
                try:
                    animated += _apply_element_animation(
                        holder, ael["blend_local"], ael["objects"],
                        content=ael.get("content", ""))
                    holder["flumen_anim"] = ael.get("version", "")
                except Exception as exc:  # noqa: BLE001
                    print("[Flumen] could not apply animation:", exc)

        # Store linked-library paths relative to the shot .blend (cross-machine).
        try:
            bpy.ops.file.make_paths_relative()
        except Exception:  # noqa: BLE001
            pass

        parts = [f"Built {len(built)} element(s)"]
        if removed_els:
            parts.append(f"unloaded {removed_els}")
        if update:
            parts.append(f"updated {len(update & {e.get('id') for e, _ in built})}"
                         f" to the latest publish")
        if placement_kept:
            parts.append(f"placement kept on {placement_kept} object(s)")
        if repaired:
            parts.append(f"repaired {len(repaired)} in place (files re-fetched, "
                         f"animation kept)")
        if dressed:
            parts.append(f"placed {dressed} dressing prop(s)")
        if looked:
            parts.append(f"applied looks on {looked} element(s)")
        if animated:
            parts.append(f"re-applied animation to {animated} object(s)")
        if tl_msg:
            parts.append(tl_msg)
        if present_ct:
            parts.append(f"{present_ct} already in scene")
        if deselected_ct:
            parts.append(f"{deselected_ct} not selected")
        if skipped:
            parts.append("skipped " + ", ".join(
                f"{e.get('id', '?')} ({err})" for e, err in skipped))
        self.report({"INFO"} if built or repaired else {"WARNING"},
                    "; ".join(parts))
        return {"FINISHED"} if built else {"CANCELLED"}

    def _resolve(self, task, list_only=False, only=None, picks=None):
        args = ["resolve-assembly", "--task", task["id"]]
        if list_only:
            args.append("--list")
        for eid in only or []:
            args += ["--only", eid]
        for eid, st in (picks or {}).items():
            args += ["--pick", f"{eid}={st}"]
        cmd, td = _toolkit_cmd(args)
        if cmd is None:
            return None
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True, **_no_window()).strip()
            return json.loads(out.splitlines()[-1]) if out else []
        except Exception:  # noqa: BLE001
            return None

# Published animations for the Load-animation dialog: {version_label: {blend_local,
# elements, by, description}}, set in invoke() and read in execute().
_LOAD_ANIM = {}

_ANIM_ENUM_CACHE = {}

def _anim_version_items(self, context):
    """Per-row version dropdown — the published anim versions that include this
    element, newest first, labelled with the publisher/notes."""
    key = self.versions_csv or ""
    if key not in _ANIM_ENUM_CACHE:
        items = []
        for v in [x for x in key.split(",") if x]:
            meta = _LOAD_ANIM.get(v, {})
            who = meta.get("by") or ""
            # splitlines() on "" is [] — an empty description must not crash.
            lines = (meta.get("description") or "").splitlines()
            desc = lines[0][:32] if lines else ""
            label = v + (f"  ·  {who}" if who else "") + (f"  ·  {desc}" if desc else "")
            items.append((v, label, ""))
        _ANIM_ENUM_CACHE[key] = items or [("", "", "")]
    return _ANIM_ENUM_CACHE[key]

class FLUMEN_AnimItem(bpy.types.PropertyGroup):
    """One row in the Load-animation dialog: an element + which published version to
    load onto it."""
    enabled: bpy.props.BoolProperty(name="Load", default=True)
    element_id: bpy.props.StringProperty()
    label: bpy.props.StringProperty()
    versions_csv: bpy.props.StringProperty()
    version: bpy.props.EnumProperty(name="Version", items=_anim_version_items)

class FLUMEN_OT_load_animation(bpy.types.Operator):
    bl_idname = "flumen.load_animation"
    bl_label = "Load animation"
    bl_description = ("Load published animation onto the shot's elements — pick a "
                      "published version per element (mix versions across elements)")

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "shot":
            self.report({"ERROR"}, "Open a shot task from the Workspace app.")
            return {"CANCELLED"}
        anims = self._list(task)
        if anims is None:
            self.report({"ERROR"}, "Couldn't list animations — launch from the "
                                   "Workspace app and check your connection.")
            return {"CANCELLED"}
        if not anims:
            self.report({"WARNING"}, "No published animation for this shot yet.")
            return {"CANCELLED"}

        global _LOAD_ANIM
        _LOAD_ANIM = {a["version"]: {"blend_local": a.get("blend_local", ""),
                                     "elements": a.get("elements", {}),
                                     "contents": a.get("contents", {}),
                                     "by": a.get("by", ""),
                                     "description": a.get("description", "")}
                      for a in anims}

        in_scene = {c.name[len(ELEMENT_HOLDER_PREFIX):] for c in bpy.data.collections
                    if c.name.startswith(ELEMENT_HOLDER_PREFIX)}
        rows = context.window_manager.flumen_anim_items
        rows.clear()
        for eid in sorted(in_scene):
            versions = [a["version"] for a in anims
                        if eid in (a.get("elements") or {})]   # newest first
            if not versions:
                continue
            it = rows.add()
            it.element_id = eid
            it.label = eid
            it.versions_csv = ",".join(versions)
            it.version = versions[0]
            it.enabled = True
        if not len(rows):
            self.report({"WARNING"}, "No elements in the scene have published "
                                     "animation. Build the shot first.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=520, title="Load animation", confirm_text="Load")

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Choose a published animation per element:")
        box = col.box()
        for it in context.window_manager.flumen_anim_items:
            row = box.row(align=True)
            row.prop(it, "enabled", text="")
            row.label(text=it.label, icon="ARMATURE_DATA")
            sub = row.row()
            sub.enabled = it.enabled
            sub.prop(it, "version", text="")

    def execute(self, context):
        objs, els = 0, 0
        for it in context.window_manager.flumen_anim_items:
            if not it.enabled:
                continue
            data = _LOAD_ANIM.get(it.version)
            holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + it.element_id)
            amap = (data.get("elements") or {}).get(it.element_id) if data else None
            if holder and data and data.get("blend_local") and amap:
                try:
                    n = _apply_element_animation(
                        holder, data["blend_local"], amap,
                        content=(data.get("contents") or {}).get(
                            it.element_id, ""))
                except Exception as exc:  # noqa: BLE001
                    print("[Flumen] load animation failed:", exc)
                    n = 0
                if n:
                    objs += n
                    els += 1
        self.report({"INFO"} if els else {"WARNING"},
                    f"Loaded animation onto {els} element(s) ({objs} object(s)).")
        return {"FINISHED"} if els else {"CANCELLED"}

    def _list(self, task):
        # --all-steps: the picker is a browser — show every step's publishes
        # (layout AND animation and…), labelled per step, newest first.
        cmd, td = _toolkit_cmd(["list-animations", "--task", task["id"],
                                "--all-steps"])
        if cmd is None:
            return None
        try:
            out = subprocess.check_output(cmd, cwd=td, text=True, **_no_window()).strip()
            return json.loads(out.splitlines()[-1]) if out else []
        except Exception:  # noqa: BLE001
            return None
