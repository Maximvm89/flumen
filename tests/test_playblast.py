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
    assert s["auto_light"] is True            # unlit shots get the camera rig
    s2 = playblast.playblast_settings({"playblast": {"engine": "BLENDER_WORKBENCH",
                                                     "fps": 30, "color": "MATERIAL",
                                                     "auto_light": False}})
    assert s2["engine"] == "BLENDER_WORKBENCH" and s2["fps"] == 30
    assert s2["color"] == "MATERIAL"
    assert s2["auto_light"] is False
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


def test_playblast_rel_sweatbox_kind():
    t = {"entity": "SEQ010/SH0010", "step": "animation"}
    # Sweatbox lands under a distinct '_sweatbox' stem so it never overwrites
    # the normal daily playblast clip.
    assert playblast.playblast_rel(t, "SH0010_animation_v011", kind="sweatbox") == \
        "07_dailies/SEQ010/SH0010/animation/SH0010_animation_v011_sweatbox.mp4"
    assert playblast.playblast_rel(t, "v1", "9x16", "sweatbox").endswith(
        "_sweatbox_9x16.mp4")


def test_run_playblast_dry_run_sweatbox(tmp_path, capsys):
    cfg = types.SimpleNamespace(resolved_local_root=lambda: str(tmp_path),
                                remote_root="/r", blender_path=None)
    rc = playblast.run_playblast(cfg, creds=None,
                                 shot_blend="/x/sweatbox.blend",
                                 task_id="shot-seq010_sh0010-animation",
                                 dry_run=True, sweatbox=True,
                                 label="SH0010_animation_v011")
    out = capsys.readouterr().out
    assert rc == 0
    # The label (the work file), not the temp snapshot name, names the clip.
    assert "SH0010_animation_v011_sweatbox.mp4" in out
    assert "sweatbox.blend_sweatbox" not in out


def test_run_playblast_only_formats(tmp_path, capsys, monkeypatch):
    """The Sweatbox's per-format tick boxes: only the ticked formats render."""
    from flumen import turntable as T
    monkeypatch.setattr(T, "_load_project_settings", lambda root: {"formats": [
        {"name": "16x9", "resolution_x": 1920, "resolution_y": 1080},
        {"name": "9x16", "resolution_x": 1080, "resolution_y": 1920}]})
    cfg = types.SimpleNamespace(resolved_local_root=lambda: str(tmp_path),
                                remote_root="/r", blender_path=None)

    def run(only):
        playblast.run_playblast(cfg, creds=None, shot_blend="/x/s.blend",
                                task_id="shot-x-animation", dry_run=True,
                                sweatbox=True, label="SH0010",
                                only_formats=only)
        return capsys.readouterr().out

    both = run(None)
    assert "SH0010_sweatbox_16x9.mp4" in both and "SH0010_sweatbox_9x16.mp4" in both
    wide = run(["16x9"])
    assert "SH0010_sweatbox_16x9.mp4" in wide and "9x16" not in wide
    tall = run(["9x16"])
    assert "SH0010_sweatbox_9x16.mp4" in tall and "16x9" not in tall
    # An unknown name must not silently render nothing.
    assert "SH0010_sweatbox_16x9.mp4" in run(["nope"])


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
