"""Light-linking snapshot & restore for lighting scenes.

Updating an alembic cache deletes the element's old objects, and Blender
silently drops deleted objects from every light-linking receiver/blocker
collection — the lighter's linking work evaporates. This module captures the
whole light-linking state (per emitter: receiver + blocker collections, each
member's INCLUDE/EXCLUDE), writes it to a JSON sidecar next to the .blend,
and re-applies it by name afterwards — tolerant of the '.001'/'_001' suffix
drift a re-import causes. Build shot calls snapshot()/restore() around a
cache update automatically; the operators are the manual handles.
"""

import json
import os
import re

import bpy


_SUFFIX_RE = re.compile(r"[._]\d{3,}$")


def _base(name):
    return _SUFFIX_RE.sub("", name)


def sidecar_path():
    """The JSON sidecar next to the saved .blend ('' when the file is
    unsaved). One fixed name per folder: the shot's work versions all share
    the newest snapshot."""
    if not bpy.data.filepath:
        return ""
    return os.path.join(os.path.dirname(bpy.data.filepath),
                        "light_links.json")


def _capture_collection(coll):
    """A light-linking collection's members with their link state."""
    return {
        "name": coll.name,
        "objects": [[ob.name, co.light_linking.link_state]
                    for ob, co in zip(coll.objects, coll.collection_objects)],
        "collections": [[ch.name, cc.light_linking.link_state]
                        for ch, cc in zip(coll.children,
                                          coll.collection_children)],
    }


def snapshot():
    """Every emitter's light-linking state: {emitter: {receiver?, blocker?}}.
    Emitters with no linking at all are skipped."""
    out = {}
    for ob in bpy.data.objects:
        ll = getattr(ob, "light_linking", None)
        if ll is None:
            continue
        rec = {}
        if ll.receiver_collection is not None:
            rec["receiver"] = _capture_collection(ll.receiver_collection)
        if ll.blocker_collection is not None:
            rec["blocker"] = _capture_collection(ll.blocker_collection)
        if rec:
            out[ob.name] = rec
    return out


def _find_object(name, used):
    """An object by exact name, else by suffix-stripped base name ('BODY_004'
    finds 'BODY.005' after a re-import renumbered it) — never one already
    claimed for another member."""
    ob = bpy.data.objects.get(name)
    if ob is not None:
        return ob
    base = _base(name)
    cands = sorted((o for o in bpy.data.objects
                    if _base(o.name) == base and o.name not in used),
                   key=lambda o: o.name)
    return cands[0] if cands else None


def _restore_collection(coll, saved, counts, warns, where):
    used = {o.name for o in coll.objects}
    for name, state in saved.get("objects") or []:
        ob = _find_object(name, used)
        if ob is None:
            warns.append(f"{where}: object '{name}' not in the scene")
            continue
        if ob.name not in used:
            try:
                coll.objects.link(ob)
            except Exception:  # noqa: BLE001
                continue
            used.add(ob.name)
            counts["relinked"] += 1
        for member, co in zip(coll.objects, coll.collection_objects):
            if member == ob and co.light_linking.link_state != state:
                co.light_linking.link_state = state
    have = {c.name for c in coll.children}
    for name, state in saved.get("collections") or []:
        ch = bpy.data.collections.get(name)
        if ch is None:
            warns.append(f"{where}: collection '{name}' not in the scene")
            continue
        if ch.name not in have:
            try:
                coll.children.link(ch)
            except Exception:  # noqa: BLE001
                continue
            have.add(ch.name)
            counts["relinked"] += 1
        for member, cc in zip(coll.children, coll.collection_children):
            if member == ch and cc.light_linking.link_state != state:
                cc.light_linking.link_state = state


def restore(data):
    """Re-apply a snapshot(): missing members are re-linked by name (suffix
    tolerant) with their saved INCLUDE/EXCLUDE; members already in place keep
    their state corrected. Idempotent — safe to run after every cache update.
    Returns (relinked_count, warnings)."""
    counts, warns = {"relinked": 0}, []
    for emitter_name, rec in (data or {}).items():
        emitter = bpy.data.objects.get(emitter_name)
        if emitter is None:
            warns.append(f"emitter '{emitter_name}' not in the scene")
            continue
        ll = emitter.light_linking
        for kind, attr in (("receiver", "receiver_collection"),
                           ("blocker", "blocker_collection")):
            saved = rec.get(kind)
            if not saved:
                continue
            coll = getattr(ll, attr)
            if coll is None:
                # the linking collection itself was lost — recreate it
                coll = (bpy.data.collections.get(saved["name"])
                        or bpy.data.collections.new(saved["name"]))
                setattr(ll, attr, coll)
            _restore_collection(coll, saved, counts, warns,
                                f"{emitter_name}/{kind}")
    return counts["relinked"], warns


class FLUMEN_OT_save_light_links(bpy.types.Operator):
    bl_idname = "flumen.save_light_links"
    bl_label = "Save light links"
    bl_description = ("Write every light's linking (receiver/blocker "
                      "collections + include/exclude) to light_links.json "
                      "next to this .blend — restore it after a cache update "
                      "with 'Load light links'")

    def execute(self, context):
        path = sidecar_path()
        if not path:
            self.report({"ERROR"}, "Save the .blend first.")
            return {"CANCELLED"}
        data = snapshot()
        if not data:
            self.report({"WARNING"}, "No light linking in this scene — "
                                     "nothing saved.")
            return {"CANCELLED"}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "lights": data}, fh, indent=1)
        n = sum(len(r.get(k, {}).get("objects", []))
                + len(r.get(k, {}).get("collections", []))
                for r in data.values() for k in ("receiver", "blocker"))
        self.report({"INFO"}, f"Saved linking for {len(data)} emitter(s) "
                              f"({n} member(s)) -> {os.path.basename(path)}")
        return {"FINISHED"}


class FLUMEN_OT_load_light_links(bpy.types.Operator):
    bl_idname = "flumen.load_light_links"
    bl_label = "Load light links"
    bl_description = ("Re-apply the light linking saved by 'Save light links' "
                      "— re-links by name anything a cache update dropped")

    def execute(self, context):
        path = sidecar_path()
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "No light_links.json next to this .blend — "
                                   "run 'Save light links' first.")
            return {"CANCELLED"}
        try:
            with open(path, encoding="utf-8") as fh:
                data = (json.load(fh) or {}).get("lights") or {}
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Unreadable light_links.json: {exc}")
            return {"CANCELLED"}
        n, warns = restore(data)
        for w in warns:
            print(f"[Flumen] light links: {w}")
        msg = f"Re-linked {n} member(s)."
        if warns:
            msg += f" {len(warns)} not found (see System Console)."
        self.report({"WARNING"} if warns else {"INFO"}, msg)
        return {"FINISHED"}


CLASSES = (FLUMEN_OT_save_light_links, FLUMEN_OT_load_light_links)
