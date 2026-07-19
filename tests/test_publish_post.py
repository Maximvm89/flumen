"""Regression tests for blender_publish_post's texture sidecar — the code that
decides what lands in publish/textures/. Runs the REAL script inside headless
Blender against every texture shape production has produced:

  * plain external images (the common character case) — copied, repathed
  * external images with COLLIDING basenames (Substance default names)
  * packed single images, including colliding ones (the house wallpaper bug)
  * UDIM sets: external, external-colliding, packed, packed-colliding
  * missing sources — left untouched, never half-copied

Skipped when no Blender is installed (e.g. the Windows CI runner); on dev
machines it exercises the exact code artists publish with.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen.launcher import find_blender  # noqa: E402

BLENDER = find_blender()
POST = ROOT / "blender_addon" / "flumen_pipeline" / "blender_publish_post.py"

pytestmark = pytest.mark.skipif(
    BLENDER is None, reason="Blender not installed on this machine")

# Builds the fixture scene, runs INSIDE Blender. Writes real tile/texture
# files, creates image datablocks per spec, saves pub.blend.
_BUILD = r"""
import json, os, sys
import bpy

spec = json.loads(open(sys.argv[-1]).read())
root = spec["root"]
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(root, "pub.blend"))

def write_file(path, color):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = bpy.data.images.new("tmp", 4, 4)
    im.pixels = list(color) * 16
    im.filepath_raw = path
    im.file_format = "PNG"
    im.save()
    bpy.data.images.remove(im)

for i, s in enumerate(spec["images"]):
    color = [(i % 3 == 0) * 1.0, (i % 3 == 1) * 1.0, (i % 3 == 2) * 1.0, 1.0]
    if s.get("tiles"):
        for n in s["tiles"]:
            write_file(os.path.join(root, s["dir"],
                                    s["base"].replace("<UDIM>", str(n))), color)
        img = bpy.data.images.load(os.path.join(
            root, s["dir"], s["base"].replace("<UDIM>", str(s["tiles"][0]))))
        img.source = "TILED"
        for n in s["tiles"][1:]:
            img.tiles.new(tile_number=n)
        img.filepath = "//%s/%s" % (s["dir"], s["base"])
        img.filepath_raw = img.filepath
    else:
        p = os.path.join(root, s["dir"], s["base"])
        write_file(p, color)
        img = bpy.data.images.load(p)
        img.filepath = "//%s/%s" % (s["dir"], s["base"])
        img.filepath_raw = img.filepath
    img.name = s["name"]
    img.use_fake_user = True
    if s.get("packed"):
        img.pack()
    if s.get("delete_source"):
        for n in (s.get("tiles") or [None]):
            p = os.path.join(root, s["dir"],
                             s["base"].replace("<UDIM>", str(n)) if n
                             else s["base"])
            os.remove(p)
bpy.ops.wm.save_mainfile()
"""

# Reports the post-process outcome as JSON on stdout.
_REPORT = r"""
import json, os
import bpy

out = []
for img in bpy.data.images:
    if img.library is not None or img.source not in ("FILE", "TILED"):
        continue
    if not img.filepath:
        continue
    ab = bpy.path.abspath(img.filepath)
    tiles = ([t.number for t in img.tiles] if img.source == "TILED" else [None])
    exists = all(os.path.isfile(ab.replace("<UDIM>", str(n)) if n else ab)
                 for n in tiles)
    out.append({"name": img.name, "filepath": img.filepath,
                "sidecar": img.filepath.startswith("//textures/"),
                "exists": exists,
                "packed": bool(img.packed_file
                               or getattr(img, "packed_files", None)
                               and len(img.packed_files))})
