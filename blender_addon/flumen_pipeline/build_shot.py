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
from . import constraints as _constraints
from . import light_links as _light_links
from ._common import (
    _prefs, _pref_local_root, _toolkit_cmd, PUBLISH_LOG, _publog, _no_window,
    _preflight_server, _shell_toolkit, _shell_json, _apply_one, active_task)
from .looks import _apply_element_look, _match_meshes_by_name


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

def _missing_cache_files():
    """CacheFile datablocks whose .abc no longer exists on disk (e.g. local
    cache files cleaned away, or never synced to this machine). An imported
    Alembic element pointing at one opens frozen at rest — Blender only
    prints per-object 'Could not create cache reader' warnings."""
    out = set()
    for cf in bpy.data.cache_files:
        try:
            if not os.path.isfile(bpy.path.abspath(cf.filepath)):
                out.add(cf)
        except Exception:  # noqa: BLE001
            pass
    return out

def _is_environment(el):
    """Environments are placed as a static unit and are NOT per-object animated
    (publish already skips them in _snapshot_poses). So published animation is
    never re-applied to them on build or cache — otherwise a stale placement/anim
    entry in the manifest would move a backdrop that should stay put."""
    return str((el or {}).get("asset", "")).startswith("environments/")

def _element_content_broken(holder, missing_libs=None, missing_caches=None):
    """True when an element holder's content can't load: its publish file is
    gone from disk (placeholder data), an imported Alembic cache's .abc is
    missing, or the holder is simply empty."""
    if len(holder.all_objects) == 0 and len(holder.children) == 0:
        return True
    caches = (_missing_cache_files() if missing_caches is None
              else missing_caches)
    if caches:
        for o in holder.all_objects:
            for m in getattr(o, "modifiers", []) or []:
                if (getattr(m, "type", "") == "MESH_SEQUENCE_CACHE"
                        and getattr(m, "cache_file", None) in caches):
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
        # ONLY numeric name-clash suffixes ('house.001', 'house.002') count as
        # variants of coll_name — never a real nested sub-collection that merely
        # shares the base name ('house.work' is house's CHILD, not a copy of it).
        # Matching the latter would link a child, then remove it as "not fuller"
        # and gut the parent — leaving an empty override (env with no walls).
        import re
        variant = re.compile(re.escape(coll_name) + r"\.\d+$")
        dotted = sorted((n for n in available
                         if n != coll_name and variant.match(n)),
                        reverse=True)
        candidates = ([coll_name] if coll_name in available else []) + dotted
    if not candidates and available:
        candidates = [available[0]]     # pre-collection publishes: first one
    if not candidates:
        return None, "no linkable collection (republish the rig/model)"

    # Link each candidate and keep the FULLEST. A stale publish can leave a
    # PARTIAL '<base>' collection beside a more complete '<base>.001' (older
    # geometry in one, newly-added props in the other) — and the manifest may
    # name the partial one. Picking the candidate with the most objects (not the
    # first non-empty) recovers the full dressing even from such a publish.
    linked, best = None, -1
    for cand in candidates:
        with bpy.data.libraries.load(blend_local, link=True,
                                     relative=True) as (src, dst):
            dst.collections = [cand]
        got = next((c for c in dst.collections if c is not None), None)
        if got is None:
            continue
        n = len(got.all_objects)
        if n > best:
            if linked is not None:
                try:
                    bpy.data.collections.remove(linked)   # a smaller variant
                except Exception:  # noqa: BLE001
                    pass
            linked, best = got, n
            if cand != coll_name and n > 0:
                print(f"[Flumen] '{coll_name}': linked fuller variant '{cand}' "
                      f"({n} objects) — stale/partial publish; republish to "
                      f"clean it up.")
        else:
            try:
                bpy.data.collections.remove(got)          # not the fullest
            except Exception:  # noqa: BLE001
                pass
    if linked is None or best == 0:
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
    _apply_cache_visibility(path, new)
    return holder, None


def _apply_cache_visibility(cache_path, objects):
    """Replay a cache's per-frame visibility from its '.vis.json' sidecar.

    Alembic carries no animated visibility, so the cache job samples it and
    ships it beside the .abc (see cache_shot._sample_visibility). Without this a
    rig that swaps variants mid-shot — the cat's bandages — comes into lighting
    with EVERY variant permanently visible. Keys go on both hide_viewport (so the
    lighter sees it) and hide_render (so it renders), with CONSTANT interpolation
    so a swap is a hard cut. Silently does nothing for caches published before
    the sidecar existed."""
    side = cache_path[:-len(".abc")] + ".vis.json" if cache_path.endswith(".abc") \
        else cache_path + ".vis.json"
    if not os.path.isfile(side):
        return 0
    try:
        with open(side, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        wanted = data.get("objects") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"[Flumen] cache visibility: unreadable sidecar {side}: {exc}")
        return 0
    if not wanted:
        return 0
    # The .abc rewrites '.001' collision suffixes as '_001' — reuse the same
    # matcher the cache look re-apply uses.
    matched = _match_meshes_by_name(list(wanted), objects)
    applied = 0
    for name, keys in wanted.items():
        o = matched.get(name)
        if o is None:
            continue
        for prop in ("hide_viewport", "hide_render"):
            try:
                for frame, visible in keys:
                    setattr(o, prop, not visible)
                    o.keyframe_insert(prop, frame=frame)
            except Exception:  # noqa: BLE001
                continue
        # Boolean swaps must not interpolate — hold each value until the next.
        ad = getattr(o, "animation_data", None)
        for fc in _action_fcurves(o):
            if fc.data_path in ("hide_viewport", "hide_render"):
                for kp in fc.keyframe_points:
                    kp.interpolation = "CONSTANT"
        applied += 1
    if applied:
        print(f"[Flumen] cache visibility: replayed animated visibility on "
              f"{applied}/{len(wanted)} mesh(es) from {os.path.basename(side)}.")
    return applied

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
        out = subprocess.check_output(cmd, cwd=td, encoding="utf-8", errors="replace", **_no_window()).strip()
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
      * bone NUMERIC custom properties (IK/FK switches, IK stretch, pole/parent
        switches, tail_follow…) — the rig-control state that decides how a limb
        is driven. Unkeyed, these reset to the rig default on rebuild (e.g. an
        arm posed in FK with IK_FK unkeyed flips to IK and shows a T-pose), so
        they are the classic "lost bits" of a rebuilt Rigify character.
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

    def snap_props(pb, prefix, animated):
        """Key the bone's NUMERIC custom properties (IK/FK switches, IK stretch,
        pole/parent switches, tail_follow, rubber_tweak … — the rig-control props
        that decide how a limb is driven) so the animator's setup survives a
        rebuild. Without this, an unkeyed switch resets to the rig default on
        Build shot: e.g. arms posed in FK but with IK_FK unkeyed flip to IK and
        show a T-pose. The numeric filter skips Rigify metadata (rigify_type is a
        string, rigify_parameters a group). Already-animated props are left alone."""
        nonlocal keyed
        try:
            keys = list(pb.keys())
        except Exception:  # noqa: BLE001
            return
        for k in keys:
            if k.startswith("_") or k in ("rigify_type", "rigify_parameters"):
                continue
            try:
                val = pb[k]
            except Exception:  # noqa: BLE001
                continue
            # scalars only — skip arrays/strings/groups (Rigify metadata, etc.)
            if not isinstance(val, (int, float, bool)):
                continue
            full = prefix + ('["%s"]' % k)
            if full in animated:                       # already animated — leave it
                continue
            try:
                if pb.keyframe_insert(data_path='["%s"]' % k, frame=start):
                    keyed += 1
            except Exception:  # noqa: BLE001 — non-animatable / read-only prop
                pass

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
                    prefix = 'pose.bones["%s"]' % pb.name
                    snap(pb, prefix, animated, identity)
                    snap_props(pb, prefix, animated)     # IK/FK switches et al.
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

