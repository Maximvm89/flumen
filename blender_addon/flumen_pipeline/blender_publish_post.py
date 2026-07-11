"""Post-process a just-saved model publish .blend (headless).

    blender -b <publish.blend> --python blender_publish_post.py -- \
        --collection <name> [--apply-modifiers]

The interactive publish saves the whole work scene as a copy with the PUBLISH
subtree wrapped in a named collection. This pass makes the FILE itself clean:

  1. delete every object outside the wrapped collection (helper cubes, ref
     cameras, lights — the scene clutter that used to travel inside the file),
  2. optionally bake the modifier stack into the meshes (static geometry only —
     armature-deformed meshes are skipped, deform must stay live),
  3. purge orphaned data (meshes/materials/images of the deleted clutter),
  4. save over the same file (compressed).

Emits FLUMEN_PROGRESS lines so the add-on's publish bar tracks this phase.
Exits non-zero on a hard failure so the caller can abort the upload.
"""

import os
import sys

import bpy


def _args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    out = {"collection": "", "apply": False, "textures_only": False}
    i = 0
    while i < len(argv):
        if argv[i] == "--collection" and i + 1 < len(argv):
            out["collection"] = argv[i + 1]
            i += 2
        elif argv[i] == "--apply-modifiers":
            out["apply"] = True
            i += 1
        elif argv[i] == "--textures-only":
            out["textures_only"] = True
            i += 1
        else:
            i += 1
    return out


def _progress(pct, msg):
    # "FLUMEN_PROGRESS <pct> <eta> <msg>" — eta unknown here, left blank.
    print(f"FLUMEN_PROGRESS {pct}  {msg}", flush=True)


def _keep_collections(root):
    keep = set()
    stack = [root]
    while stack:
        c = stack.pop()
        keep.add(c)
        stack.extend(c.children)
    return keep


def _bone_widgets(objs):
    """Objects used as bone custom shapes by any armature in `objs` — e.g.
    Rigify's WGTS_* control shapes. They live OUTSIDE the publish root by
    design (Rigify keeps them in their own collection), but deleting them
    strips every control of its shape and leaves the rig unusable."""
    out = set()
    for o in objs:
        if getattr(o, "type", "") != "ARMATURE" or not getattr(o, "pose", None):
            continue
        for pb in o.pose.bones:
            cs = getattr(pb, "custom_shape", None)
            if cs is not None:
                out.add(cs)
    return out


def _bake_modifiers(coll):
    """Bake each mesh's modifier stack into its data. Armature-deformed meshes
    are skipped (a rigged publish must keep deform live). Objects sharing mesh
    data get their own copy when baked — unavoidable to bake per-instance."""
    baked = skipped = 0
    targets = [o for o in coll.all_objects
               if o.type == "MESH" and o.modifiers]
    for o in targets:
        if any(m.type == "ARMATURE" for m in o.modifiers):
            skipped += 1
            continue
        try:
            deps = bpy.context.evaluated_depsgraph_get()
            ev = o.evaluated_get(deps)
            me = bpy.data.meshes.new_from_object(
                ev, preserve_all_data_layers=True, depsgraph=deps)
            old = o.data
            me.name = old.name
            o.data = me
            o.modifiers.clear()
            if old.users == 0:
                bpy.data.meshes.remove(old)
            baked += 1
        except Exception as exc:  # noqa: BLE001 — keep going, report at the end
            print(f"[Flumen] post: could not bake modifiers on {o.name}: {exc}")
            skipped += 1
    return baked, skipped


