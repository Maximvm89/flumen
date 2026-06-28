"""Tests for animpipe.playblast (pure helpers + dry-run; no real Blender/FTP)."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import playblast, tasks


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
