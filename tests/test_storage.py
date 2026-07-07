import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flumen import storage as S


def _f(rel, size=100, mtime=0.0):
    return {"rel": rel, "size": size, "mtime": mtime}


def test_scan_local_walks_and_relativizes(tmp_path):
    (tmp_path / "03_assets" / "environments" / "disco" / "model" / "work").mkdir(
        parents=True)
    p = (tmp_path / "03_assets" / "environments" / "disco" / "model" / "work"
         / "disco_model_v001.blend")
    p.write_bytes(b"x" * 10)
    (tmp_path / "top.txt").write_text("hi")
    files = S.scan_local(str(tmp_path))
    rels = {f["rel"]: f for f in files}
    assert "top.txt" in rels
    key = "03_assets/environments/disco/model/work/disco_model_v001.blend"
    assert rels[key]["size"] == 10


def test_is_temp_patterns():
    assert S.is_temp("07_dailies/x/_tt_frames_disco_v001/frame_0001.png")
    assert S.is_temp("03_assets/a/model/work/disco_model_v001.blend1")
    assert S.is_temp("03_assets/.DS_Store")
    assert not S.is_temp("03_assets/a/model/work/disco_model_v001.blend")


def test_split_work_versions_keeps_latest_two():
    files = [_f(f"a/model/work/disco_model_v{v:03d}.blend") for v in (1, 2, 3, 7)]
    files.append(_f("a/model/publish/disco_model_v001.blend"))  # not work
    old, active = S.split_work_versions(files, keep_latest=2)
    assert old == {"a/model/work/disco_model_v001.blend",
                   "a/model/work/disco_model_v002.blend"}
    assert active == {"a/model/work/disco_model_v003.blend",
                      "a/model/work/disco_model_v007.blend"}


def test_split_work_versions_groups_by_name():
    files = [_f("a/work/x_v001.blend"), _f("a/work/y_v001.blend")]
    old, active = S.split_work_versions(files, keep_latest=2)
    assert old == set() and len(active) == 2


def test_classify_all_categories():
    files = [
        _f("03_assets/e/disco/model/publish/disco_model_v001.blend", 50),
        _f("03_assets/e/disco/model/work/disco_model_v001.blend", 10),
        _f("03_assets/e/disco/model/work/disco_model_v002.blend", 20),
        _f("03_assets/e/disco/model/work/disco_model_v003.blend", 30),
        _f("03_assets/e/disco/model/work/disco_model_v004.blend", 40),
        _f("03_assets/e/disco/model/work/disco_model_v004.blend1", 40),
        _f("05_library/hdri/studio.hdr", 99),          # size differs from server
        _f("dev/notes.txt", 5),                        # nowhere else
    ]
    remote = {
        "03_assets/e/disco/model/publish/disco_model_v001.blend": 50,
        "03_assets/e/disco/model/work/disco_model_v001.blend": 10,
        "05_library/hdri/studio.hdr": 123456,
    }
    cats = {r["rel"]: r["category"] for r in S.classify(files, remote)}
    assert cats["03_assets/e/disco/model/publish/disco_model_v001.blend"] == S.MIRRORED
    # superseded AND mirrored -> plain safe
    assert cats["03_assets/e/disco/model/work/disco_model_v001.blend"] == S.MIRRORED
    assert cats["03_assets/e/disco/model/work/disco_model_v002.blend"] == S.OLD_WORK
    assert cats["03_assets/e/disco/model/work/disco_model_v003.blend"] == S.ACTIVE_WORK
    assert cats["03_assets/e/disco/model/work/disco_model_v004.blend"] == S.ACTIVE_WORK
    assert cats["03_assets/e/disco/model/work/disco_model_v004.blend1"] == S.TEMP
    assert cats["05_library/hdri/studio.hdr"] == S.LOCAL_ONLY   # differs = keep
    assert cats["dev/notes.txt"] == S.LOCAL_ONLY


def test_active_work_never_deletable_even_when_mirrored():
    files = [_f("a/work/x_v001.blend", 10)]
    cats = S.classify(files, {"a/work/x_v001.blend": 10})
    assert cats[0]["category"] == S.ACTIVE_WORK
    assert S.ACTIVE_WORK not in S.DELETABLE


def test_summarize_counts_sizes_and_reclaimable():
    recs = [
        {"rel": "a", "size": 100, "category": S.MIRRORED},
        {"rel": "b", "size": 30, "category": S.TEMP},
        {"rel": "c", "size": 7, "category": S.OLD_WORK},
        {"rel": "d", "size": 1, "category": S.LOCAL_ONLY},
    ]
    s = S.summarize(recs)
    assert s[S.MIRRORED] == {"count": 1, "size": 100}
    assert s[S.ACTIVE_WORK] == {"count": 0, "size": 0}
    assert s["reclaimable"] == 130      # mirrored + temp, not old work


def test_group_key_entity_or_top_level():
    assert S.group_key("03_assets/environments/disco/model/work/f.blend") == \
        "03_assets/environments/disco"
    assert S.group_key("04_shots/sq010/sh010/layout/f.blend") == \
        "04_shots/sq010/sh010"
    assert S.group_key("02_pipeline/users.json") == "02_pipeline"
    assert S.group_key("top.txt") == "top.txt"


def test_human_size():
    assert S.human_size(512) == "512 B"
    assert S.human_size(2048) == "2.0 KB"
    assert S.human_size(14.2 * 1024**3).endswith("GB")
