"""Tests for animpipe.lookdev — HDRI listing + resolution (pure, no bpy)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import lookdev


def _make_hdris(tmp_path, *names):
    d = tmp_path / "05_library" / "hdri"
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_bytes(b"x")
    return str(tmp_path)


def test_list_hdris_finds_and_sorts(tmp_path):
    root = _make_hdris(tmp_path, "studio_soft.exr", "courtyard.hdr", "notes.txt",
                       "neutral.EXR")
    assert lookdev.list_hdris(root) == ["courtyard.hdr", "neutral.EXR",
                                        "studio_soft.exr"]  # .txt excluded, sorted


def test_list_hdris_missing_dir(tmp_path):
    assert lookdev.list_hdris(str(tmp_path)) == []


def test_resolve_chosen_over_default(tmp_path):
    root = _make_hdris(tmp_path, "studio.exr", "outdoor.exr")
    ps = {"turntable": {"lookdev_hdri": "studio.exr"}}
    chosen = lookdev.resolve_hdri(ps, "outdoor.exr", root)
    assert chosen.endswith("05_library/hdri/outdoor.exr")


def test_resolve_falls_back_to_project_default(tmp_path):
    root = _make_hdris(tmp_path, "studio.exr")
    ps = {"turntable": {"lookdev_hdri": "studio.exr"}}
    assert lookdev.resolve_hdri(ps, None, root).endswith("studio.exr")
    assert lookdev.resolve_hdri(ps, "", root).endswith("studio.exr")


def test_resolve_none_when_missing(tmp_path):
    root = _make_hdris(tmp_path, "studio.exr")
    # default points at a file that isn't there, no override -> None (neutral world)
    ps = {"turntable": {"lookdev_hdri": "ghost.exr"}}
    assert lookdev.resolve_hdri(ps, None, root) is None
    assert lookdev.resolve_hdri({}, None, root) is None
