"""Tests for flumen.playblast (pure helpers + dry-run; no real Blender/FTP)."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import playblast, tasks


def test_playblast_settings_defaults_and_override():
    s = playblast.playblast_settings({})
    assert s["engine"] == "BLENDER_EEVEE_NEXT" and s["resolution_x"] == 1280
    assert s["color"] == "TEXTURE"
    s2 = playblast.playblast_settings({"playblast": {"engine": "BLENDER_WORKBENCH",
                                                     "fps": 30, "color": "MATERIAL"}})
    assert s2["engine"] == "BLENDER_WORKBENCH" and s2["fps"] == 30
    assert s2["color"] == "MATERIAL"
    assert s2["resolution_y"] == 720          # untouched default preserved


def test_playblast_rel():
    shot = tasks.new_task("shot", "SEQ010/SH0010", "layout")
    assert playblast.playblast_rel(shot, "SH0010_layout_v002") == \
        "07_dailies/SEQ010/SH0010/layout/SH0010_layout_v002_playblast.mp4"


def test_run_playblast_dry_run(tmp_path, capsys):
    cfg = types.SimpleNamespace(resolved_local_root=lambda: str(tmp_path),
                                remote_root="/r", blender_path=None)
    rc = playblast.run_playblast(cfg, creds=None,
                                 shot_blend="/x/SH0010_layout_v001.blend",
                                 task_id="shot-seq010_sh0010-layout", dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SH0010_layout_v001_playblast.mp4" in out


def test_delivery_formats_parse_and_env():
    settings = {"formats": [
        {"name": "16x9", "resolution_x": 1920, "resolution_y": 1080},
        {"name": "9x16", "resolution_x": 1080, "resolution_y": 1920},
        {"name": "", "resolution_x": 10, "resolution_y": 10},      # no name
        {"name": "bad", "resolution_x": 0, "resolution_y": 100},   # bad res
    ]}
    fmts = playblast.delivery_formats(settings)
    assert [f["name"] for f in fmts] == ["16x9", "9x16"]
    assert playblast.formats_env(fmts) == "16x9:1920x1080,9x16:1080x1920"
    assert playblast.delivery_formats({}) == []       # single-format project


def test_playblast_rel_per_format():
    t = {"entity": "SEQ010/SH0010", "step": "layout"}
    assert playblast.playblast_rel(t, "shot_v003", "16x9") == \
        "07_dailies/SEQ010/SH0010/layout/shot_v003_playblast_16x9.mp4"
    assert playblast.playblast_rel(t, "shot_v003", "9x16").endswith(
        "_playblast_9x16.mp4")
    # legacy single-format naming unchanged
    assert playblast.playblast_rel(t, "shot_v003") == \
        "07_dailies/SEQ010/SH0010/layout/shot_v003_playblast.mp4"