def _nla_strip_count(obj):
    """How many NLA strips this object carries — animation the active-action
    capture does NOT see (a strip's action is not `animation_data.action`)."""
    ad = getattr(obj, "animation_data", None)
    n = 0
    for tr in getattr(ad, "nla_tracks", []) or []:
        n += len(getattr(tr, "strips", []) or [])
    return n

def _diagnose_element_anim(coll, phase):
    """READ-ONLY inventory of an element holder's animation, printed to the
    System Console. Reveals where 'lost bits' come from: objects whose motion is
    in NLA strips (not captured), parented control/IK-target objects with no
    action of their own (not captured, and not keyed by _snapshot_poses which
    only keys parentless objects), and how many F-curves each captured object
    actually carries. `phase` is 'publish' or 'build'. No behavior change."""
    import sys as _sys
    eid = coll.name[len(ELEMENT_HOLDER_PREFIX):]
    objs = list(coll.all_objects)
    captured = uncaptured = nla = 0
    lines = []
    for o in objs:
        ad = getattr(o, "animation_data", None)
        act = getattr(ad, "action", None) if ad else None
        nstrips = _nla_strip_count(o)
        nla += nstrips
        nfc = len(_action_fcurves(o)) if act else 0
        parented = getattr(o, "parent", None) is not None
        if act:
            captured += 1
            flag = ""
        elif nstrips:
            uncaptured += 1
            flag = "  <== NLA only (strips captured via bindings)"
        elif getattr(o, "type", "") in ("EMPTY", "ARMATURE"):
            # a control/target object with no action: only captured if snapshot
            # keys it, and snapshot skips PARENTED objects — the IK-target trap.
            flag = "  <== no action" + (", PARENTED (snapshot skips)" if parented
                                        else "")
        else:
            flag = ""
        lines.append(
            f"    - {o.name!r} [{getattr(o, 'type', '?')}] "
            f"action={act.name if act else None!r} fcurves={nfc} "
            f"nla_strips={nstrips} parented={parented}{flag}")
    print(f"[Flumen] anim-diag ({phase}) {eid!r}: {len(objs)} object(s), "
          f"{captured} with active action, {uncaptured} NLA-only, "
          f"{nla} NLA strip(s) total", file=_sys.stderr, flush=True)
    if os.environ.get("FLUMEN_ANIM_DEBUG"):
        for ln in lines:
            print(ln, file=_sys.stderr, flush=True)

def _stable_obj_name(o):
    """A per-holder STABLE identity for an object, used to key animation so it
    re-applies correctly across rebuilds and for multiple instances of the same
    asset. Blender collision-suffixes global names (a 2nd orso's 'BODY' becomes
    'BODY.002', and which suffix lands where isn't stable across a rebuild), so
    matching by o.name drops animation on duplicate instances. A library-override
    object carries a reference to its SOURCE object, whose name is fixed in the
    publish and unique within the linked collection — the stable key. Falls back
    to o.name for non-override (locally created) objects like the camera rig."""
    ov = getattr(o, "override_library", None)
    ref = getattr(ov, "reference", None) if ov else None
    return ref.name if ref is not None else o.name

def _anim_data_owners(o):
    """(tag, animation_data) for every animatable container an object carries:
    the object itself, its data block (armature properties, camera lens…) and
    its shape keys. On 4.4+ these are often SLOTS of one shared Action, each
    bound through its own animation_data — capturing only the object level
    loses the rest (a rig's armature-data slot held 1321 of a skeleton's 1392
    F-curves). Tags key the manifest bindings, stable across a rebuild."""
    out = []
    ad = getattr(o, "animation_data", None)
    if ad is not None:
        out.append(("object", ad))
    data = getattr(o, "data", None)
    dad = getattr(data, "animation_data", None) if data is not None else None
    if dad is not None:
        out.append(("data", dad))
    sk = getattr(data, "shape_keys", None) if data is not None else None
    sad = getattr(sk, "animation_data", None) if sk is not None else None
    if sad is not None:
        out.append(("shapekeys", sad))
    return out

def _slot_id(ad):
    """The bound action slot's stable identifier ('' when unslotted/unbound)."""
    try:
        return getattr(getattr(ad, "action_slot", None), "identifier", "") or ""
    except Exception:  # noqa: BLE001
        return ""

def _nla_snapshot(ad):
    """JSON-able capture of an animation_data's NLA stack: ([tracks], {actions}).
    Enough to recreate layered playback on rebuild — track order/mute, each
    strip's action + timing + blend settings. Transition/meta strips (no action)
    are skipped; keyed strip influence isn't carried (static value only)."""
    tracks, acts = [], set()
    for tr in getattr(ad, "nla_tracks", []) or []:
        strips = []
        for s in getattr(tr, "strips", []) or []:
            act = getattr(s, "action", None)
            if act is None:
                continue
            acts.add(act)
            strips.append({
                "name": s.name, "action": act.name,
                "slot": (getattr(getattr(s, "action_slot", None),
                                 "identifier", "") or ""),
                "frame_start": float(s.frame_start),
                "frame_end": float(s.frame_end),
                "action_frame_start": float(s.action_frame_start),
                "action_frame_end": float(s.action_frame_end),
                "repeat": float(getattr(s, "repeat", 1.0)),
                # TIME STRETCH. Without it a strip rebuilds at scale 1.0 and
                # plays its action at the wrong speed: orso_1's 318-frame
                # action was stretched over 382 frames (scale 1.2), so the
                # rebuild ran it ~20% fast, ended 63 frames early and put the
                # character 10 metres from where the animator left it.
                "scale": float(getattr(s, "scale", 1.0)),
                "blend_type": str(s.blend_type),
                "extrapolation": str(s.extrapolation),
                "influence": float(s.influence),
                "use_animated_influence": bool(
                    getattr(s, "use_animated_influence", False)),
                "mute": bool(s.mute),
            })
        # Keep tracks with NO strips too: their MUTE state is real state. An
        # animator in tweak mode typically mutes the other layers, and dropping
        # those tracks silently un-mutes them on rebuild.
        tracks.append({"name": tr.name, "mute": bool(tr.mute),
                       "strips": strips})
    return tracks, acts

