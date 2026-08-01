"""Predict what publishing a work file would do to each element's animation.

Read-only dry run of the shot publish dialog: opens the .blend headless, hashes
every element's animation exactly the way the add-on does, and classifies it
against what is already published.

    .venv/bin/python scripts/publish_preflight.py SEQ010/SH0010 path/to/work.blend

  new       this element has never been published
  changed   genuinely new animation — publishing moves it forward
  unchanged identical to the newest published version — nothing to add
  behind    this scene holds an OLDER published animation that has since been
            superseded — publishing is a straight revert of someone's work
  stale     the content IS new, but the scene was BUILT from an older publish,
            so whatever landed since is missing from it and would be buried

Publishing an old work file wholesale is the classic way to lose animation: it
becomes the newest version for EVERY element it contains, including the ones
the artist never touched. Run this first, then publish only what you mean.

The .blend must sit at its normal depth in the project mirror, or its linked
rigs load empty and every element hashes as if it had no animation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))

# Runs INSIDE Blender: reuse the add-on's own hashing so the numbers are the
# ones the publish dialog would compute — not a re-implementation that drifts.
_HASHER = r'''
import bpy, os, json, sys
sys.path.insert(0, os.environ["ADDON_DIR"])
from flumen_pipeline.build_shot import _element_anim_hashes
h = _element_anim_hashes()
# also report which published version each element was BUILT from: content
# hashes alone can't tell "new work" from "new work on a stale base".
loaded = {}
for c in bpy.data.collections:
    if c.name.startswith("element__"):
        loaded[c.name[len("element__"):]] = str(c.get("flumen_anim", "") or "")
json.dump({"hashes": h, "loaded": loaded}, open(os.environ["PREFLIGHT_OUT"], "w"))
print("[preflight] hashed", len(h), "element(s)")
'''


def _classifier():
    """The add-on's own classify_anim_status — imported, never re-implemented,
    so this dry run can't drift from what the publish dialog actually does.
    The add-on package imports bpy at module level, so stub it out (same shim
    tests/test_addon.py uses); the function itself is pure Python."""
    import types
    if "bpy" not in sys.modules:
        try:
            import bpy  # noqa: F401
        except ImportError:
            bpy = types.ModuleType("bpy")
            bpy.types = types.SimpleNamespace(
                Operator=object, Panel=object, AddonPreferences=object,
                Menu=object, PropertyGroup=object)
            _p = lambda *a, **k: None  # noqa: E731
            bpy.props = types.SimpleNamespace(
                BoolProperty=_p, StringProperty=_p, IntProperty=_p,
                FloatProperty=_p, EnumProperty=_p, CollectionProperty=_p,
                PointerProperty=_p)
            bpy.utils = types.SimpleNamespace(
                user_resource=lambda *a, **k: tempfile.gettempdir())
            bpy.context = types.SimpleNamespace()
            bpy.data = types.SimpleNamespace(scenes=[])
            sys.modules["bpy"] = bpy
    from flumen_pipeline.operators import classify_anim_status
    return classify_anim_status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shot", help="shot entity, e.g. SEQ010/SH0010")
    ap.add_argument("blend", help="the work .blend you are thinking of publishing")
    ap.add_argument("--step", default="animation")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    if not os.path.isfile(args.blend):
        sys.exit(f"error: no such file: {args.blend}")

    from flumen.config import ProjectConfig
    from flumen.sftp import SFTPClient, SFTPCredentials
    from flumen import elements as E
    from flumen.launcher import find_blender
    classify_anim_status = _classifier()

    blender = find_blender(None)
    if not blender:
        sys.exit("error: Blender not found.")

    fd, out = tempfile.mkstemp(suffix=".json"); os.close(fd)
    fd, script = tempfile.mkstemp(suffix=".py"); os.close(fd)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_HASHER)
    env = dict(os.environ, PREFLIGHT_OUT=out,
               ADDON_DIR=os.path.join(ROOT, "blender_addon"))
    print(f"hashing {os.path.basename(args.blend)} …")
    subprocess.run([blender, "-b", args.blend, "--python", script],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    _d = json.load(open(out))
    cur, loaded_of = _d["hashes"], _d.get("loaded", {})
    os.remove(out); os.remove(script)

    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    with SFTPClient(creds) as c:
        anims = E.published_animations(c, cfg.remote_root, args.shot, args.step)
    anims = [a for a in anims if a.get("step") == args.step]   # one sequence
    history: dict[str, list] = {}
    for a in anims:                                            # newest first
        for eid, h in (a.get("hashes") or {}).items():
            history.setdefault(eid, []).append(
                (a.get("version", ""), h, a.get("by", "")))

    newest_of = {}
    for a in anims:
        for eid in (a.get("elements") or {}):
            newest_of.setdefault(eid, a.get("version", ""))
    buckets: dict[str, list] = {}
    for eid in sorted(cur):
        status, ref, by = classify_anim_status(
            cur[eid], history.get(eid) or [],
            loaded_of.get(eid, ""), newest_of.get(eid, ""))
        buckets.setdefault(status, []).append(
            (eid, ref, by, loaded_of.get(eid, "")))

    print(f"\n=== publishing {os.path.basename(args.blend)} into "
          f"{args.shot} / {args.step} would mean ===")
    for status, title in (("behind", "REVERTS — identical to an older publish"),
                          ("stale", "NEW WORK ON A STALE BASE — buries newer"),
                          ("changed", "moves forward (built from the newest)"),
                          ("new", "first publish for this element"),
                          ("unchanged", "already published, nothing to add")):
        rows = buckets.get(status) or []
        if not rows:
            continue
        print(f"\n  {title}  [{len(rows)}]")
        for eid, ref, by, ld in rows:
            if status == "behind":
                print(f"     {eid:<18} would bury {ref}"
                      + (f" by {by}" if by else ""))
            elif status == "stale":
                print(f"     {eid:<18} built from {ld or '?'} — buries {ref}"
                      + (f" by {by}" if by else ""))
            elif status == "unchanged":
                print(f"     {eid:<18} = {ref}")
            else:
                print(f"     {eid}")
    n = len(buckets.get("behind") or []) + len(buckets.get("stale") or [])
    print("\n" + ("-" * 60))
    nb = len(buckets.get("behind") or []); ns = len(buckets.get("stale") or [])
    if n:
        if nb:
            print(f"{nb} element(s) are a STRAIGHT REVERT — the publish dialog "
                  f"leaves these unticked.\n   Leave them so unless you really "
                  f"mean to roll them back.")
        if ns:
            print(f"{ns} element(s) carry NEW work built on an OLD base. These "
                  f"stay TICKED (the work is real),\n   but publishing them "
                  f"buries the newer version. Untick any you did not intend to "
                  f"touch.")
    else:
        print("No rollbacks: every element in this scene is at or ahead of what "
              "is published.")
    return 1 if n else 0


if __name__ == "__main__":
    raise SystemExit(main())
