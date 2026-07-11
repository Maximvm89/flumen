"""Tests for flumen.review — the per-item review-status model (no network)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import review as R, tasks
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


def test_matches_query_across_fields_and_terms():
    it = {"entity": "characters/frankenstein", "step": "surface",
          "version": "frankenstein_surface_rosa_v001", "by": "leonardo.milossi",
          "status": "to_review", "description": "first look", "date": "2026-06-28"}
    assert R.matches_query(it, "")                 # empty -> everything
    assert R.matches_query(it, "frankenstein")     # entity/version
    assert R.matches_query(it, "surface")          # step
    assert R.matches_query(it, "leonardo")         # artist
    assert R.matches_query(it, "rosa")             # version detail
    assert R.matches_query(it, "To review")        # status label
    assert R.matches_query(it, "frank rosa leo")   # all terms must match (AND)
    assert not R.matches_query(it, "marco")        # different artist
    assert not R.matches_query(it, "frank zzz")    # one term misses -> no match


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
    from flumen import turntable
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


def test_delete_review_removes_media_and_clears_record():
    s = FakeSrv()
    t = tasks.new_task("asset", "characters/frankenstein", "surface")
    tt = ("07_dailies/characters/frankenstein/surface/"
          "frankenstein_surface_black_white_v004_turntable.mp4")
    sheet = ("07_dailies/characters/frankenstein/surface/"
             "frankenstein_surface_black_white_v004_textures.png")
    t["publishes"] = [{"turntable": tt, "sheet": sheet, "time": 1, "by": "marco",
                       "files": ["…/frankenstein_surface_black_white_v004.blend"]}]
    tasks.save_task(s, "/r", t)
    s.files["/r/" + tt] = "<mp4>"
    s.files["/r/" + sheet] = "<png>"
    item = R.review_items([t])[0]

    assert R.delete_review(s, "/r", item) is True
    # files gone from the server
    assert "/r/" + tt not in s.files and "/r/" + sheet not in s.files
    # record no longer a review item
    reloaded = tasks.get_task(s, "/r", t["id"])
    rec = reloaded["publishes"][0]
    assert "turntable" not in rec and "sheet" not in rec
    assert R.review_items([reloaded]) == []
    # the published look .blend is untouched
    assert rec["files"] == ["…/frankenstein_surface_black_white_v004.blend"]


def _still_task():
    t = tasks.new_task("asset", "environments/disco", "model")
    t["stills"] = [{"file": ("07_dailies/environments/disco/model/"
                             "disco_model_review_20260702_162929.png"),
                    "by": "marco", "time": 1751468969.0,
                    "review_status": "to_review"}]
    return t


def test_review_items_include_stills():
    t = _still_task()
    items = R.review_items([t])
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "still"
    assert it["source"] == t["stills"][0]["file"]
    assert it["clip"] == "disco_model_review_20260702_162929.png"
    # redundant '<leaf>_<step>_' prefix trimmed from the label
    assert it["version"] == "review_20260702_162929"
    assert it["by"] == "marco"
    assert it["status"] == "to_review"
    # status filter applies to stills too
    assert R.review_items([t], statuses={"reviewed"}) == []


def test_set_status_on_still():
    t = _still_task()
    rel = t["stills"][0]["file"]
    assert R.set_status_on_task(t, rel, "reviewed") is True
    assert t["stills"][0]["review_status"] == "reviewed"
    assert t["status"] != "done"
    assert R.set_status_on_task(t, rel, "approved") is True
    assert t["status"] == "done"


def test_delete_review_removes_still_record():
    s = FakeSrv()
    t = _still_task()
    rel = t["stills"][0]["file"]
    tasks.save_task(s, "/r", t)
    s.files["/r/" + rel] = "<png>"
    item = R.review_items([t])[0]
    assert R.delete_review(s, "/r", item) is True
    assert "/r/" + rel not in s.files
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert reloaded["stills"] == []
    assert R.review_items([reloaded]) == []


def test_index_html_embeds_still_as_image():
    t = _still_task()
    manifest = R.build_manifest(R.review_items([t]), "2026-07-02")
    html = R.render_index_html(manifest)
    assert '<img src="disco_model_review_20260702_162929.png"' in html
    assert "<video" not in html


def test_write_review_folder_holds_only_this_export(tmp_path):
    import os
    from test_tasks import FakeSrv
    s = FakeSrv()
    local_root = str(tmp_path)

    def item(name):
        rel = f"07_dailies/characters/x/model/{name}_turntable.mp4"
        p = os.path.join(local_root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("vid")
        return {"source": rel, "clip": f"{name}_turntable.mp4", "entity": "x",
                "step": "model", "version": name, "status": "to_review",
                "by": "m", "time": 1, "task_id": "t", "description": ""}

    a, b = item("a_v1"), item("b_v1")
    folder = os.path.join(local_root, "07_dailies", "_reviews", "2026-06-27")
    mp4s = lambda: {f for f in os.listdir(folder) if f.endswith(".mp4")}

    R.write_review_folder(s, remote_root="/r", local_root=local_root,
                          items=[a, b], date_str="2026-06-27", username="m")
    assert mp4s() == {"a_v1_turntable.mp4", "b_v1_turntable.mp4"}

    # re-export only one -> the folder holds ONLY that one (no accumulation)
    R.write_review_folder(s, remote_root="/r", local_root=local_root,
                          items=[a], date_str="2026-06-27", username="m")
    assert mp4s() == {"a_v1_turntable.mp4"}
    # and the stale one is gone from the server too
    assert not any("b_v1_turntable.mp4" in k for k in s.files)


def test_render_index_html_lists_items_and_status():
    manifest = R.build_manifest([
        {"entity": "characters/frankenstein", "step": "model",
         "version": "frankenstein_model_v003", "by": "marco", "status": "approved",
         "clip": "frankenstein_model_v003_turntable.mp4", "source": "x", "time": 1,
         "description": "shading pass"}], "2026-06-27")
    html = R.render_index_html(manifest)
    assert "frankenstein_model_v003_turntable.mp4" in html
    assert "Approved" in html and "marco" in html and "<video" in html


def test_playblast_version_and_shot_kind():
    rel = ("07_dailies/SEQ010/SH0010/layout/SH0010_layout_v002_playblast.mp4")
    assert R.version_from_turntable(rel) == "SH0010_layout_v002"
    shot = tasks.new_task("shot", "SEQ010/SH0010", "layout")
    shot["publishes"] = [{"turntable": rel, "time": 1, "by": "marco"}]
    items = R.review_items([shot])
    assert len(items) == 1
    assert items[0]["kind"] == "shot" and items[0]["version"] == "SH0010_layout_v002"


def _pb(entity, version, fmt):
    return f"07_dailies/{entity}/layout/{version}_playblast_{fmt}.mp4"


def test_multi_format_playblast_review_items():
    """Dual-delivery playblasts: every format is its own Dailies item, sharing
    one review status; version labels carry the format tag."""
    t = tasks.new_task("shot", "SEQ010/SH0010", "layout")
    r16 = _pb("SEQ010/SH0010", "SH0010_layout_v016", "16x9")
    r9 = _pb("SEQ010/SH0010", "SH0010_layout_v016", "9x16")
    t["publishes"] = [{"turntable": r16, "turntables": [r16, r9], "time": 100}]
    items = R.review_items([t])
    assert {i["source"] for i in items} == {r16, r9}
    assert {i["version"] for i in items} == {
        "SH0010_layout_v016 · 16x9", "SH0010_layout_v016 · 9x16"}
    # setting status via EITHER format stamps the shared record
    assert R.set_status_on_task(t, r9, "approved") is True
    assert t["publishes"][0]["review_status"] == "approved"
    assert R.review_items([t], statuses=["approved"]) and \
        all(i["status"] == "approved" for i in R.review_items([t]))


def test_delete_one_format_keeps_the_other():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("shot", "SEQ010/SH0010", "layout"))
    r16 = _pb("SEQ010/SH0010", "SH0010_layout_v016", "16x9")
    r9 = _pb("SEQ010/SH0010", "SH0010_layout_v016", "9x16")
    tasks.publish_task(s, "/r", "marco", ["/tmp/SH0010_layout_v016.blend"], t["id"])
    from flumen import turntable
    turntable.record_turntable(s, "/r", t["id"], r16, "marco", extra_rels=[r9])
    loaded = tasks.get_task(s, "/r", t["id"])
    assert loaded["publishes"][-1]["turntables"] == [r16, r9]
    # delete the 9x16 item -> 16x9 remains a review item
    assert R.delete_review(s, "/r", {"task_id": t["id"], "source": r9}) is True
    loaded = tasks.get_task(s, "/r", t["id"])
    rec = loaded["publishes"][-1]
    assert rec["turntable"] == r16 and "turntables" not in rec
    # delete the last one -> record stops being a review item
    assert R.delete_review(s, "/r", {"task_id": t["id"], "source": r16}) is True
    loaded = tasks.get_task(s, "/r", t["id"])
    assert "turntable" not in loaded["publishes"][-1]


def test_version_from_turntable_format_suffix():
    assert R.version_from_turntable("a/SH0010_layout_v016_playblast_9x16.mp4") \
        == "SH0010_layout_v016 · 9x16"
    assert R.version_from_turntable("a/SH0010_layout_v016_playblast.mp4") \
        == "SH0010_layout_v016"
    assert R.version_from_turntable("a/x_model_v003_turntable.mp4") == "x_model_v003"