def _collect_element_animation(only_ids=None):
    """Gather each element's animation inside the 'element__*' holders — active
    Actions on every level (object, data, shape keys) plus each level's NLA
    stack. Returns (set_of_actions, {eid: {stable_name: action_name}},
    {eid: {stable_name: bindings}}): the first two feed libraries.write + the
    manifest's legacy 'elements' map (unchanged shape, old consumers keep
    working); the third is the new 'bindings' section — per level: action,
    bound slot identifier and NLA tracks. Objects are keyed by their STABLE
    source name (see _stable_obj_name). `only_ids` limits to those elements."""
    actions = set()
    elem_actions = {}
    elem_bindings = {}
    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        eid = coll.name[len(ELEMENT_HOLDER_PREFIX):]
        if only_ids is not None and eid not in only_ids:
            continue
        _diagnose_element_anim(coll, "publish")
        mapping, binds = {}, {}
        for o in coll.all_objects:
            b = {}
            for tag, ad in _anim_data_owners(o):
                act = getattr(ad, "action", None)
                tracks, strip_acts = _nla_snapshot(ad)
                if act is None and not tracks:
                    continue
                if act is not None:
                    actions.add(act)
                actions.update(strip_acts)
                entry = {"action": act.name if act else "",
                         "slot": _slot_id(ad)}
                if tracks:
                    entry["nla"] = tracks
                # NLA TWEAK MODE. While tweaking, the active action REPLACES the
                # strip being edited. Rebuild without it and the strip AND the
                # active action both evaluate — the same action applied twice,
                # which threw orso_1 ten metres off while orso (not in tweak
                # mode) was perfect.
                if getattr(ad, "use_tweak_mode", False):
                    entry["tweak"] = True
                b[tag] = entry
            # Constraints the ANIMATOR added (Child Of handing the sheet from
            # the bat to the bear…). They live only in the local override, so a
            # rebuild loses them and the pose animated against them breaks.
            # Captured even on an object with no action of its own.
            csnap = _constraints.snapshot(o)
            if csnap:
                b["constraints"] = csnap
            if not b:
                continue
            stable = _stable_obj_name(o)
            if (b.get("object") or {}).get("action"):
                mapping[stable] = b["object"]["action"]  # legacy map, unchanged
            binds[stable] = b
        if mapping:
            elem_actions[eid] = mapping
        if binds:
            elem_bindings[eid] = binds
    return actions, elem_actions, elem_bindings

def _ad_fcurves(ad):
    """Every F-curve an animation_data's active action drives through its BOUND
    slot (legacy .fcurves + 4.4+ slotted channelbag)."""
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

def _action_fcurves(obj):
    """Every F-curve of an object's active action (legacy + 4.4+ slotted channelbag)."""
    return _ad_fcurves(getattr(obj, "animation_data", None))

