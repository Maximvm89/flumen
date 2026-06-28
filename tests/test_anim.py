"""Tests for the addon's animation publish helpers (pure logic, no bpy)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blender_addon"))

from legami_pipeline import anim


def test_anim_blend_and_manifest_paths():
    pub = "/x/publish/SH0010_layout_v003.blend"
    ab = anim.anim_blend_path(pub)
    assert ab == "/x/publish/SH0010_layout_v003_anim.blend"
    assert anim.anim_manifest_path(ab) == \
        "/x/publish/SH0010_layout_v003_anim.manifest.json"


def test_is_anim_blend_distinguishes_from_main_shot():
    assert anim.is_anim_blend("SH0010_layout_v003_anim.blend")
    assert not anim.is_anim_blend("SH0010_layout_v003.blend")


def test_build_anim_manifest_groups_per_element_and_drops_empty():
    m = anim.build_anim_manifest(3, {
        "frankenstein": {"frank_rig": "frank_rigAction"},
        "camera": {"SEQ010_SH0010": "CamAction"},
        "box": {},                       # no animation -> dropped
    })
    assert m["version"] == 3
    assert set(m["elements"]) == {"frankenstein", "camera"}
    assert m["elements"]["camera"] == {"SEQ010_SH0010": "CamAction"}
