"""Diff the ANIMATION of two .blend files, per object, per frame.

Answers "the publish doesn't match the animator's scene" objectively instead of
by eyeballing a playblast: it samples what actually reaches the screen — each
mesh's DEFORMED geometry (armature + modifiers evaluated) and its effective
visibility — then reports which objects diverge, when, and by how much.

    .venv/bin/python scripts/anim_diff.py GROUND_TRUTH.blend REBUILT.blend
    .venv/bin/python scripts/anim_diff.py a.blend b.blend --start 1001 --end 1387 --step 5
    .venv/bin/python scripts/anim_diff.py a.blend b.blend --match LENZUOLO

IMPORTANT: both files must sit at their normal depth inside the project mirror
(…/04_sequences/<shot>/<step>/<dir>/x.blend). A .blend copied to /tmp cannot
resolve its '//../../..' library paths, every linked rig loads empty, and the
diff then compares two piles of nothing and reports success.

Visibility is sampled BEFORE muting: hide_viewport removes an object from the
dependency graph, so geometry can only be evaluated with the hide channels
muted — the sampler does both passes, in that order.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Runs INSIDE Blender: writes {key: {"vis": [...], "geo": [...]}} to $ANIMDIFF_OUT.
_SAMPLER = r'''
import bpy, os, json
out = os.environ["ANIMDIFF_OUT"]
start = int(os.environ["ANIMDIFF_START"]); end = int(os.environ["ANIMDIFF_END"])
step = int(os.environ["ANIMDIFF_STEP"]); match = os.environ.get("ANIMDIFF_MATCH", "")
sc = bpy.context.scene; vl = bpy.context.view_layer

def holder_of(o):
    """The element__* collection an object belongs to — the stable identity
    across files (object NAMES pick up .001 suffixes on a rebuild)."""
    for c in o.users_collection:
        seen, stack = set(), [c]
        while stack:
            cur = stack.pop()
            if cur.name in seen:
                continue
            seen.add(cur.name)
            if cur.name.startswith("element__"):
                return cur.name
            for p in bpy.data.collections:
                if cur.name in [x.name for x in p.children]:
                    stack.append(p)
    return (o.users_collection[0].name if o.users_collection else "?")

targets = [o for o in sc.objects if o.type == "MESH"
           and (not match or match.lower() in o.name.lower())]
res = {}
# --- pass 1: effective visibility (must run BEFORE muting hide channels) ---
for o in targets:
    rows = []
    for f in range(start, end + 1, step):
        sc.frame_set(f)
        try: vis = bool(o.visible_get(view_layer=vl))
        except Exception: vis = not o.hide_viewport
        rows.append(1 if vis else 0)
    res[f"{holder_of(o)}/{o.name}"] = {"vis": rows}

# --- pass 2: deformed geometry (needs the object IN the depsgraph) ---
def _unhide(o):
    ad = o.animation_data
    for fc in list(getattr(ad, "drivers", []) or []):
        if "hide" in fc.data_path: fc.mute = True
    act = getattr(ad, "action", None)
    if act:
        for lay in getattr(act, "layers", []):
            for st in getattr(lay, "strips", []):
                for sl in getattr(act, "slots", []):
                    cb = st.channelbag(sl)
                    for fc in (cb.fcurves if cb else []):
                        if "hide" in fc.data_path: fc.mute = True
    try: o.hide_viewport = False; o.hide_render = False
    except Exception: pass

for o in targets:
    _unhide(o)
for o in targets:
    key = f"{holder_of(o)}/{o.name}"
    rows = []
    for f in range(start, end + 1, step):
        sc.frame_set(f)
        ev = o.evaluated_get(bpy.context.evaluated_depsgraph_get())
        try: me = ev.to_mesh()
        except Exception: rows.append(None); continue
        n = len(me.vertices)
        if not n:
            rows.append(None); ev.to_mesh_clear(); continue
        mw = ev.matrix_world; cx = cy = cz = 0.0
        for v in me.vertices:
            wv = mw @ v.co; cx += wv.x; cy += wv.y; cz += wv.z
        rows.append([round(cx / n, 5), round(cy / n, 5), round(cz / n, 5), n])
        ev.to_mesh_clear()
    res.setdefault(key, {})["geo"] = rows
json.dump({"start": start, "end": end, "step": step, "objects": res},
          open(out, "w"))
print(f"[anim-diff] sampled {len(res)} mesh(es) over {start}-{end} step {step}")
'''


def _blender() -> str:
    from flumen.launcher import find_blender
    b = find_blender(None)
    if not b:
        sys.exit("error: Blender not found (set FLUMEN_BLENDER or install it).")
    return b


def sample(blend: str, start: int, end: int, step: int, match: str) -> dict:
    if not os.path.isfile(blend):
        sys.exit(f"error: no such file: {blend}")
    fd, out = tempfile.mkstemp(suffix=".json"); os.close(fd)
    fd, script = tempfile.mkstemp(suffix=".py"); os.close(fd)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_SAMPLER)
    env = dict(os.environ, ANIMDIFF_OUT=out, ANIMDIFF_START=str(start),
               ANIMDIFF_END=str(end), ANIMDIFF_STEP=str(step),
               ANIMDIFF_MATCH=match)
    print(f"sampling {os.path.basename(blend)} …")
    subprocess.run([_blender(), "-b", blend, "--python", script],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    data = json.load(open(out))
    os.remove(out); os.remove(script)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ground_truth", help="the animator's work .blend")
    ap.add_argument("rebuilt", help="the .blend rebuilt from the publish")
    ap.add_argument("--start", type=int, default=0, help="first frame (default: scene range)")
    ap.add_argument("--end", type=int, default=0)
    ap.add_argument("--step", type=int, default=5, help="sample every Nth frame (default 5)")
    ap.add_argument("--match", default="", help="only objects whose name contains this")
    ap.add_argument("--tol", type=float, default=1e-4,
                    help="metres of centroid drift to count as a difference")
    args = ap.parse_args()

    start = args.start or 1001
    end = args.end or 1387
    a = sample(args.ground_truth, start, end, args.step, args.match)
    b = sample(args.rebuilt, start, end, args.step, args.match)
    ao, bo = a["objects"], b["objects"]

    only_a = sorted(set(ao) - set(bo))
    only_b = sorted(set(bo) - set(ao))
    frames = list(range(start, end + 1, args.step))
    findings = []
    for key in sorted(set(ao) & set(bo)):
        va, vb = ao[key].get("vis") or [], bo[key].get("vis") or []
        vis_bad = [frames[i] for i in range(min(len(va), len(vb))) if va[i] != vb[i]]
        ga, gb = ao[key].get("geo") or [], bo[key].get("geo") or []
        worst, worst_f, nverts = 0.0, None, ""
        for i in range(min(len(ga), len(gb))):
            x, y = ga[i], gb[i]
            if x is None or y is None:
                continue
            if x[3] != y[3]:
                nverts = f"  vertex count {x[3]} vs {y[3]}"
            d = math.dist(x[:3], y[:3])
            if d > worst:
                worst, worst_f = d, frames[i]
        if vis_bad or worst > args.tol or nverts:
            findings.append((worst, key, vis_bad, worst_f, nverts))

    print("\n================ ANIM DIFF ================")
    print(f"ground truth : {args.ground_truth}")
    print(f"rebuilt      : {args.rebuilt}")
    print(f"frames       : {start}-{end} step {args.step}  ({len(frames)} samples)")
    if only_a:
        print(f"\nMISSING from the rebuild ({len(only_a)}):")
        for k in only_a[:40]:
            print(f"   {k}")
    if only_b:
        print(f"\nEXTRA in the rebuild ({len(only_b)}):")
        for k in only_b[:40]:
            print(f"   {k}")
    if not findings:
        print("\nNo differences: visibility and deformed geometry match on every "
              "sampled frame.")
    else:
        print(f"\n{len(findings)} object(s) differ, worst first:")
        for worst, key, vis_bad, worst_f, nverts in sorted(findings, reverse=True):
            print(f"\n  {key}{nverts}")
            if worst > args.tol:
                print(f"     deformation: up to {worst:.4f} m, worst at frame {worst_f}")
            if vis_bad:
                rng = f"{vis_bad[0]}-{vis_bad[-1]}" if len(vis_bad) > 1 else str(vis_bad[0])
                print(f"     visibility : differs on {len(vis_bad)} sampled frame(s) [{rng}]")
    print("===========================================")
    return 1 if (findings or only_a) else 0


if __name__ == "__main__":
    raise SystemExit(main())
