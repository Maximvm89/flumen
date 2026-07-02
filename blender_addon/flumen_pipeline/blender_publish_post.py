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

import sys

import bpy


def _args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    out = {"collection": "", "apply": False}
    i = 0
    while i < len(argv):
        if argv[i] == "--collection" and i + 1 < len(argv):
            out["collection"] = argv[i + 1]
            i += 2
        elif argv[i] == "--apply-modifiers":
            out["apply"] = True
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


def main():
    a = _args()
    coll = bpy.data.collections.get(a["collection"])
    if coll is None:
        print(f"[Flumen] post: collection '{a['collection']}' not found "
              f"in the publish — aborting.")
        sys.exit(1)

    _progress(10, "cleaning scene")
    keep_objs = set(coll.all_objects)
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

    baked = 0
    if a["apply"]:
        _progress(35, "baking modifiers")
        baked, skipped = _bake_modifiers(coll)
        if skipped:
            print(f"[Flumen] post: {skipped} object(s) kept live modifiers "
                  f"(armature-deformed or failed).")

    _progress(70, "purging orphan data")
    try:
        for _ in range(3):     # recursive chains need a few passes
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True,
                                   do_recursive=True)
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] post: orphan purge failed:", exc)

    _progress(85, "saving")
    bpy.ops.wm.save_mainfile(compress=True)
    _progress(100, "done")
    print(f"[Flumen] post: cleaned {removed} clutter object(s)"
          + (f", baked modifiers on {baked} mesh(es)" if a["apply"] else "")
          + ".")


main()
