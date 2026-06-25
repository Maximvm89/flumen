"""Tests for animpipe.turntable pure helpers + task recording."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import turntable, tasks
from test_tasks import FakeSrv  # reuse the in-memory fake


def test_turntable_settings_defaults_and_override():
    s = turntable.turntable_settings({})
    assert s["engine"] == "EEVEE" and s["frames"] == 120
    s2 = turntable.turntable_settings({"turntable": {"frames": 60, "engine": "CYCLES"}})
    assert s2["frames"] == 60 and s2["engine"] == "CYCLES"
    assert s2["resolution_x"] == 1280  # untouched default preserved


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
    from animpipe import ledger
    assert ledger.load_ledgers(s, "/r")[rel][0] == "marco"