def _strip_fcurves(s):
    """Every F-curve an NLA strip's action drives through the STRIP's bound slot
    (falling back to all slots' channelbags when unbound)."""
    act = getattr(s, "action", None)
    if not act:
        return []
    fcs = list(getattr(act, "fcurves", []) or [])           # legacy
    slot = getattr(s, "action_slot", None)
    slots = [slot] if slot else list(getattr(act, "slots", []) or [])
    for layer in getattr(act, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            for sl in slots:
                try:
                    cbag = strip.channelbag(sl)
                except Exception:  # noqa: BLE001
                    cbag = None
                if cbag:
                    fcs.extend(cbag.fcurves)
    return fcs

def _element_anim_hashes(only_ids=None):
    """A deterministic content hash per element with animation: a sha1 of every
    object's F-curves (data_path#index = frame:value;…, rounded + sorted), plus
    data-level/shape-key curves and the NLA stack (strip curves + timing).
    Identical animation -> identical hash, so a publish can tell what actually
    changed. The object-level part strings are unchanged from the original
    format, so elements with only a plain single-slot object action keep their
    historical hash — no spurious 'changed' flags after upgrading."""
    import hashlib

    def _kfs(fc):
        return ";".join(f"{k.co[0]:.4f}:{k.co[1]:.6f}"
                        for k in fc.keyframe_points)

    out = {}
    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        eid = coll.name[len(ELEMENT_HOLDER_PREFIX):]
        if only_ids is not None and eid not in only_ids:
            continue
        parts = []
        for o in coll.all_objects:
            for tag, ad in _anim_data_owners(o):
                pre = "" if tag == "object" else f"{tag}:"
                for fc in _ad_fcurves(ad):
                    parts.append(f"{o.name}/{pre}{fc.data_path}"
                                 f"#{fc.array_index}={_kfs(fc)}")
                for tr in getattr(ad, "nla_tracks", []) or []:
                    for s in getattr(tr, "strips", []) or []:
                        if getattr(s, "action", None) is None:
                            continue
                        head = (f"{o.name}/{tag}:nla/{tr.name}/{s.name}"
                                f"@{s.frame_start:.2f}-{s.frame_end:.2f}"
                                f"/{s.blend_type}/{s.influence:.3f}"
                                f"/{int(bool(s.mute or tr.mute))}")
                        parts.append(head)
                        for fc in _strip_fcurves(s):
                            parts.append(f"{head}/{fc.data_path}"
                                         f"#{fc.array_index}={_kfs(fc)}")
            # Animator-added constraints are published state too: adding or
            # retargeting one must read as 'changed' in the publish dialog.
            # Appended only when there ARE any, so elements without them keep
            # their historical hash (no spurious 'changed' after this upgrade).
            csnap = _constraints.snapshot(o)
            if csnap:
                parts.append(f"{o.name}/constraints="
                             f"{_constraints.digest(csnap)}")
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

# Read-only re-apply trace, inspectable from the Python console after a build:
#   import flumen_pipeline.build_shot as B; [print(x) for x in B._ANIM_DEBUG_LOG]
# Records, per element, what the animation re-apply received and matched — so a
# case like "instance 2's visibility keys never land" can be seen exactly.
_ANIM_DEBUG_LOG = []

def _bind_slot(ad, act, identifier=""):
    """Bind an animation_data to the action slot with the recorded identifier
    (falling back to the first slot when unrecorded/missing). No-op pre-4.4."""
    try:
        slots = list(getattr(act, "slots", []) or [])
        if not slots:
            return
        want = next((s for s in slots
                     if getattr(s, "identifier", "") == identifier), None)
        if want is not None:
            ad.action_slot = want
        elif getattr(ad, "action_slot", None) is None:
            ad.action_slot = slots[0]
    except Exception:  # noqa: BLE001
        pass

def _rebuild_nla(ad, tracks, loaded):
    """Recreate a captured NLA stack (see _nla_snapshot) on an animation_data,
    replacing whatever stack is there — the published state is the truth on a
    build/load. Returns the number of strips created."""
    if ad is None or not tracks:
        return 0
    try:
        for tr in list(getattr(ad, "nla_tracks", []) or []):
            ad.nla_tracks.remove(tr)
    except Exception:  # noqa: BLE001
        pass
    made = 0
    for t in tracks:                      # captured bottom-first; .new() appends
        try:
            tr = ad.nla_tracks.new()
        except Exception:  # noqa: BLE001
            continue
        tr.name = t.get("name") or tr.name
        tr.mute = bool(t.get("mute"))
        for s in t.get("strips") or []:
            act = loaded.get(s.get("action", ""))
            if act is None:
                continue
            try:
                st = tr.strips.new(s.get("name") or act.name,
                                   int(round(float(s.get("frame_start", 1)))),
                                   act)
            except Exception as exc:  # noqa: BLE001 — overlap/bad range
                print(f"[Flumen] NLA strip '{s.get('name')}' not recreated: "
                      f"{exc}")
                continue
            # ORDER MATTERS. frame_end is DERIVED —
            #   frame_end = frame_start + action_length * repeat * scale
            # — so Blender recomputes it whenever repeat or scale changes.
            # Setting frame_end before them (as this did) meant repeat=1.0 threw
            # the captured end away and the strip collapsed back to the raw
            # action length, dropping any time stretch. Set the inputs first and
            # frame_end LAST, so it also repairs manifests published before
            # 'scale' was captured (there, frame_end alone carries the stretch).
            # SCALE is the strip's time stretch and Blender does NOT derive it
            # from frame_end — set it explicitly. Manifests published before it
            # was captured carry the stretch only implicitly, as the ratio of
            # the scene span to the action span, so reconstruct it there:
            #   scale = (frame_end-frame_start) / (action_span * repeat)
            # Without this a 1.2x strip rebuilds at 1.0 and the action plays 50
            # frames out of sync (orso_1 landed 10 m away).
            vals = dict(s)
            if vals.get("scale") is None:
                try:
                    span = float(vals["frame_end"]) - float(vals["frame_start"])
                    a_span = (float(vals["action_frame_end"])
                              - float(vals["action_frame_start"]))
                    rep = float(vals.get("repeat") or 1.0) or 1.0
                    if a_span > 0 and span > 0:
                        vals["scale"] = span / (a_span * rep)
                except Exception:  # noqa: BLE001
                    pass
            for attr in ("action_frame_start", "action_frame_end",
                         "repeat", "scale", "frame_start", "frame_end",
                         "influence"):
                if vals.get(attr) is None:
                    continue
                try:
                    setattr(st, attr, float(vals[attr]))
                except Exception:  # noqa: BLE001
                    pass
            for attr in ("blend_type", "extrapolation"):
                if s.get(attr):
                    try:
                        setattr(st, attr, s[attr])
                    except Exception:  # noqa: BLE001
                        pass
            try:
                st.use_animated_influence = bool(s.get("use_animated_influence"))
            except Exception:  # noqa: BLE001
                pass
            st.mute = bool(s.get("mute"))
            if s.get("slot"):
                try:
                    want = next((sl for sl in getattr(act, "slots", []) or []
                                 if getattr(sl, "identifier", "") == s["slot"]),
                                None)
                    if want is not None:
                        st.action_slot = want
                except Exception:  # noqa: BLE001
                    pass
            made += 1
    return made

def _apply_object_bindings(o, b, loaded):
    """Apply one object's captured non-object-level animation: the data-block
    and shape-key actions (with their slot bindings) and every level's NLA
    stack. `b` is this object's bindings entry; `loaded` maps manifest action
    name -> appended datablock. Returns how many things were applied."""
    did = 0
    data = getattr(o, "data", None)
    sk = getattr(data, "shape_keys", None) if data is not None else None
    owners = {"object": o, "data": data, "shapekeys": sk}
    for tag, target in owners.items():
        t = b.get(tag) or {}
        if target is None or not t:
            continue
        act = loaded.get(t.get("action", "")) if tag != "object" else None
        if act is not None:
            try:
                target.animation_data_create()
                target.animation_data.action = act
                _bind_slot(target.animation_data, act, t.get("slot", ""))
                did += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[Flumen] {tag} action on '{o.name}' not applied: {exc}")
        ad = getattr(target, "animation_data", None)
        if t.get("nla") and ad is not None:
            did += _rebuild_nla(ad, t["nla"], loaded)
            _restore_tweak_mode(ad, t)
    return did


def _restore_tweak_mode(ad, entry):
    """Re-enter NLA tweak mode when the animator was in it.

    In tweak mode Blender plays the action THROUGH the strip being edited, so
    the strip's time mapping applies (Francesco's orso_1 strip is stretched
    1.2x: scene frame 1301 reads action frame 1251). A rebuild without it
    evaluates the action directly at scene time — 50 frames out of sync, the
    character 10 m away.

    The action must STAY assigned: tweak mode is 'this action is the one being
    edited in that strip', not 'the strip replaces the action'. Clearing it
    instead makes the strip the only source, and with extrapolation=NOTHING the
    pose collapses to rest the moment the strip ends (the character snapped to
    the origin after frame 1382). Blender needs the strip flagged as selected
    to know which one is being tweaked.

    OLD MANIFESTS: a publish made with a pre-0.18.12 add-on never recorded the
    flag (v034 of SEQ010/SH0010, published from a stale machine, re-broke
    orso_1 this way). But the state itself is self-evident: the ACTIVE action
    also living in a strip is only ever a tweak-mode capture — outside tweak
    mode Blender would evaluate the same action twice, at two different time
    mappings, which no animator means. So when the flag is absent but the
    active action is found among the strips, enter tweak mode anyway."""
    act = getattr(ad, "action", None)
    if act is None:
        return
    found = False
    for tr in getattr(ad, "nla_tracks", []) or []:
        for st in getattr(tr, "strips", []) or []:
            if getattr(st, "action", None) is act:
                try:
                    st.select = True
                    found = True
                except Exception:  # noqa: BLE001
                    pass
    if not found:
        return
    if not entry.get("tweak"):
        print(f"[Flumen] active action '{act.name}' also lives in an NLA "
              f"strip but the manifest has no tweak flag (published by an "
              f"old add-on) — entering tweak mode to keep the strip's time "
              f"mapping.")
    try:
        ad.use_tweak_mode = True
    except Exception as exc:  # noqa: BLE001
        print(f"[Flumen] could not re-enter NLA tweak mode: {exc}")


def _apply_element_animation(holder, anim_blend, action_map, content="",
                             bindings=None):
    """Append the published Actions and assign them onto this element's objects by
    name, so a freshly-built element comes back animated. `content` = the
    publish the animation was captured against (stale-placement guard).
    `bindings` (newer manifests) adds per-object slot identifiers, data-block +
    shape-key actions and NLA stacks; older manifests without it re-apply the
    object-level actions exactly as before."""
    action_map = action_map or {}
    bindings = bindings or {}
    dbg = {"holder": holder.name, "in_keys": sorted(action_map),
           "bind_keys": sorted(bindings),
           "content": content, "have_blend": bool(anim_blend and anim_blend
                                                   and os.path.isfile(anim_blend or "")),
           "loaded": [], "matched": [], "applied": 0, "note": ""}
    _ANIM_DEBUG_LOG.append(dbg)
    if not (anim_blend and (action_map or bindings)
            and os.path.isfile(anim_blend)):
        dbg["note"] = "no blend / empty map"
        return 0
    _diagnose_element_anim(holder, "build")
    # NOTE: the old _stale_content_filter (drop every non-armature key when the
    # linked publish differs from capture) is intentionally NOT applied anymore.
    # Animation is now matched by STABLE override-reference name, so a key only
    # ever lands on the object whose source name matches — a stale/restructured
    # key finds no match and is harmlessly ignored. The blanket filter was both
    # unnecessary and destructive: it was deleting legitimate per-object keys
    # (a 2nd instance's hide_viewport visibility) whenever it mis-fired.
    dbg["after_filter_keys"] = sorted(action_map)
    want = set(action_map.values())
    for b in bindings.values():           # data/shape-key/NLA actions too
        for t in b.values():
            if t.get("action"):
                want.add(t["action"])
            for tr in t.get("nla") or []:
                for s in tr.get("strips") or []:
                    if s.get("action"):
                        want.add(s["action"])
    with bpy.data.libraries.load(anim_blend, link=False) as (src, dst):
        all_src = list(src.actions)       # every action name in the anim blend
        req_names = [a for a in all_src if a in want]
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
    dbg["want"] = sorted(want)
    dbg["blend_missing_wanted"] = sorted(want - set(all_src))  # capture never wrote these
    dbg["loaded"] = sorted(loaded)
    # Exact names first, then a BASE-NAME fallback scoped to this holder:
    # model elements' object names carry scene-dependent .00N suffixes (every
    # model publish ships a 'PUBLISH' root empty — a layout with twelve model
    # elements numbers them by link order), so the layout's 'PUBLISH.003' is
    # a fresh animation scene's 'PUBLISH.001'. Within one holder the base
    # name is unambiguous; the fallback only fires when it's unique on BOTH
    # sides (the manifest and the scene).
    keyspace = set(action_map) | set(bindings)
    manifest_by_base = {}
    for name in keyspace:
        b = name.split(".")[0]
        manifest_by_base[b] = None if b in manifest_by_base else name
    holder_objs = list(holder.all_objects)
    base_count = {}
    for o in holder_objs:
        b = o.name.split(".")[0]
        base_count[b] = base_count.get(b, 0) + 1
    applied = 0
    for o in holder_objs:
        # Match by STABLE source name first (the override reference — robust to
        # Blender's collision suffixes and to multiple instances of the same
        # asset, e.g. two orsos whose 'BODY' meshes rebuild under different
        # global names). Then the exact global name (manifests published before
        # this change were keyed that way), then the unique-base fallback.
        ref = _stable_obj_name(o)
        key = (ref if ref in keyspace
               else o.name if o.name in keyspace
               else None)
        if key is None and base_count.get(o.name.split(".")[0]) == 1:
            key = manifest_by_base.get(o.name.split(".")[0])
        act = loaded.get(action_map.get(key, "")) if key else None
        b = bindings.get(key) or {} if key else {}
        dbg["matched"].append((o.name, ref, key, action_map.get(key),
                               act is not None))
        did = 0
        if act is not None:
            o.animation_data_create()
            o.animation_data.action = act
            # Blender 4.4+ slotted actions: a slot must be bound to drive the
            # object. Prefer the slot recorded at capture; fall back to the
            # auto-bind / first slot. (No-op on older Blender without slots.)
            _bind_slot(o.animation_data, act,
                       (b.get("object") or {}).get("slot", ""))
            did += 1
        if b:
            did += _apply_object_bindings(o, b, loaded)
        if did:
            applied += 1
    dbg["applied"] = applied
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
        label = f"load cache v{v:03d}" if v else "load cache"
        if el.get("cache_pinned"):
            label += " · APPROVED"      # pinned via the app, not just newest
        # Flag where it comes from so the reviewer knows whether the shot is
        # building from the server or a not-yet-uploaded local cache.
        label += (" · LOCAL (not on server)" if el.get("cache_source") == "local"
                  else " · server")
        return label
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

def _holder_cache_version(holder):
    """The cache version an element's holder currently plays, parsed from its
    CacheFile paths ('…/gatto_mummia_v001.abc' -> 1). 0 when the holder has no
    cache at all (e.g. an earlier build fell back to linking the rig)."""
    import re
    best = 0
    for o in holder.all_objects:
        for mod in getattr(o, "modifiers", []):
            cf = getattr(mod, "cache_file", None)
            fp = getattr(cf, "filepath", "") if cf else ""
            m = re.search(r"_v(\d+)\.abc$", os.path.basename(fp))
            if m:
                best = max(best, int(m.group(1)))
    return best

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


def _holder_libs(holder):
    """{library basename: object count} for every distinct library the holder's
    objects link (directly or via an override reference)."""
    from collections import Counter
    counts = Counter()
    for o in holder.all_objects:
        lib = getattr(o, "library", None)
        if lib is None:
            ov = getattr(o, "override_library", None)
            ref = getattr(ov, "reference", None) if ov else None
            lib = getattr(ref, "library", None) if ref else None
        if lib is None:
            continue
        try:
            base = os.path.basename(bpy.path.abspath(lib.filepath))
        except Exception:  # noqa: BLE001
            base = os.path.basename(lib.filepath or "")
        if base:
            counts[base] += 1
    return counts


def _loaded_step_file(holder, latest):
    """Basename of the library in `holder` that matches `latest`'s publish stem
    (everything before the _vNNN), so a mixed holder — e.g. an environment that
    holds BOTH the model override and the dressing override — reports the version
    of the SAME step it is being compared against, not whichever library the
    object iterator happened to hit first. Picks the most-used matching library
    (an orphaned old version lingers with few/no holder objects). Falls back to
    the first-library heuristic when nothing matches the stem."""
    import re
    stem = re.sub(r"_v\d+\.blend$", "_", os.path.basename(latest or ""))
    if stem:
        matches = {b: n for b, n in _holder_libs(holder).items()
                   if b.startswith(stem)}
        if matches:
            return max(matches, key=matches.get)
    return _element_loaded_file(holder)

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
    # Lighting: a cache element imports a baked alembic (geometry + animation
    # are IN the cache), so the anim "will apply" notes don't apply. But the
    # cache VERSION and the look (applied onto the cache at build time) both
    # move on their own tracks — compare each against what the scene holds, so
    # a re-cache or a look republish pre-ticks the row. Without this a lighting
    # scene silently kept its first-ever cache forever (the cat rendered with
    # v001 face sets under a v006 look).
    if el.get("cache_rel"):
        if holder is None:
            base = _element_detail(el, False)
            if look_avail:
                base += f"  ·  look {look_avail} will apply"
            return base, False
        notes, update = [], False
        latest = int(el.get("cache_version") or 0)
        pinned = bool(el.get("cache_pinned"))
        have = _holder_cache_version(holder)
        # An APPROVED (pinned) version must win in BOTH directions — a
        # rollback pin is older than what the scene plays, so 'have != latest'
        # flags it where the plain newest-only check would stay silent.
        if latest and have and (have < latest or (pinned and have != latest)):
            what = "approved cache" if pinned else "new cache"
            notes.append(f"{what} v{latest:03d} (scene has v{have:03d})")
            update = True
        elif latest and not have:
            notes.append(f"cache v{latest:03d} available (scene has no cache)")
            update = True
        elif latest:
            notes.append(f"cache v{latest:03d}"
                         + (" (approved)" if pinned else "") + " ✓")
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
        return ("  ·  ".join(notes) if notes else "already in scene"), update
    # Set dressing versions on its OWN track — the dresser re-publishes without
    # touching the env model, so a geometry-only update check misses it. --list
    # inlines {name, version}; the label matches the holder's flumen_dressing
    # stamp so a newer dressing flags an update (and pre-ticks the env row).
    dr = el.get("dressing") or {}
    dress_avail = (f"{dr.get('name', '')} v{int(dr.get('version', 0)):03d}"
                   if isinstance(dr, dict) and dr.get("name") else "")
    if holder is None:                       # not in the scene yet
        base = _element_detail(el, False)
        if look_avail:
            base += f"  ·  look {look_avail} will apply"
        if dress_avail:
            base += f"  ·  dressing {dress_avail} will apply"
        if avail and not _is_environment(el):   # environments are never animated
            base += f"  ·  anim {avail} will apply"
        return base, False
    notes, update = [], False
    if el.get("kind") != "camera" and el.get("blend_rel"):
        latest = os.path.basename(el["blend_rel"])
        # Match the SAME step's library (model vs dressing) — an environment
        # holder mixes both, and a first-object heuristic would compare the model
        # publish against a dressing library and flag a phantom "new model".
        loaded = _loaded_step_file(holder, latest)
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
    if dress_avail:
        cur_dress = str(holder.get("flumen_dressing", "") or "")
        if cur_dress == dress_avail:
            notes.append(f"dressing {dress_avail} ✓")
        elif cur_dress:
            notes.append(f"new dressing {dress_avail} (scene has {cur_dress})")
            update = True
        else:
            notes.append(f"dressing {dress_avail} available")
            update = True
    applied = str(holder.get("flumen_anim", "") or "")
    if avail and not _is_environment(el):    # environments are never animated
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
    is_cache: bpy.props.BoolProperty(default=False)  # lighting: loads a baked cache
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
        missing_caches = _missing_cache_files()
        anim_meta = ((data.get("anim") or {}).get("elements")) or {}
        unloaded = _scene_unloaded_ids(context.scene)
        is_lighting = task.get("step") == "lighting"
        rows = context.window_manager.flumen_build_items
        rows.clear()
        for el in listed:
            it = rows.add()
            it.payload = json.dumps(el)
            it.kind = el.get("kind", "asset")
            it.is_cache = bool(el.get("cache_rel"))   # lighting: loads a cache
            it.label = el.get("label") or el.get("id", "")
            eid = str(el.get("id", ""))
            holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid)
            it.present = holder is not None
            it.unload = False
            # In scene but its publish (or imported cache .abc) is gone from
            # disk (e.g. local files cleaned): offer a rebuild, pre-checked.
            it.broken = (holder is not None
                         and _element_content_broken(holder, missing_libs,
                                                     missing_caches))
            it.detail, it.update = _element_update_notes(el, holder, anim_meta)
            # Updates arrive PRE-TICKED: opening Build shot and clicking Build
            # brings every element to the newest publish + animation. Untick a
            # row to keep what's in the scene (e.g. unpublished local anim on
            # that element — an update re-applies the newest PUBLISHED one).
            it.enabled = (not it.present) or it.broken or it.update
            # Lighting builds from baked caches. An animated asset with no cache
            # yet would fall back to its rig/model — don't auto-import that; leave
            # it unticked so the lighter opts in. Environments (static link +
            # set-dressing) and the camera have no cache by nature and still
            # build by default.
            if (is_lighting and it.kind == "asset" and not it.is_cache
                    and not _is_environment(el)):
                it.enabled = False
                if not it.present:
                    it.detail = "no cache yet — tick to link the rig"
            # Deliberately unloaded from this scene: stays out until the
            # artist opts back in — never silently rebuilt by a routine Build.
            if not it.present and eid in unloaded:
                it.enabled = False
                it.detail = "unloaded from this scene — tick to load it back"
            # Diagnostic (System Console): the real per-row state, so a row that
            # is stuck "needs rebuild" can be traced — is it broken (missing lib),
            # or update (loaded file vs latest, or anim/look)?
            import sys as _sys
            print(f"[Flumen] build row: {eid!r} present={it.present} "
                  f"broken={it.broken} update={it.update} "
                  f"loaded={_element_loaded_file(holder) if holder else '-'!r} "
                  f"cache_rel={bool(el.get('cache_rel'))} "
                  f"anim_avail={(anim_meta.get(eid) or {}).get('version', '')!r} "
                  f"| {it.detail}", file=_sys.stderr, flush=True)
            steps = el.get("available_steps") or []
            it.steps_csv = ",".join(steps)
            if steps and el.get("source_step") in steps:
                it.step = el["source_step"]      # default to the resolved step
        return context.window_manager.invoke_props_dialog(
            self, width=900, title="Build shot", confirm_text="Build")

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
            # The step (Model/Rig…) picker is only meaningful for an asset that
            # LINKS a published step and has more than one to choose. A lighting
            # cache row imports a baked alembic — no step to pick — and a single-
            # step asset has nothing to choose, so hide the dropdown in both.
            show_step = (not it.is_cache
                         and len([s for s in it.steps_csv.split(",") if s]) > 1)
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
                # updating: what's new, and (for multi-step assets) which step
                row.label(text=it.detail)
                if show_step:
                    sub = row.row()
                    sub.prop(it, "step", text="")
            elif it.present:
                # in scene: version state (publish + anim), up to date or behind
                row.label(text=it.detail)
            elif it.kind == "camera":
                row.label(text=it.detail)
            else:
                # asset not in scene yet: what will come in, + a step dropdown
                # only when there's actually a step to choose (not a cache).
                row.label(text=it.detail)
                if show_step:
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
        _ANIM_DEBUG_LOG.clear()          # fresh trace for this build
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
        # Caches missing BEFORE the resolve re-downloads them: unlike a library
        # (healed in place by lib.reload() below), a CacheFile whose reader
        # already failed can't be reloaded reliably — those elements get a hard
        # rebuild (clear + re-import of the freshly fetched cache).
        missing_caches_before = _missing_cache_files()
        # Light linking, captured BEFORE any holder is cleared: deleting an
        # element's objects silently drops them from every receiver/blocker
        # collection, wiping the lighter's linking. Restored by name after the
        # rebuilds below.
        ll_snapshot = _light_links.snapshot() if (rebuild or update) else {}
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
        anim_warnings = []   # (id, wanted objects, holder objects) for anim that
                             # matched nothing — its keyed objects aren't in the link
        snapshots, placement_kept = {}, 0
        for el in elements:
            eid = str(el.get("id", ""))
            if eid in rebuild or eid in update:
                holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid)
                if holder is not None:
                    if (eid in rebuild
                            and not _element_content_broken(
                                holder, missing_caches=missing_caches_before)):
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
            # under the holder and place it at its published transform, plus the
            # dresser's local 'extras' geometry. A dressing can be extras-only
            # (no placed props), so fire on either.
            dressing = el.get("dressing")
            if (holder and isinstance(dressing, dict)
                    and (dressing.get("props") or dressing.get("extras"))):
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
            # Environments are excluded — they are placed as a static unit, never
            # animated, so a stale manifest entry must not move them.
            ael = anim_elements.get(el.get("id"))
            if (holder and ael and ael.get("blend_local")
                    and (ael.get("objects") or ael.get("bindings"))
                    and not _is_environment(el)):
                try:
                    n_anim = _apply_element_animation(
                        holder, ael["blend_local"], ael["objects"],
                        content=ael.get("content", ""),
                        bindings=ael.get("bindings"))
                    animated += n_anim
                    holder["flumen_anim"] = ael.get("version", "")
                    # Animation was published but bound to NOTHING: the objects it
                    # keys don't exist in the linked content (e.g. an anim keyed on
                    # a rig control 'Benda_BBone_RIG' while the model publishes only
                    # 'PUBLISH'/'bendage', or a missing cache). The element imports
                    # static at origin — flag it loudly instead of failing silently.
                    if n_anim == 0:
                        want = sorted(ael["objects"].keys())
                        have = sorted({o.name.split(".")[0]
                                       for o in holder.all_objects})
                        anim_warnings.append((el.get("id", "?"), want, have))
                        print(f"[Flumen] ANIM MISMATCH ({el.get('id')}): animation "
                              f"targets {want} but the linked content has "
                              f"{have} — element is STATIC at origin. Publish a "
                              f"cache for it, or bake its animation onto 'PUBLISH'.")
                except Exception as exc:  # noqa: BLE001
                    print("[Flumen] could not apply animation:", exc)
            else:
                # Record why the re-apply was skipped for this element, so a case
                # like "instance 2 got no animation" is visible in _ANIM_DEBUG_LOG.
                _ANIM_DEBUG_LOG.append({
                    "holder": (holder.name if holder else None),
                    "element_id": el.get("id"),
                    "skipped": True, "has_holder": bool(holder),
                    "has_ael": bool(ael),
                    "ael_objects": sorted((ael or {}).get("objects") or {}),
                    "is_env": _is_environment(el)})

        # SECOND PASS: animator-added constraints. Deliberately after the whole
        # element loop — a Child Of on the bat's bone targets the bear's sheet,
        # and while the loop is still running that element may not exist yet, so
        # the target would resolve to None and the constraint land dead.
        constrained = 0
        for el in elements:
            ael = anim_elements.get(el.get("id"))
            holder = bpy.data.collections.get(
                ELEMENT_HOLDER_PREFIX + str(el.get("id", "")))
            if not (holder and ael and ael.get("bindings")) \
                    or _is_environment(el):
                continue
            try:
                constrained += _constraints.restore(holder, ael["bindings"])
            except Exception as exc:  # noqa: BLE001
                print(f"[Flumen] could not restore constraints on "
                      f"{el.get('id')}: {exc}")
        if constrained:
            print(f"[Flumen] restored {constrained} animator constraint(s)")

        # Put the lighter's light linking back: the cleared elements' objects
        # were silently dropped from every receiver/blocker collection when
        # they were deleted — re-link the re-imported ones by name.
        if ll_snapshot:
            try:
                n_ll, ll_warns = _light_links.restore(ll_snapshot)
            except Exception as exc:  # noqa: BLE001
                n_ll, ll_warns = 0, [f"restore failed: {exc}"]
            for w in ll_warns:
                print(f"[Flumen] light links: {w}")
            if n_ll:
                print(f"[Flumen] light links: re-linked {n_ll} member(s) "
                      f"after the update")

        # Updating/rebuilding an element CLEARS its old content but leaves the old
        # publish's library orphaned — e.g. an environment updated model v008 ->
        # v009 keeps BOTH linked, a broken library graph that can crash Blender's
        # UI when the work file is reopened. Purge orphaned LINKED data and sweep
        # the now-empty library entries (as the unload path does), so a superseded
        # version is genuinely gone. do_local_ids stays False — only drop stale
        # linked publishes, never local data the build just created.
        if update or rebuild:
            try:
                for _ in range(3):
                    bpy.data.orphans_purge(do_local_ids=False,
                                           do_linked_ids=True,
                                           do_recursive=True)
            except Exception:  # noqa: BLE001
                pass
            for lib in list(bpy.data.libraries):
                try:
                    if not lib.users_id:
                        bpy.data.libraries.remove(lib)
                except Exception:  # noqa: BLE001
                    pass

        # Store linked-library paths relative to the shot .blend (cross-machine).
        try:
            bpy.ops.file.make_paths_relative()
        except Exception:  # noqa: BLE001
            pass

        parts = [f"Built {len(built)} element(s)"]
        # Download summary from resolve-assembly — shows the skip optimisation at
        # work (how much was re-fetched vs already local) right in the status bar.
        fetch = (data or {}).get("_fetch") or {}
        if fetch.get("downloaded") or fetch.get("skipped"):
            parts.append(f"fetched {fetch['downloaded']} file(s) "
                         f"({fetch.get('bytes', 0) / 1e6:.0f} MB), "
                         f"{fetch['skipped']} already up-to-date")
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
        # Animation that bound to nothing — the element is in the scene but frozen
        # at origin. Loud, named, and actionable (see the System Console for the
        # object-name diff): the fix is a published cache or a PUBLISH-baked anim.
        if anim_warnings:
            names = ", ".join(i for i, _, _ in anim_warnings)
            parts.append(f"⚠ animation did NOT bind on {names} — no matching "
                         f"object in the linked content (static at origin; needs "
                         f"a cache or PUBLISH-baked anim — see System Console)")
        self.report({"WARNING"} if anim_warnings else
                    {"INFO"} if built or repaired else {"WARNING"},
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
        # FLUMEN_VERBOSE makes resolve-assembly announce each file it downloads or
        # skips on stderr — which flows to Blender's System Console (Window ▸ Toggle
        # System Console on Windows; the launching terminal on Mac). check_output
        # only captures stdout (the JSON), so the progress stays visible.
        env = dict(os.environ, FLUMEN_VERBOSE="1")
        try:
            out = subprocess.check_output(cmd, cwd=td, encoding="utf-8", errors="replace", env=env,
                                          **_no_window()).strip()
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
            bmap = (data.get("bindings") or {}).get(it.element_id) if data else None
            if holder and data and data.get("blend_local") and (amap or bmap):
                try:
                    n = _apply_element_animation(
                        holder, data["blend_local"], amap or {},
                        content=(data.get("contents") or {}).get(
                            it.element_id, ""),
                        bindings=bmap)
                except Exception as exc:  # noqa: BLE001
                    print("[Flumen] load animation failed:", exc)
                    n = 0
                # Constraint targets live in OTHER elements, which are already in
                # the scene here (unlike a build, this loads onto a live shot).
                try:
                    n += _constraints.restore(holder, bmap or {})
                except Exception as exc:  # noqa: BLE001
                    print("[Flumen] could not restore constraints:", exc)
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
            out = subprocess.check_output(cmd, cwd=td, encoding="utf-8", errors="replace", **_no_window()).strip()
            return json.loads(out.splitlines()[-1]) if out else []
        except Exception:  # noqa: BLE001
            return None


def _is_cache_holder(holder):
    """True when the element holder's content is an imported ALEMBIC cache — its
    meshes carry a MeshSequenceCache modifier — rather than a linked rig/model."""
    for o in holder.all_objects:
        if getattr(o, "type", "") != "MESH":
            continue
        for m in getattr(o, "modifiers", []) or []:
            if getattr(m, "type", "") == "MESH_SEQUENCE_CACHE":
                return True
    return False

class FLUMEN_OT_reapply_cache_looks(bpy.types.Operator):
    bl_idname = "flumen.reapply_cache_looks"
    bl_label = "Reapply looks on caches"
    bl_description = ("Lighting: re-fetch each cached character's published look "
                      "(per the shot's element look rules) and re-assign it onto "
                      "every imported Alembic cache in the scene — instances "
                      "included. Only touches caches, never linked rigs/models")

    def execute(self, context):
        task = active_task()
        if not task or task.get("type") != "shot":
            self.report({"ERROR"}, "Open a lighting shot task from the "
                                   "Workspace app.")
            return {"CANCELLED"}
        data = self._resolve(task)
        if not data:
            self.report({"ERROR"}, "Couldn't resolve the shot's looks — check "
                                   "your connection and retry.")
            return {"CANCELLED"}
        looked = missing = skipped = 0
        for el in data.get("elements") or []:
            eid = str(el.get("id", ""))
            holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid)
            if holder is None or not _is_cache_holder(holder):
                continue                       # only imported alembic caches
            ld = el.get("look_data")
            if not (isinstance(ld, dict) and ld.get("blend_local")):
                if el.get("look_error"):
                    missing += 1
                continue
            try:
                n = _apply_element_look(holder, ld)
            except Exception as exc:  # noqa: BLE001
                print("[Flumen] reapply look failed on", eid, exc)
                n = 0
            if n:
                holder["flumen_look"] = (f"{ld.get('name', '')} "
                                         f"v{int(ld.get('version', 0)):03d}")
                looked += 1
            else:
                skipped += 1
        parts = [f"Re-applied looks on {looked} cache(s)"]
        if skipped:
            parts.append(f"{skipped} matched no meshes")
        if missing:
            parts.append(f"{missing} have no published look")
        self.report({"INFO"} if looked else {"WARNING"}, "; ".join(parts) + ".")
        return {"FINISHED"} if looked else {"CANCELLED"}

    def _resolve(self, task):
        # Resolve the assembly to fetch each element's look (blend + manifest);
        # caches already local are skipped by the download's size+mtime check.
        cmd, td = _toolkit_cmd(["resolve-assembly", "--task", task["id"]])
        if cmd is None:
            return None
        env = dict(os.environ, FLUMEN_VERBOSE="1")
        try:
            out = subprocess.check_output(cmd, cwd=td, encoding="utf-8", errors="replace", env=env,
                                          **_no_window()).strip()
            return json.loads(out.splitlines()[-1]) if out else None
        except Exception:  # noqa: BLE001
            return None
