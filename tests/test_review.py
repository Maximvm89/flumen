"""Tests for animpipe.review — the per-item review-status model (no network)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import review as R, tasks
from test_tasks import FakeSrv


def _tt(entity, version):
    return f"07_dailies/{entity}/model/{version}_turntable.mp4"


def test_version_and_clip_name():
    rel = _tt("characters/frankenstein", "frankenstein_model_v003")
    assert R.version_from_turntable(rel) == "frankenstein_model_v003"
    assert R.clip_name({"turntable": rel}) == "frankenstein_model_v003_turntable.mp4"


def test_review_status_default_and_value():
    assert R.review_status({}) == "to_review"
    assert R.review_status({"review_status": "approved"}) == "approved"
    assert R.review_status({"review_status": "bogus"}) == "to_review"


def test_item_date_from_timestamp():
    # 1782462729 -> a fixed UTC-ish date; just assert it formats as YYYY-MM-DD.
    d = R.item_date({"time": 1782462729})
    assert len(d) == 10 and d[4] == "-" and d[7] == "-"
    assert R.item_date({}) == ""


def test_review_items_every_version_filtered_and_sorted():
    t = tasks.new_task("asset", "characters/frankenstein", "model")
    t["status"] = "review"
    t["publishes"] = [
        {"turntable": _tt("characters/frankenstein", "frankenstein_model_v002"),
         "time": 100, "by": "marco"},                                   # to_review
        {"turntable": _tt("characters/frankenstein", "frankenstein_model_v003"),
         "time": 200, "by": "marco", "review_status": "approved"},
        {"files": ["x.blend"], "time": 300},                            # no turntable
    ]
    items = R.review_items([t])
    assert len(items) == 2                       # every turntable version, not the latest
    assert {i["version"] for i in items} == {
        "frankenstein_model_v002", "frankenstein_model_v003"}
    # newest time first
    assert items[0]["version"] == "frankenstein_model_v003"
    assert items[0]["status"] == "approved" and items[1]["status"] == "to_review"
    # status filter
    appr = R.review_items([t], statuses=["approved"])
    assert [i["version"] for i in appr] == ["frankenstein_model_v003"]


def test_set_status_on_task_approved_completes_task():
    t = tasks.new_task("asset", "characters/frankenstein", "model")
    t["status"] = "review"
    rel = _tt("characters/frankenstein", "frankenstein_model_v004")
    t["publishes"] = [{"turntable": rel, "time": 1}]
    assert R.set_status_on_task(t, rel, "approved") is True
    assert t["publishes"][0]["review_status"] == "approved"
    assert t["status"] == "done"          # approving completes the task


def test_set_status_on_task_non_approved_leaves_task_status():
    t = tasks.new_task("asset", "characters/frankenstein", "model")
    t["status"] = "review"
    rel = _tt("characters/frankenstein", "frankenstein_model_v004")
    t["publishes"] = [{"turntable": rel}, {"turntable": rel}]  # dup records
    assert R.set_status_on_task(t, rel, "reviewed") is True
    assert all(r["review_status"] == "reviewed" for r in t["publishes"])  # both stamped
    assert t["status"] == "review"        # unchanged
    assert R.set_status_on_task(t, "no_such.mp4", "reviewed") is False


def test_set_review_status_roundtrip_on_server():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/panda", "model"))
    rel = _tt("characters/panda", "panda_model_v001")
    tasks.publish_task(s, "/r", "marco", ["/tmp/panda_model_v001.blend"], t["id"])
    # attach a turntable to the publish (as the render would)
    from animpipe import turntable
    turntable.record_turntable(s, "/r", t["id"], rel, "marco")
    assert R.set_review_status(s, "/r", t["id"], rel, "approved", "marco") is True
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert reloaded["publishes"][-1]["review_status"] == "approved"
    assert reloaded["status"] == "done"


def test_review_items_carry_sheet_and_kind():
    surf = tasks.new_task("asset", "characters/frankenstein", "surface")
    surf["publishes"] = [{
        "turntable": "07_dailies/characters/frankenstein/surface/"
                     "frankenstein_surface_default_v001_turntable.mp4",
        "sheet": "07_dailies/characters/frankenstein/surface/"
                 "frankenstein_surface_default_v001_textures.png",
        "time": 100, "by": "marco",
        "files": ["…/frankenstein_surface_default_v001.blend"]}]
    model = tasks.new_task("asset", "characters/frankenstein", "model")
    model["publishes"] = [{"turntable": _tt("characters/frankenstein",
                                            "frankenstein_model_v004"),
                           "time": 50, "by": "marco"}]
    items = {i["kind"]: i for i in R.review_items([surf, model])}
    assert items["look"]["sheet"].endswith("_textures.png")
    assert items["model"]["sheet"] == ""           # model review has no sheet
    assert items["look"]["kind"] == "look"


def test_index_html_shows_sheet_image():
    manifest = R.build_manifest([
        {"entity": "characters/frank", "step": "surface", "version": "frank_surface_default_v001",
         "by": "marco", "status": "to_review", "clip": "x_turntable.mp4",
         "sheet": "07_dailies/characters/frank/surface/x_textures.png",
         "source": "x", "time": 1, "description": "look", "kind": "look"}], "2026-06-27")
    html = R.render_index_html(manifest)
    assert "<img" in html and "x_textures.png" in html and "<video" in html


def test_render_index_html_lists_items_and_status():
    manifest = R.build_manifest([
        {"entity": "characters/frankenstein", "step": "model",
         "version": "frankenstein_model_v003", "by": "marco", "status": "approved",
         "clip": "frankenstein_model_v003_turntable.mp4", "source": "x", "time": 1,
         "description": "shading pass"}], "2026-06-27")
    html = R.render_index_html(manifest)
    assert "frankenstein_model_v003_turntable.mp4" in html
    assert "Approved" in html and "marco" in html and "<video" in html
