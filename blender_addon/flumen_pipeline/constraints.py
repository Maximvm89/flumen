"""Capture and restore ANIMATOR-added constraints across a shot rebuild.

A rig's own constraints ship inside the linked library and come back for free.
What does not come back is anything the animator added on top in their local
override: the Child Of on the bat's bone that carries the sheet, the Copy
Transforms that hands a prop from one character to another. Those live only in
the animator's file, so a rebuilt shot drops them — the prop stops following,
and the pose that was animated *against* the constraint reads as broken.

Two things make this more than "copy the constraint stack":

* **Which constraints are the animator's.** On a library override,
  ``Constraint.is_override_data`` is True only for constraints added locally, so
  the rig's own stack is never re-added (and never duplicated).
* **Targets move.** A constraint points at an object datablock, and a rebuild
  renames objects (Blender's ``.001`` collision suffixes) — usually in a
  DIFFERENT element that may not even be built yet. Targets are therefore
  recorded as (element id, stable source name) and resolved in a SECOND PASS,
  after every element exists.

The snapshot rides in the animation manifest's per-object ``bindings`` under a
``constraints`` key, so nothing in the publish/resolve chain needed a new shape;
consumers that predate it ignore the key.
"""

from __future__ import annotations

import json

import bpy

ELEMENT_HOLDER_PREFIX = "element__"

# Read-only trace, inspectable from the Python console after a build:
#   import flumen_pipeline.constraints as C; [print(x) for x in C._LOG]
_LOG = []

# UI/derived state, never restored. Everything else writable on the constraint
# is captured generically, so a constraint type we've never seen still round
# trips (Child Of's inverse_matrix included — without it the prop jumps).
_SKIP = {"rna_type", "name", "type", "active", "is_valid", "is_override_data",
         "error_location", "error_rotation", "is_proxy_local"}


def _element_of(ob):
    """The element id whose holder collection contains `ob` ('' when none).
    Walks up through nested collections — a rig usually sits a level or two
    under the holder."""
    seen, stack = set(), list(getattr(ob, "users_collection", []) or [])
    parents = {}
    for p in bpy.data.collections:
        for ch in p.children:
            parents.setdefault(ch.name, []).append(p)
    while stack:
        c = stack.pop()
        if c.name in seen:
            continue
        seen.add(c.name)
        if c.name.startswith(ELEMENT_HOLDER_PREFIX):
            return c.name[len(ELEMENT_HOLDER_PREFIX):]
        stack.extend(parents.get(c.name, []))
    return ""


def _stable(ob):
    """The override reference name — stable across a rebuild (see
    build_shot._stable_obj_name; duplicated here to keep this module
    importable on its own)."""
    ov = getattr(ob, "override_library", None)
    ref = getattr(ov, "reference", None) if ov else None
    return ref.name if ref is not None else ob.name


def _ref_of(ob):
    """Serialise an object pointer so it can be found again after a rebuild."""
    if ob is None:
        return None
    return {"element": _element_of(ob), "obj": _stable(ob), "name": ob.name}


def resolve_ref(ref):
    """The object a captured reference points at now, or None. STRICTLY scoped
    to the recorded element: several instances of one asset share a stable name
    (every character's armature is 'rig'), so any wider search risks pinning
    the bandage to the wrong character — silently, which is worse than not
    pinning it at all. Element recorded but not built -> None, and the caller
    refuses to create the constraint. Only a ref with NO element (a local
    object like the camera rig) may search the whole scene, and then only on
    an unambiguous match."""
    if not isinstance(ref, dict):
        return None
    want = ref.get("obj") or ""
    eid = ref.get("element") or ""
    if eid:
        holder = bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid)
        if holder is None:
            return None                   # target's element isn't in the scene
        for o in holder.all_objects:
            if _stable(o) == want:
                return o
        for o in holder.all_objects:      # renamed source: exact scene name
            if o.name == (ref.get("name") or ""):
                return o
        return None
    exact = bpy.data.objects.get(ref.get("name") or "")
    if exact is not None:
        return exact
    hits = [o for o in bpy.data.objects if _stable(o) == want]
    return hits[0] if len(hits) == 1 else None


def _seq(val):
    """A float array property as plain JSON: flat for vectors, nested rows for
    matrices (Child Of's inverse_matrix), which is how they're set back."""
    out = []
    for x in val:
        try:
            out.append([float(y) for y in x])
        except TypeError:
            out.append(float(x))
    return out


def _props_of(con):
    """Every writable property of a constraint, as JSON. Pointers become
    references; collection properties (only the Armature constraint's multi
    'targets') are skipped — flagged in the trace rather than half-restored."""
    vals, skipped = {}, []
    for prop in con.bl_rna.properties:
        ident = prop.identifier
        if ident in _SKIP or prop.is_readonly:
            continue
        try:
            val = getattr(con, ident)
        except Exception:  # noqa: BLE001
            continue
        if prop.type == "COLLECTION":
            skipped.append(ident)
            continue
        if prop.type == "POINTER":
            vals[ident] = _ref_of(val) if isinstance(val, bpy.types.Object) \
                else None
        elif getattr(prop, "is_array", False):
            vals[ident] = _seq(val)
        elif prop.type in ("STRING", "ENUM"):
            vals[ident] = str(val)
        elif prop.type == "BOOLEAN":
            vals[ident] = bool(val)
        elif prop.type == "INT":
            vals[ident] = int(val)
        elif prop.type == "FLOAT":
            vals[ident] = round(float(val), 6)
    if skipped:
        vals["_skipped"] = skipped
    return vals