def _sidecar_textures():
    """Normalize the publish's textures into a sidecar folder beside the file:
    packed images are unpacked to //textures/, external images are copied there
    and their paths remapped. The .blend stays lean (no 456 MB packed monsters)
    and the toolkit uploads the folder once, skipping unchanged files on later
    versions. Runs after the purge so only images actually used survive."""
    import shutil
    base = os.path.dirname(bpy.data.filepath)
    tex_dir = os.path.join(base, "textures")
    taken = {}          # sidecar filename -> source path (collision guard)
    unpacked = copied = missing = 0
    for img in bpy.data.images:
        if img.library is not None:
            continue    # linked from another publish — its sidecar, not ours
        if img.source == "TILED":
            print(f"[Flumen] post: UDIM image '{img.name}' left as-is "
                  f"(tiled textures aren't sidecar'd yet).")
            continue
        if img.source not in ("FILE", "SEQUENCE"):
            continue    # generated / render results carry no file
        if img.packed_file:
            try:
                os.makedirs(tex_dir, exist_ok=True)
                img.unpack(method="WRITE_LOCAL")   # writes //textures/<name>
                unpacked += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[Flumen] post: could not unpack '{img.name}': {exc}")
            continue
        src = bpy.path.abspath(img.filepath)
        if not os.path.isfile(src):
            missing += 1
            print(f"[Flumen] post: texture missing on disk (left as-is): "
                  f"{img.name} -> {img.filepath}")
            continue
        name = os.path.basename(src)
        if taken.get(name, src) != src:            # same name, different file
            name = f"{img.name}_{name}"
        dst = os.path.join(tex_dir, name)
        if os.path.abspath(dst) != os.path.abspath(src):
            os.makedirs(tex_dir, exist_ok=True)
            shutil.copy2(src, dst)
        taken[name] = src
        img.filepath = "//textures/" + name
        copied += 1
    if unpacked or copied or missing:
        print(f"[Flumen] post: sidecar textures: {copied} linked, "
              f"{unpacked} unpacked"
              + (f", {missing} missing" if missing else "") + ".")


def main():
    a = _args()
    if a["textures_only"]:
        # Dressing publishes: no scene stripping (the scene IS the product) —
        # just normalize local-extras shading into the sidecar folder.
        _progress(40, "textures -> sidecar")
        _sidecar_textures()
        _progress(85, "saving")
        bpy.ops.wm.save_mainfile(compress=True)
        _progress(100, "done")
        print("[Flumen] post: textures-only pass complete.")
        return

    coll = bpy.data.collections.get(a["collection"])
    if coll is None:
        print(f"[Flumen] post: collection '{a['collection']}' not found "
              f"in the publish — aborting.")
        sys.exit(1)
    if not coll.all_objects:
        # Never ship an empty publish. Classic cause: another collection owned
        # the asset's name, so the publish wrap got a '.001' suffix.
        others = [c.name for c in bpy.data.collections
                  if c is not coll and c.all_objects]
        print(f"[Flumen] post: collection '{a['collection']}' is EMPTY — "
              f"aborting instead of publishing nothing."
              + (f" Non-empty collections here: {', '.join(others[:5])}"
                 if others else ""))
        sys.exit(1)

    _progress(10, "cleaning scene")
    keep_objs = set(coll.all_objects)
    # A rig's bone widgets are part of the rig even though they sit outside
    # the PUBLISH root. Keep the datablocks but unlink them from every
    # collection: invisible in the publish, still saved (the custom_shape
    # reference keeps them alive through the orphan purge), and a downstream
    # LINK of the collection pulls them in as indirect dependencies.
    widgets = _bone_widgets(keep_objs)
    keep_objs |= widgets
    removed = 0
    for o in list(bpy.data.objects):
        if o not in keep_objs:
            bpy.data.objects.remove(o, do_unlink=True)
            removed += 1
    keep_colls = _keep_collections(coll)
    for c in list(bpy.data.collections):
        if c not in keep_colls:
            bpy.data.collections.remove(c)
    scene = bpy.context.scene
    if coll.name not in scene.collection.children:
        scene.collection.children.link(coll)
    for w in widgets:
        for c in list(w.users_collection):
            try:
                c.objects.unlink(w)
            except Exception:  # noqa: BLE001
                pass
    if widgets:
        print(f"[Flumen] post: kept {len(widgets)} bone-widget object(s) "
              f"(rig control shapes).")
    # Keep script texts (Rigify's rig_ui.py — the Rig Main Properties panel)
    # from the orphan purge when the publish carries a rig.
    if any(getattr(o, "type", "") == "ARMATURE" for o in coll.all_objects):
        for t in bpy.data.texts:
            t.use_fake_user = True

    baked = 0
    if a["apply"]:
        _progress(35, "baking modifiers")
        baked, skipped = _bake_modifiers(coll)
        if skipped:
            print(f"[Flumen] post: {skipped} object(s) kept live modifiers "
                  f"(armature-deformed or failed).")

    _progress(60, "purging orphan data")
    try:
        for _ in range(3):     # recursive chains need a few passes
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True,
                                   do_recursive=True)
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] post: orphan purge failed:", exc)

    _progress(75, "textures -> sidecar")
    _sidecar_textures()

    _progress(85, "saving")
    bpy.ops.wm.save_mainfile(compress=True)
    _progress(100, "done")
    print(f"[Flumen] post: cleaned {removed} clutter object(s)"
          + (f", baked modifiers on {baked} mesh(es)" if a["apply"] else "")
          + ".")


main()
