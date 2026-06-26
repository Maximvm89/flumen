"""Tests for animpipe.review pure helpers + task stamping (no network)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import review as R, tasks
from test_tasks import FakeSrv  # reuse the in-memory fake


def _tt(entity, version):
    step = "model"
    return f"07_dailies/{entity}/{step}/{version}_turntable.mp4"


def test_review_dir_rel():
    assert R.review_dir_rel("2026-06-26") == "07_dailies/_reviews/2026-06-26"


def test_version_and_clip_name():
    rel = _tt("characters/frankenstein", "frankenstein_model_v003")
    assert R.version_from_turntable(rel) == "frankenstein_model_v003"
    assert R.clip_name({"turntable": rel}) == "frankenstein_model_v003_turntable.mp4"


def test_collectable_status_and_not_reviewed():
    t_review = tasks.new_task("asset", "characters/frankenstein", "model")
    t_review["status"] = "review"
    t_review["publishes"] = [
        {"turntable": _tt("characters/frankenstein", "v1")},                 # collect
        {"turntable": _tt("characters/frankenstein", "v2"), "reviewed": "x"},  # done
        {"files": ["a.blend"]},                                              # no tt
    ]
    t_todo = tasks.new_task("asset", "props/axe", "model")
    t_todo["status"] = "todo"
    t_todo["publishes"] = [{"turntable": _tt("props/axe", "v1")}]  # wrong status

    picked = R.collectable([t_review, t_todo])
    assert len(picked) == 1
    assert picked[0][1]["turntable"] == _tt("characters/frankenstein", "v1")


def test_collectable_status_override():
    t = tasks.new_task("asset", "props/axe", "model")
    t["status"] = "done"
    t["publishes"] = [{"turntable": _tt("props/axe", "v1")}]
    assert R.collectable([t], status="review") == []
    assert len(R.collectable([t], status="done")) == 1


def test_mark_reviewed_targets_matching_record():
    rel1 = _tt("characters/frankenstein", "v1")
    rel2 = _tt("characters/frankenstein", "v2")
    task = tasks.new_task("asset", "characters/frankenstein", "model")
    task["publishes"] = [{"turntable": rel1}, {"turntable": rel2}]
    assert R.mark_reviewed(task, rel2, "2026-06-26") is True
    assert task["publishes"][0].get("reviewed") is None
    assert task["publishes"][1]["reviewed"] == "2026-06-26"
    assert R.mark_reviewed(task, "nope.mp4", "2026-06-26") is False


def test_build_manifest_sorted_and_counted():
    e1 = {"entity": "props/axe", "step": "model"}
    e2 = {"entity": "characters/frankenstein", "step": "model"}
    m = R.build_manifest([e1, e2], "2026-06-26")
    assert m["count"] == 2 and m["date"] == "2026-06-26"
    assert [c["entity"] for c in m["clips"]] == ["characters/frankenstein", "props/axe"]


def test_render_index_html_lists_every_clip():
    manifest = R.build_manifest([
        {"entity": "characters/frankenstein", "step": "model",
         "version": "frankenstein_model_v003", "by": "marco",
         "description": "first pass",
         "clip": "frankenstein_model_v003_turntable.mp4"},
    ], "2026-06-26")
    html = R.render_index_html(manifest)
    assert "frankenstein_model_v003_turntable.mp4" in html
    assert "marco" in html and "first pass" in html
    assert "<video" in html and "2026-06-26" in html


def test_render_index_html_escapes():
    manifest = R.build_manifest([
        {"entity": "a<b", "step": "model", "version": "v1", "by": "x",
         "description": "a & <b>", "clip": "c.mp4"}], "2026-06-26")
    html = R.render_index_html(manifest)
    assert "a<b" not in html and "a&lt;b" in html
    assert "a &amp; &lt;b&gt;" in html


def test_record_collected_stamps_last_publish():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/frankenstein", "model"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"], t["id"])
    rel = _tt("characters/frankenstein", "frankenstein_model_v001")
    # attach a turntable to the publish first (as turntable.record_turntable would)
    from animpipe import turntable
    turntable.record_turntable(s, "/r", t["id"], rel, "marco")

    assert R.record_collected(s, "/r", t["id"], rel, "2026-06-26", "marco") is True
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert reloaded["publishes"][-1]["reviewed"] == "2026-06-26"
    # idempotent-ish: a non-matching rel returns False
    assert R.record_collected(s, "/r", t["id"], "other.mp4", "2026-06-26", "marco") is False
