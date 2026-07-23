"""Cache a shot: headlessly build it from the PUBLISHED animation, then
bake each rigged, animated character to its own Alembic and publish the
caches for lighting to load. Drives build_shot.py headlessly (the actual
scene assembly lives there); this module is the cache operation on top."""

import json
import os
import subprocess

import bpy

from .build_shot import (
    ELEMENT_HOLDER_PREFIX, _ELEMENT_LOADERS, _action_fcurves,
    _apply_build_frame_range, _apply_dressing_props, _apply_element_animation,
    _is_environment)
from ._common import (
    _no_window, _publog, _toolkit_cmd, active_task)
from .looks import _apply_element_look
from .startup import scaffold_empty_scene


def _resolve_assembly(task, list_only=False, only=None, picks=None):
    """Module-level assembly resolve (build_shot has a method version). Returns
    the parsed JSON or None."""
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
        out = subprocess.check_output(cmd, cwd=td, text=True,
                                      **_no_window()).strip()
        return json.loads(out.splitlines()[-1]) if out else {}
    except Exception as exc:  # noqa: BLE001
        _publog(f"resolve-assembly failed: {exc}")
        return None

def _headless_build_shot(context, task):
    """Build every resolvable element into the scene (no dialog): link/import,
    stamp, apply the element's look and published animation, place the camera,
    set the frame range. Additive — an element already in the scene is left
    alone. For the headless 'Cache shot' path. Returns the count built."""
    data = _resolve_assembly(task)
    if not data:
        return 0
    elements = data.get("elements") or []
    anim_elements = ((data.get("anim") or {}).get("elements")) or {}
    built = 0
    n = len(elements)
    print(f"[Flumen] cache: building {n} element(s) from the published shot…",
          flush=True)
    for i, el in enumerate(elements, 1):
        eid = str(el.get("id", ""))
        if bpy.data.collections.get(ELEMENT_HOLDER_PREFIX + eid) is not None:
            continue                              # already present — additive
        loader = _ELEMENT_LOADERS.get(el.get("kind"))
        if loader is None:
            continue
        print(f"[Flumen] cache:   [{i}/{n}] building {eid} "
              f"({el.get('kind', 'asset')})…", flush=True)
        try:
            holder, err = loader(context, el)
        except Exception as exc:  # noqa: BLE001
            holder, err = None, str(exc)
        if not holder:
            _publog(f"headless build: {eid} failed: {err}")
            continue
        built += 1
        holder["flumen_step"] = ("camera" if el.get("kind") == "camera"
                                 else el.get("source_step", ""))
        holder["flumen_asset"] = el.get("asset", "")
        dressing = el.get("dressing")
        if isinstance(dressing, dict) and dressing.get("props"):
            _apply_dressing_props(context, holder, el)
        ld = el.get("look_data")
        if isinstance(ld, dict) and ld.get("blend_local"):
            try:
                _apply_element_look(holder, ld)
                holder["flumen_look"] = (f"{ld.get('name', '')} "
                                         f"v{int(ld.get('version', 0)):03d}")
            except Exception as exc:  # noqa: BLE001
                _publog(f"headless build: look on {eid} failed: {exc}")
        ael = anim_elements.get(eid)
        if (ael and ael.get("blend_local") and ael.get("objects")
                and not _is_environment(el)):
            want = len(ael.get("objects") or {})
            try:
                got = _apply_element_animation(holder, ael["blend_local"],
                                               ael["objects"],
                                               content=ael.get("content", ""))
                holder["flumen_anim"] = ael.get("version", "")
                # Diagnostic: how many of the manifest's animated objects actually
                # got their action. A shortfall here is why a character can bake to
                # the cache with a wrong pose / dropped limb.
                print(f"[Flumen] cache:   anim on {eid}: {got}/{want} object(s) "
                      f"animated (v{ael.get('version', '?')})", flush=True)
                if got < want:
                    _publog(f"cache: {eid} animation incomplete — {got}/{want} "
                            f"objects matched the manifest by name")
            except Exception as exc:  # noqa: BLE001
                _publog(f"headless build: anim on {eid} failed: {exc}")
    try:
        bpy.ops.file.make_paths_relative()
    except Exception:  # noqa: BLE001
        pass
    # Set the shot's frame range from the resolve (the interactive dialog seeds
    # _BUILD_FRAME_RANGE in invoke(); headless has no dialog, so set it here).
    # Without this the scene stays at 1-250 and a cache bakes frames BEFORE the
    # animation starts (keyed at 1001+) — static geometry.
    fs, fe = data.get("frame_start"), data.get("frame_end")
    if fs and fe:
        try:
            scene = context.scene
            scene.frame_start, scene.frame_end = int(fs), int(fe)
            scene.frame_set(int(fs))
        except Exception:  # noqa: BLE001
            pass
    else:
        _apply_build_frame_range(context)
    return built

