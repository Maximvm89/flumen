"""Tests for flumen.turntable pure helpers + task recording."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import turntable, tasks
from test_tasks import FakeSrv  # reuse the in-memory fake


def test_turntable_settings_defaults_and_override():
    s = turntable.turntable_settings({})
    assert s["engine"] == "EEVEE" and s["frames"] == 120
    s2 = turntable.turntable_settings({"turntable": {"frames": 60, "engine": "CYCLES"}})
    assert s2["frames"] == 60 and s2["engine"] == "CYCLES"
    assert s2["resolution_x"] == 1280  # untouched default preserved


def test_bundled_path_source_vs_frozen(monkeypatch):
    # From source: next to the module.
    monkeypatch.delattr(turntable.sys, "frozen", raising=False)
    p = turntable._bundled_path("blender_turntable.py")
    assert p.endswith("blender_turntable.py")
    assert "flumen" in p and p == \
        str(Path(turntable.__file__).parent / "blender_turntable.py")
    # Frozen: under the PyInstaller bundle's flumen/ data dir.
    monkeypatch.setattr(turntable.sys, "frozen", True, raising=False)
    monkeypatch.setattr(turntable.sys, "_MEIPASS", "/bundle", raising=False)
    assert turntable._bundled_path("blender_turntable.py") == \
        "/bundle/flumen/blender_turntable.py".replace("/", __import__("os").sep)


def test_dailies_rel():
    asset = tasks.new_task("asset", "characters/panda", "model")
    shot = tasks.new_task("shot", "SEQ010/SH0010", "animation")
    assert turntable.dailies_rel(asset, "panda_model_v003") == \
        "07_dailies/characters/panda/model/panda_model_v003_turntable.mp4"
    assert turntable.dailies_rel(shot, "SH0010_animation_v001") == \
        "07_dailies/SEQ010/SH0010/animation/SH0010_animation_v001_turntable.mp4"


def test_record_turntable_attaches_to_last_publish():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/panda", "model"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/panda_model_v001.blend"], t["id"],
                       description="first")
    rel = "07_dailies/characters/panda/model/panda_model_v001_turntable.mp4"
    turntable.record_turntable(s, "/r", t["id"], rel, "marco")
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert reloaded["publishes"][-1]["turntable"] == rel
    from flumen import ledger
    assert ledger.load_ledgers(s, "/r")[rel][0] == "marco"


def test_run_look_review_dry_run(tmp_path, capsys):
    import types
    cfg = types.SimpleNamespace(resolved_local_root=lambda: str(tmp_path),
                                remote_root="/r", blender_path=None)
    rc = turntable.run_look_review(
        cfg, creds=None, task_id="asset-characters_frank-surface",
        entity="characters/frank", base="frank_surface_default", version=2,
        model_path="/x/model.blend", look_blend="/x/look.blend",
        manifest_path="/x/look.manifest.json",
        blend_rel="03_assets/characters/frank/surface/publish/"
                  "frank_surface_default_v002.blend",
        hdri=None, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert ("07_dailies/characters/frank/surface/"
            "frank_surface_default_v002_turntable.mp4") in out
    assert "frank_surface_default_v002_textures.png" in out
    assert "turntable + sheet" in out


def test_look_dailies_rel():
    assert turntable.look_dailies_rel(
        "characters/frankenstein", "frankenstein_surface_default_v001",
        "turntable.mp4") == \
        ("07_dailies/characters/frankenstein/surface/"
         "frankenstein_surface_default_v001_turntable.mp4")
    assert turntable.look_dailies_rel(
        "characters/frankenstein", "frankenstein_surface_default_v001",
        "textures.png").endswith("_v001_textures.png")


def test_record_review_media_matches_the_right_look_record():
    s = FakeSrv()
    t = tasks.save_task(s, "/r",
                        tasks.new_task("asset", "characters/frank", "surface"))
    # two looks published; review media must land on the SECOND one only
    tasks.publish_task(s, "/r", "marco",
                       ["/tmp/frank_surface_default_v001.blend"], t["id"])
    rels = tasks.publish_task(s, "/r", "marco",
                              ["/tmp/frank_surface_clean_v001.blend"], t["id"])
    blend_rel = next(r for r in rels if r.endswith("frank_surface_clean_v001.blend"))
    tt = "07_dailies/characters/frank/surface/frank_surface_clean_v001_turntable.mp4"
    sheet = "07_dailies/characters/frank/surface/frank_surface_clean_v001_textures.png"
    assert turntable.record_review_media(s, "/r", t["id"], blend_rel, "marco",
                                         turntable=tt, sheet=sheet) is True
    recs = tasks.get_task(s, "/r", t["id"])["publishes"]
    clean = next(r for r in recs if any("clean" in f for f in r["files"]))
    default = next(r for r in recs if any("default" in f for f in r["files"]))
    assert clean.get("turntable") == tt and clean.get("sheet") == sheet
    assert "turntable" not in default and "sheet" not in default   # untouched
    # no matching record -> False
    assert turntable.record_review_media(s, "/r", t["id"], "nope.blend", "marco",
                                         turntable=tt) is False


def test_face_camera_yaw():
    import math
    front = (0.0, -1.0)                       # studio convention: front = -Y
    # camera already in front -> no rotation
    assert turntable.face_camera_yaw(front, (0.0, -5.0)) == 0.0
    # camera behind -> half turn
    assert abs(abs(turntable.face_camera_yaw(front, (0.0, 5.0)))
               - math.pi) < 1e-9
    # camera on -X -> quarter turn, sign must rotate front ONTO the camera dir
    yaw = turntable.face_camera_yaw(front, (-5.0, 0.0))
    fx, fy = front
    rx = fx * math.cos(yaw) - fy * math.sin(yaw)
    ry = fx * math.sin(yaw) + fy * math.cos(yaw)
    assert abs(rx - -1.0) < 1e-9 and abs(ry) < 1e-9
    # degenerate: camera straight above the socket -> leave the asset alone
    assert turntable.face_camera_yaw(front, (0.0, 0.0)) == 0.0


def test_face_camera_env_defaults():
    s = turntable.turntable_settings({})
    assert s["face_camera"] is True and s["front_offset_deg"] == 0.0
    s2 = turntable.turntable_settings({"turntable": {"face_camera": False,
                                                     "front_offset_deg": 90}})
    assert s2["face_camera"] is False and s2["front_offset_deg"] == 90


def test_run_look_review_dry_run_turntable_only(tmp_path, capsys):
    import types
    cfg = types.SimpleNamespace(resolved_local_root=lambda: str(tmp_path),
                                remote_root="/r", blender_path=None)
    rc = turntable.run_look_review(
        cfg, creds=None, task_id="asset-characters_frank-surface",
        entity="characters/frank", base="frank_surface_default", version=2,
        model_path="/x/model.blend", look_blend="/x/look.blend",
        manifest_path="/x/look.manifest.json",
        blend_rel="03_assets/characters/frank/surface/publish/"
                  "frank_surface_default_v002.blend",
        hdri=None, turntable_only=True, dry_run=True)
    assert rc == 0
    assert "turntable only" in capsys.readouterr().out


def test_record_turntable_extra_rels_shared_record():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/panda", "model"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/panda_model_v001.blend"], t["id"])
    r1 = "07_dailies/characters/panda/model/panda_model_v001_turntable.mp4"
    turntable.record_turntable(s, "/r", t["id"], r1, "marco")
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert "turntables" not in reloaded["publishes"][-1]   # single clip: legacy shape