def _is_animator_added(ob, con):
    """True when the animator added this constraint, not the rigger.

    ``Constraint.is_override_data`` is True for a constraint that came from the
    LINKED reference and False for one added locally in the override — the
    opposite of what its name suggests, verified on Blender 5.1 (a rigger's
    Copy Rotation reads True, an animator's Child Of added on top reads False).
    A plain local object (the camera rig) has no library stack to tell apart, so
    all of its constraints count; the name guard on restore keeps that
    idempotent. If the property is missing entirely we refuse to guess on an
    override — capturing the rig's whole stack is the damaging direction."""
    if getattr(ob, "override_library", None) is not None:
        if not hasattr(con, "is_override_data"):
            return False
        return not con.is_override_data
    return True


def snapshot(ob):
    """Animator-added constraints on an object and its pose bones, or {}:
    ``{"object": [con, …], "bones": {bone: [con, …]}}``."""
    out = {}
    obj_cons = [{"name": c.name, "type": c.type, "props": _props_of(c)}
                for c in (getattr(ob, "constraints", None) or [])
                if _is_animator_added(ob, c)]
    if obj_cons:
        out["object"] = obj_cons
    pose = getattr(ob, "pose", None)
    bones = {}
    for pb in (getattr(pose, "bones", None) or []):
        cons = [{"name": c.name, "type": c.type, "props": _props_of(c)}
                for c in pb.constraints if _is_animator_added(ob, c)]
        if cons:
            bones[pb.name] = cons
    if bones:
        out["bones"] = bones
    return out


def digest(snap):
    """A stable string for a snapshot, so the publish dialog's content hash
    notices a constraint being added, retargeted or removed."""
    return json.dumps(snap, sort_keys=True, separators=(",", ":"))


def _apply_one(stack, spec, trace, where=""):
    """Add one captured constraint to a constraint stack. Skips a constraint
    that is already there BY NAME — the rig's own, or a second restore pass —
    so this never duplicates. Returns True when one was created.

    Every recorded object target must resolve BEFORE the constraint is
    created: a Copy Transforms whose target element wasn't built would either
    sit dead (empty target) or — via any name fallback — pin to the WRONG
    same-named object. Neither is acceptable, so the constraint is skipped
    with a loud console line naming the element to build; re-running Load
    animation after building it re-creates the constraint (the by-name guard
    doesn't block it, since it was never made)."""
    name = spec.get("name") or ""
    if name and name in {c.name for c in stack}:
        trace.append((name, "exists"))
        return False
    props = spec.get("props") or {}
    targets = {}
    for ident, val in props.items():
        if isinstance(val, dict) and "obj" in val:      # a serialised pointer
            tgt = resolve_ref(val)
            if tgt is None:
                miss = val.get("element") or val.get("name") or "?"
                trace.append((name, f"target unresolved: {miss}"))
                print(f"[Flumen] CONSTRAINT NOT RESTORED {where}'{name}' — "
                      f"its target (element '{miss}') is not in the scene. "
                      f"Build that element, then re-apply with Load "
                      f"animation.")
                return False
            targets[ident] = tgt
    try:
        con = stack.new(type=spec.get("type", ""))
    except Exception as exc:  # noqa: BLE001 — unknown/unsupported type
        trace.append((name, f"new failed: {exc}"))
        return False
    if name:
        con.name = name
    for ident, val in props.items():
        if ident.startswith("_"):
            continue
        try:
            prop = con.bl_rna.properties[ident]
        except Exception:  # noqa: BLE001 — property gone in this Blender
            continue
        try:
            if prop.type == "POINTER":
                if ident in targets:
                    setattr(con, ident, targets[ident])
            elif isinstance(val, list) and val and isinstance(val[0], list):
                from mathutils import Matrix
                setattr(con, ident, Matrix(val))
            else:
                setattr(con, ident, val)
        except Exception as exc:  # noqa: BLE001
            trace.append((name, f"{ident}: {exc}"))
    trace.append((name, "added"))
    return True


def restore(holder, bindings):
    """Re-add the captured constraints for one element holder. `bindings` is the
    manifest's per-object map ({stable_name: {…, 'constraints': snapshot}}).

    Call this AFTER every element is built: a constraint typically targets
    another element, and a target that doesn't exist yet resolves to None and is
    silently dropped. Returns how many constraints were created."""
    if holder is None or not bindings:
        return 0
    by_stable, by_name = {}, {}
    for o in holder.all_objects:
        by_stable.setdefault(_stable(o), o)
        by_name.setdefault(o.name, o)
    made = 0
    for key, entry in bindings.items():
        snap = (entry or {}).get("constraints") if isinstance(entry, dict) \
            else None
        if not snap:
            continue
        ob = by_stable.get(key) or by_name.get(key)
        trace = []
        if ob is None:
            _LOG.append({"holder": holder.name, "object": key,
                         "note": "object not found"})
            continue
        for spec in snap.get("object") or []:
            made += _apply_one(ob.constraints, spec, trace,
                               where=f"({holder.name}/{ob.name}) ")
        pose = getattr(ob, "pose", None)
        for bone, specs in (snap.get("bones") or {}).items():
            pb = pose.bones.get(bone) if pose else None
            if pb is None:
                trace.append((bone, "bone not found"))
                continue
            for spec in specs:
                made += _apply_one(pb.constraints, spec, trace,
                                   where=f"({holder.name}/{bone}) ")
        _LOG.append({"holder": holder.name, "object": ob.name, "trace": trace})
    return made