def _cache_shot_elements(context, task, only=None):
    """Bake rigged+animated elements to alembic and publish them. `only` limits
    to those element ids (None = all candidates). Records the anim version each
    was baked from. Returns (published_pairs, failed)."""
    import tempfile
    scene = context.scene
    fs, fe = int(scene.frame_start), int(scene.frame_end)
    tmp = tempfile.mkdtemp(prefix="flumen_cache_")
    pairs, failed, anim_of = [], [], {}
    prev_active = context.view_layer.objects.active
    todo = [(eid, h) for eid, h in _cache_candidates()
            if only is None or eid in only]
    print(f"[Flumen] cache: baking {len(todo)} element(s) to Alembic over "
          f"frames {fs}-{fe} — this evaluates every frame, it can take a bit.",
          flush=True)
    for i, (eid, holder) in enumerate(todo, 1):
        anim_of[eid] = str(holder.get("flumen_anim", "") or "")
        meshes = [o for o in holder.all_objects
                  if getattr(o, "type", "") == "MESH"]
        if not meshes:
            failed.append((eid, "no meshes"))
            continue
        print(f"[Flumen] cache:   [{i}/{len(todo)}] baking {eid} "
              f"({len(meshes)} mesh(es))…", flush=True)
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:  # noqa: BLE001
            pass
        bpy.ops.object.select_all(action="DESELECT")
        for o in meshes:
            try:
                o.select_set(True)
            except Exception:  # noqa: BLE001
                pass
        context.view_layer.objects.active = meshes[0]
        path = os.path.join(tmp, f"{eid}.abc")
        try:
            bpy.ops.wm.alembic_export(
                filepath=path, selected=True, start=fs, end=fe,
                flatten=True, uvs=True, packuv=True, normals=True,
                face_sets=True, evaluation_mode="RENDER")
        except Exception as exc:  # noqa: BLE001
            failed.append((eid, str(exc)))
            continue
        if os.path.isfile(path):
            pairs.append((eid, path))
        else:
            failed.append((eid, "export produced no file"))
    context.view_layer.objects.active = prev_active
    if not pairs:
        return [], failed
    total_mb = sum(os.path.getsize(p) for _e, p in pairs) / 1e6
    no_upload = os.environ.get("FLUMEN_CACHE_NO_UPLOAD") == "1"
    print(f"[Flumen] cache: {'writing (LOCAL, no upload)' if no_upload else 'uploading'} "
          f"{len(pairs)} cache(s) ({total_mb:.0f} MB)…", flush=True)
    args = ["publish-cache", "--task", task["id"]]
    if no_upload:
        args.append("--no-upload")
    for eid, path in pairs:
        args += ["--cache", f"{eid}={path}"]
        if anim_of.get(eid):
            args += ["--anim", f"{eid}={anim_of[eid]}"]
    cmd, td = _toolkit_cmd(args)
    if cmd is None:
        return [], failed + [("*", "toolkit not available to publish")]
    _publog(f"cache-shot: {' '.join(str(c) for c in cmd)}", echo=False)
    p = subprocess.run(cmd, cwd=td, text=True, capture_output=True,
                       **_no_window())
    for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
        _publog("  " + line, echo=False)
    if p.returncode != 0:
        return [], failed + [("*", "publish-cache failed")]
    return [(eid, "published") for eid, _ in pairs], failed