print("REPORT=" + json.dumps(out))
"""


def _run(tmp_path, images):
    root = tmp_path / "asset"
    root.mkdir()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"root": str(root), "images": images}))
    build = tmp_path / "build.py"
    build.write_text(_BUILD)
    subprocess.run([BLENDER, "-b", "--python", str(build), "--", str(spec)],
                   check=True, capture_output=True, text=True, timeout=300)
    subprocess.run([BLENDER, "-b", str(root / "pub.blend"),
                    "--python", str(POST), "--", "--textures-only"],
                   check=True, capture_output=True, text=True, timeout=300)
    report = tmp_path / "report.py"
    report.write_text(_REPORT)
    p = subprocess.run([BLENDER, "-b", str(root / "pub.blend"),
                        "--python", str(report)],
                       check=True, capture_output=True, text=True, timeout=300)
    line = next(l for l in p.stdout.splitlines() if l.startswith("REPORT="))
    data = {r["name"]: r for r in json.loads(line[len("REPORT="):])}
    texdir = root / "textures"
    files = sorted(f.name for f in texdir.iterdir()) if texdir.is_dir() else []
    return data, files


def test_plain_external_images_copy_and_repath(tmp_path):
    # the normal character case — must keep working exactly as before
    data, files = _run(tmp_path, [
        {"name": "body", "dir": "texA", "base": "hero_Body_BaseColor.png"},
        {"name": "eyes", "dir": "texA", "base": "hero_Eyes_BaseColor.png"},
    ])
    assert files == ["hero_Body_BaseColor.png", "hero_Eyes_BaseColor.png"]
    for r in data.values():
        assert r["sidecar"] and r["exists"] and not r["packed"]


def test_external_basename_collision_gets_unique_names(tmp_path):
    data, files = _run(tmp_path, [
        {"name": "wall", "dir": "wall", "base": "DefaultMaterial_Base_color.png"},
        {"name": "ceiling", "dir": "ceil",
         "base": "DefaultMaterial_Base_color.png"},
    ])
    assert len(files) == 2                      # two DISTINCT files
    assert data["wall"]["filepath"] != data["ceiling"]["filepath"]
    for r in data.values():
        assert r["sidecar"] and r["exists"]


def test_packed_collision_the_house_bug(tmp_path):
    # three packed images sharing one export name must yield three files
    data, files = _run(tmp_path, [
        {"name": f"set{i}", "dir": f"d{i}",
         "base": "DefaultMaterial_Base_color.png", "packed": True}
        for i in range(3)
    ])
    assert len(files) == 3
    assert len({r["filepath"] for r in data.values()}) == 3
    for r in data.values():
        assert r["sidecar"] and r["exists"] and not r["packed"]


def test_udim_external_and_collision(tmp_path):
    data, files = _run(tmp_path, [
        {"name": "setA", "dir": "dirA", "base": "set.<UDIM>.png",
         "tiles": [1001, 1002]},
        {"name": "setB", "dir": "dirB", "base": "set.<UDIM>.png",
         "tiles": [1001]},
    ])
    assert len(files) == 3                      # 2 tiles + 1 renamed tile
    assert data["setA"]["filepath"] != data["setB"]["filepath"]
    for r in data.values():
        assert r["sidecar"] and r["exists"] and not r["packed"]


def test_udim_packed_unpacks_and_colliding_stays_packed(tmp_path):
    data, files = _run(tmp_path, [
        {"name": "packedset", "dir": "dirP", "base": "pk.<UDIM>.png",
         "tiles": [1001, 1002], "packed": True},
        {"name": "rival", "dir": "dirR", "base": "pk.<UDIM>.png",
         "tiles": [1001], "packed": True},
    ])
    # first claims the stem and unpacks; the second collides -> stays PACKED
    # (fat but correct), never overwriting the first's tiles
    unpacked = [r for r in data.values() if not r["packed"]]
    still = [r for r in data.values() if r["packed"]]
    assert len(unpacked) == 1 and len(still) == 1
    assert unpacked[0]["sidecar"] and unpacked[0]["exists"]
    assert len(files) == 2                      # only the winner's tiles


def test_missing_sources_left_untouched(tmp_path):
    data, files = _run(tmp_path, [
        {"name": "ghost", "dir": "gone", "base": "ghost.png",
         "delete_source": True},
        {"name": "ghostudim", "dir": "gone2", "base": "gu.<UDIM>.png",
         "tiles": [1001], "delete_source": True},
    ])
    for r in data.values():
        assert not r["sidecar"]                 # paths untouched
    assert files == []                          # nothing half-copied