def headless_build_and_cache():
    """Entry point for the Workspace app's 'Cache shot' right-click: build the
    shot (if needed) and cache it, headless. Prints a FLUMEN result line and
    returns an exit code."""
    ctx = bpy.context
    task = active_task()
    if not task or task.get("type") != "shot":
        print("[Flumen] cache: no active shot task.")
        return 1
    # Build from PUBLISHED data on a clean scene (no default cube/camera/light,
    # no animator work file) — caching is always of reviewed, published anim.
    try:
        scaffold_empty_scene()
    except Exception:  # noqa: BLE001
        pass
    n = _headless_build_shot(ctx, task)
    _publog(f"cache: built {n} element(s) before caching", echo=True)
    # Restrict to the elements the artist ticked in the Workspace dialog.
    only_env = os.environ.get("FLUMEN_CACHE_ONLY", "")
    only = set(x for x in only_env.split(",") if x) if only_env else None
    pairs, failed = _cache_shot_elements(ctx, task, only=only)
    if not pairs:
        print("[Flumen] cache: nothing cached — "
              + "; ".join(f"{e}: {m}" for e, m in failed))
        return 1
    print(f"[Flumen] cache: published {len(pairs)} element(s): "
          + ", ".join(e for e, _ in pairs))
    return 0

def _cache_candidates():
    """Element holders to alembic-cache: rigged AND animated. An armature with
    an action (its animation) makes it a deforming character; environments
    (backdrops) and camera holders are skipped, and a static model with no
    armature has nothing to bake. Returns [(element_id, holder)]."""
    out = []
    for coll in bpy.data.collections:
        if not coll.name.startswith(ELEMENT_HOLDER_PREFIX):
            continue
        if str(coll.get("flumen_asset", "")).startswith("environments/"):
            continue
        if str(coll.get("flumen_step", "")) == "camera":
            continue                              # the camera rig is not cached
        arms = [o for o in coll.all_objects
                if getattr(o, "type", "") == "ARMATURE"]
        animated = any(_action_fcurves(a) for a in arms)
        if arms and animated:
            out.append((coll.name[len(ELEMENT_HOLDER_PREFIX):], coll))
    return out

class FLUMEN_OT_cache_shot(bpy.types.Operator):
    bl_idname = "flumen.cache_shot"
    bl_label = "Cache shot (Alembic)"
    bl_description = ("Bake each rigged, animated character in this shot to its "
                      "own Alembic cache over the shot frame range and publish "
                      "them — the inputs a Lighting shot build imports. Camera, "
                      "environment and un-animated models are skipped")

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "shot":
            self.report({"ERROR"}, "Open a shot task from the Workspace app.")
            return {"CANCELLED"}
        self._cands = _cache_candidates()
        if not self._cands:
            self.report({"WARNING"}, "No rigged, animated elements to cache — "
                                     "Build shot and load animation first.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=420, title="Cache shot", confirm_text="Cache")

    def draw(self, context):
        col = self.layout.column()
        fs = int(context.scene.frame_start)
        fe = int(context.scene.frame_end)
        col.label(text=f"Bake to Alembic over frames {fs}–{fe}:")
        box = col.box()
        for eid, _ in self._cands:
            box.label(text=eid, icon="OUTLINER_OB_ARMATURE")
        col.label(text="Published per element; Lighting Build shot imports "
                       "the latest.", icon="INFO")

    def execute(self, context):
        task = active_task()
        if not task:
            return {"CANCELLED"}
        try:
            pairs, failed = _cache_shot_elements(context, task)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Cache failed: {exc}")
            return {"CANCELLED"}
        if not pairs:
            self.report({"ERROR"}, "Nothing cached — "
                        + "; ".join(f"{e}: {m}" for e, m in failed))
            return {"CANCELLED"}
        note = f"; skipped {len(failed)}" if failed else ""
        self.report({"INFO"}, f"Cached + published {len(pairs)} element(s){note}.")
        return {"FINISHED"}
