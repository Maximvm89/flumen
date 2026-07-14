"""Tests for flumen.tasks (no real FTP)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import tasks
from workspace_app import core


class FakeSrv:
    """In-memory listdir/read_text/write_text."""
    def __init__(self):
        self.files = {}

    def listdir(self, d):
        prefix = d.rstrip("/") + "/"
        out, seen = [], set()
        for p in self.files:
            if p.startswith(prefix):
                name = p[len(prefix):].split("/")[0]
                if name in seen:
                    continue
                seen.add(name)
                out.append({"name": name, "is_dir": "/" in p[len(prefix):],
                            "size": 0, "mtime": 0, "owner": ""})
        return out

    def read_text(self, p):
        return self.files.get(p)

    def write_text(self, p, t):
        self.files[p] = t

    def remove(self, p):
        self.files.pop(p, None)

    def upload(self, local, remote, callback=None):
        self.files[remote] = f"<blend:{local}>"
        if callback:                       # simulate a single full-size transfer
            import os as _os
            try:
                n = _os.path.getsize(local)
            except OSError:
                n = 0
            callback(n, n)


def test_make_id_safe():
    assert tasks.make_id("shot", "SEQ010/SH0010", "animation") == \
        "shot-seq010_sh0010-animation"


def test_matches_query():
    t = tasks.new_task("asset", "characters/panda", "model",
                       assignees=["marco.parisi2"])
    assert tasks.matches_query(t, "")            # empty -> match all
    assert tasks.matches_query(t, "panda")       # entity
    assert tasks.matches_query(t, "marco")       # assignee
    assert tasks.matches_query(t, "model")       # step
    assert tasks.matches_query(t, "asset")       # type
    assert tasks.matches_query(t, "panda model")  # all terms (AND)
    assert tasks.matches_query(t, "To do")       # status label
    assert not tasks.matches_query(t, "saxophone")
    assert not tasks.matches_query(t, "panda lighting")  # one term fails


def test_validate_name():
    assert tasks.validate_name(None, "asset_name", "hero_armor")
    assert not tasks.validate_name(None, "asset_name", "Hero")        # uppercase
    assert not tasks.validate_name(None, "asset_name", "hero armor")  # space
    assert tasks.validate_name(None, "sequence", "SEQ010")
    assert not tasks.validate_name(None, "sequence", "seq010")        # lowercase
    assert tasks.validate_name(None, "shot", "SH0010")
    # config override wins
    assert tasks.validate_name({"asset_name": r"^x\d$"}, "asset_name", "x5")
    assert not tasks.validate_name({"asset_name": r"^x\d$"}, "asset_name", "hero")


def test_schema_helpers():
    schema = {
        "root": {"03_assets": {"characters": {}, "props": {}}},
        "asset_template": {"model": {"work": {}, "publish": {}}, "rig": {"work": {}}},
        "shot_template": {"animation": {"work": {}}, "_cache": {}},
    }
    assert tasks.asset_categories(schema) == ["characters", "props"]
    assert set(tasks.steps_for(schema, "asset")) == {"model", "rig"}
    assert tasks.steps_for(schema, "shot") == ["animation"]  # _cache has no work/


def test_delete_task():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("shot", "SEQ010/SH0010", "animation"))
    assert len(tasks.load_tasks(s, "/r")) == 1
    tasks.delete_task(s, "/r", t["id"])
    assert tasks.load_tasks(s, "/r") == []


def test_publish_task():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/panda", "model"))
    rels = tasks.publish_task(s, "/r", "marco",
                              ["/tmp/panda_model_v001.blend",
                               "/tmp/panda_model_v001.fbx"], t["id"],
                              description="first blocking pass")
    base = "03_assets/characters/panda/model/publish/"
    assert rels == [base + "panda_model_v001.blend", base + "panda_model_v001.fbx"]
    # both files landed at the right remote paths
    assert "/r/" + rels[0] in s.files and "/r/" + rels[1] in s.files
    reloaded = tasks.load_tasks(s, "/r")[0]
    # status advanced to review
    assert reloaded["status"] == "review"
    # publish history recorded with description + files
    assert len(reloaded["publishes"]) == 1
    h = reloaded["publishes"][0]
    assert h["description"] == "first blocking pass"
    assert h["by"] == "marco"
    assert h["files"] == rels
    # attribution recorded for both
    from flumen import ledger
    led = ledger.load_ledgers(s, "/r")
    assert led[rels[0]][0] == "marco" and led[rels[1]][0] == "marco"


def test_published_files_versions():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/panda", "model"))
    tasks.publish_task(s, "/r", "marco",
                       ["/tmp/panda_model_v001.blend", "/tmp/panda_model_v001.fbx"],
                       t["id"], description="first")
    tasks.publish_task(s, "/r", "anna",
                       ["/tmp/panda_model_v002.blend", "/tmp/panda_model_v002.fbx"],
                       t["id"], description="fixes")
    reloaded = tasks.get_task(s, "/r", t["id"])
    blends = tasks.published_files(reloaded, ".blend")
    assert [b["name"] for b in blends] == ["panda_model_v002.blend",
                                           "panda_model_v001.blend"]  # newest first
    assert blends[0]["by"] == "anna" and blends[0]["description"] == "fixes"
    # only .blend, no fbx
    assert all(b["name"].endswith(".blend") for b in blends)


def test_publish_task_missing():
    s = FakeSrv()
    assert tasks.publish_task(s, "/r", "marco", "/tmp/x.blend", "nope") is None


def test_task_path_helpers():
    shot = tasks.new_task("shot", "SEQ010/SH0010", "animation")
    asset = tasks.new_task("asset", "characters/hero", "model")
    assert tasks.task_dir_rel(shot) == "04_sequences/SEQ010/SH0010/animation"
    assert tasks.task_work_rel(shot) == "04_sequences/SEQ010/SH0010/animation/work"
    assert tasks.task_dir_rel(asset) == "03_assets/characters/hero/model"
    assert tasks.task_work_rel(asset) == "03_assets/characters/hero/model/work"


def test_save_load_roundtrip():
    s = FakeSrv()
    t = tasks.new_task("asset", "characters/hero", "model")
    tasks.save_task(s, "/r", t, actor="marco")
    loaded = tasks.load_tasks(s, "/r")
    assert len(loaded) == 1
    assert loaded[0]["entity"] == "characters/hero"
    assert loaded[0]["step"] == "model"
    assert loaded[0]["status"] == "todo"
    assert loaded[0]["updated_by"] == "marco"


def test_assign_multiple_and_unassign():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("shot", "SEQ010/SH0010", "animation"))
    tid = t["id"]
    tasks.assign(s, "/r", tid, "marco")
    tasks.assign(s, "/r", tid, "anna")
    cur = tasks.load_tasks(s, "/r")[0]
    assert cur["assignees"] == ["anna", "marco"]  # multiple, sorted
    tasks.assign(s, "/r", tid, "marco", add=False)
    assert tasks.load_tasks(s, "/r")[0]["assignees"] == ["anna"]


def test_set_status():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("shot", "SEQ010/SH0010", "lighting"))
    tasks.set_status(s, "/r", t["id"], "review", actor="marco")
    assert tasks.load_tasks(s, "/r")[0]["status"] == "review"
    assert tasks.set_status(s, "/r", t["id"], "bogus") is None  # invalid ignored


def _node(name, rel, is_dir, children=None):
    return core.TreeNode(name=name, rel=rel, is_dir=is_dir,
                         children=children or {})


def test_generate_from_tree():
    # build: 04_sequences/SEQ010/SH0010/{animation/{work},_cache}
    #        03_assets/characters/hero/{model/{work}}
    anim = _node("animation", "04_sequences/SEQ010/SH0010/animation", True,
                 {"work": _node("work", ".../work", True)})
    cache = _node("_cache", "04_sequences/SEQ010/SH0010/_cache", True)
    shot = _node("SH0010", "04_sequences/SEQ010/SH0010", True,
                 {"animation": anim, "_cache": cache})
    seq = _node("SEQ010", "04_sequences/SEQ010", True, {"SH0010": shot})
    seqs = _node("04_sequences", "04_sequences", True, {"SEQ010": seq})
    model = _node("model", "03_assets/characters/hero/model", True,
                  {"work": _node("work", ".../work", True)})
    hero = _node("hero", "03_assets/characters/hero", True, {"model": model})
    chars = _node("characters", "03_assets/characters", True, {"hero": hero})
    assets = _node("03_assets", "03_assets", True, {"characters": chars})
    root = _node("", "", True, {"04_sequences": seqs, "03_assets": assets})

    gen = tasks.generate_from_tree(root)
    by = {(t["type"], t["entity"], t["step"]) for t in gen}
    assert ("shot", "SEQ010/SH0010", "animation") in by
    assert ("asset", "characters/hero", "model") in by
    # _cache has no 'work' child -> not a task
    assert not any(t["step"] == "_cache" for t in gen)
    assert len(gen) == 2

    cat = tasks.build_catalog(root)
    assert cat["shot"] == {"SEQ010/SH0010": ["animation"]}
    assert cat["asset"] == {"characters/hero": ["model"]}


def test_next_version_uses_history_max_not_count():
    """Next version comes from the highest published version, ignoring gaps and
    duplicate records — so it never re-issues an existing number."""
    task = tasks.new_task("asset", "characters/frankenstein", "model")
    base = "frankenstein_model"
    assert tasks.next_version(task, base) == 1  # nothing published yet
    task["publishes"] = [
        {"files": [f"x/{base}_v001.blend", f"x/{base}_v001.fbx"]},
        {"files": [f"x/{base}_v003.blend"]},          # v002 gap
        {"files": [f"x/{base}_v003.blend"]},          # duplicate v003
    ]
    assert tasks.next_version(task, base) == 4         # max(3)+1, not count+1


def test_published_versions_and_file_version():
    assert tasks.file_version("a/frank_model_v007.fbx") == 7
    assert tasks.file_version("no_version.blend") is None
    task = {"publishes": [{"files": ["x_v001.blend", "x_v001.fbx"]},
                          {"files": ["x_v002.blend"]}]}
    assert tasks.published_versions(task) == {1, 2}


def test_publish_task_refuses_to_overwrite_existing_version():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/frank", "model"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/frank_model_v001.blend"], t["id"])
    # Re-publishing the SAME version must raise, not silently overwrite/duplicate.
    import pytest
    with pytest.raises(ValueError, match="already published"):
        tasks.publish_task(s, "/r", "marco", ["/tmp/frank_model_v001.blend"], t["id"])
    # A new version is fine.
    rels = tasks.publish_task(s, "/r", "marco", ["/tmp/frank_model_v002.blend"], t["id"])
    assert rels and rels[0].endswith("frank_model_v002.blend")
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert tasks.published_versions(reloaded) == {1, 2}


def test_publish_task_different_looks_share_version_number():
    # Two named looks version independently: clean_v001 must NOT clash with
    # default_v001 (they're different files), only an identical file is refused.
    s = FakeSrv()
    t = tasks.save_task(s, "/r",
                        tasks.new_task("asset", "characters/frank", "surface"))
    tasks.publish_task(s, "/r", "marco",
                       ["/tmp/frank_surface_default_v001.blend"], t["id"])
    # a *different look* at v001 — allowed
    rels = tasks.publish_task(s, "/r", "marco",
                              ["/tmp/frank_surface_clean_v001.blend"], t["id"])
    assert rels and rels[0].endswith("frank_surface_clean_v001.blend")
    # re-publishing the SAME look+version is still refused
    import pytest
    with pytest.raises(ValueError, match="already published"):
        tasks.publish_task(s, "/r", "marco",
                           ["/tmp/frank_surface_clean_v001.blend"], t["id"])


def test_sequences_from_tasks():
    ts = [
        tasks.new_task("shot", "SEQ010/SH0010", "animation"),
        tasks.new_task("shot", "SEQ010/SH0020", "layout"),
        tasks.new_task("shot", "SEQ020/SH0010", "comp"),
        tasks.new_task("asset", "characters/frank", "model"),  # ignored
    ]
    assert tasks.sequences_from_tasks(ts) == ["SEQ010", "SEQ020"]
    assert tasks.sequences_from_tasks([]) == []


def test_new_task_distinct_ids_per_step():
    ids = {tasks.new_task("shot", "SEQ010/SH0010", s)["id"]
           for s in ("layout", "animation", "lighting")}
    assert len(ids) == 3   # multi-step creation yields distinct tasks


def test_published_looks_latest_per_name():
    t = tasks.new_task("asset", "characters/frankenstein", "surface")
    pub = "03_assets/characters/frankenstein/surface/publish/"
    t["publishes"] = [
        {"files": [pub + "frankenstein_surface_default_v001.blend",
                   pub + "frankenstein_surface_default_v001.manifest.json"],
         "time": 1, "by": "marco"},
        {"files": [pub + "frankenstein_surface_default_v002.blend"],
         "time": 2, "by": "marco"},
        {"files": [pub + "frankenstein_surface_damaged_v001.blend"],
         "time": 3, "by": "anna"},
    ]
    looks = tasks.published_looks(t)
    assert [(l["look"], l["version"]) for l in looks] == [
        ("damaged", 1), ("default", 2)]              # latest per look, sorted by name
    default = next(l for l in looks if l["look"] == "default")
    assert default["blend_rel"] == pub + "frankenstein_surface_default_v002.blend"
    assert default["manifest_rel"] == \
        pub + "frankenstein_surface_default_v002.manifest.json"


def test_published_looks_empty():
    t = tasks.new_task("asset", "characters/frankenstein", "surface")
    assert tasks.published_looks(t) == []


def test_model_task_id_is_sibling():
    # The surface task's model sibling is the same entity at the 'model' step.
    surf = tasks.new_task("asset", "characters/hero", "surface")
    assert tasks.model_task_id(surf["entity"]) == \
        tasks.make_id("asset", "characters/hero", "model")


def test_publish_task_textures_go_under_textures_subdir():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/hero", "surface"))
    rels = tasks.publish_task(
        s, "/r", "marco",
        ["/tmp/hero_surface_v001.blend", "/tmp/hero_surface_v001.manifest.json"],
        t["id"], description="first look",
        texture_files=["/tmp/diffuse.a1b2c3d4.png", "/tmp/normal.e5f6a7b8.exr"])
    base = "03_assets/characters/hero/surface/publish/"
    # the .blend and manifest stay flat; textures land under publish/textures/
    assert rels[0] == base + "hero_surface_v001.blend"
    assert rels[1] == base + "hero_surface_v001.manifest.json"
    assert base + "textures/diffuse.a1b2c3d4.png" in rels
    assert base + "textures/normal.e5f6a7b8.exr" in rels
    assert "/r/" + base + "textures/diffuse.a1b2c3d4.png" in s.files
    # version detection still keys off the .blend, so the overwrite guard holds
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert tasks.published_versions(reloaded) == {1}
    assert reloaded["publishes"][0]["files"] == rels


def test_steps_for_category_overlay():
    schema = {"asset_template": {"model": {"work": {}}, "rig": {"work": {}}},
              "asset_templates": {"environments": {"dressing": {"work": {}}}},
              "shot_template": {"layout": {"work": {}}}}
    assert tasks.steps_for(schema, "asset") == ["model", "rig"]           # no category
    assert tasks.steps_for(schema, "asset", "characters") == ["model", "rig"]
    assert tasks.steps_for(schema, "asset", "environments") == \
        ["model", "rig", "dressing"]
    assert tasks.steps_for(schema, "shot") == ["layout"]                  # unaffected


def test_published_dressings_latest_per_name():
    t = tasks.new_task("asset", "environments/market_square", "dressing")
    base = "03_assets/environments/market_square/dressing/publish/"
    t["publishes"] = [
        {"files": [base + "market_square_dressing_night_market_v001.blend",
                   base + "market_square_dressing_night_market_v001.manifest.json"],
         "time": 1, "by": "leo"},
        {"files": [base + "market_square_dressing_night_market_v002.blend"],
         "time": 2, "by": "leo", "description": "more lanterns"},
        {"files": [base + "market_square_dressing_day_v001.blend"],
         "time": 3, "by": "marco"},
        {"files": [base + "market_square_model_v001.blend"], "time": 4},  # not a dressing
    ]
    out = tasks.published_dressings(t)
    assert [(d["dressing"], d["version"]) for d in out] == \
        [("day", 1), ("night_market", 2)]                     # latest per name, sorted
    nm = out[1]
    assert nm["blend_rel"].endswith("_v002.blend")
    assert nm["manifest_rel"].endswith("_v002.manifest.json")
    assert nm["description"] == "more lanterns"


def test_publish_task_skips_unchanged_sidecar_textures(tmp_path):
    """Flat publish/textures/ pool: same-size files on the server are skipped,
    new/changed ones upload. Look-style per-version subfolders always upload."""
    class SizedSrv(FakeSrv):
        def listdir(self, d):
            out = super().listdir(d)
            for e in out:
                p = d.rstrip("/") + "/" + e["name"]
                if p in self.files:
                    e["size"] = len(self.files[p])
            return out

    s = SizedSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "environments/house", "model"))
    blend = tmp_path / "house_model_v002.blend"
    blend.write_bytes(b"BLENDER-v4")          # valid header, 10 bytes
    tdir = tmp_path / "textures"
    tdir.mkdir()
    same = tdir / "wood.png"
    same.write_bytes(b"X" * 4)          # server copy has the same size -> skip
    new = tdir / "brick.png"
    new.write_bytes(b"Y" * 6)           # not on the server yet -> upload
    pub = tasks.task_dir_rel(t) + "/publish"
    s.files["/r/" + pub + "/textures/wood.png"] = "QQQQ"     # len 4

    rels = tasks.publish_task(s, "/r", "marco", [str(blend)], t["id"],
                        texture_files=[str(same), str(new)])
    assert any(r.endswith("house_model_v002.blend") for r in rels)
    assert "/r/" + pub + "/textures/brick.png" in s.files
    # the unchanged texture was NOT re-uploaded (server content untouched)
    assert s.files["/r/" + pub + "/textures/wood.png"] == "QQQQ"


def test_publish_task_uploads_changed_sidecar_texture(tmp_path):
    class SizedSrv(FakeSrv):
        def listdir(self, d):
            out = super().listdir(d)
            for e in out:
                p = d.rstrip("/") + "/" + e["name"]
                if p in self.files:
                    e["size"] = len(self.files[p])
            return out

    s = SizedSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "environments/house", "model"))
    blend = tmp_path / "house_model_v003.blend"
    blend.write_bytes(b"BLENDER-v4")
    tdir = tmp_path / "textures"
    tdir.mkdir()
    changed = tdir / "wood.png"
    changed.write_bytes(b"X" * 9)       # size differs from the server's 4
    pub = tasks.task_dir_rel(t) + "/publish"
    s.files["/r/" + pub + "/textures/wood.png"] = "QQQQ"

    tasks.publish_task(s, "/r", "marco", [str(blend)], t["id"],
                 texture_files=[str(changed)])
    assert s.files["/r/" + pub + "/textures/wood.png"] != "QQQQ"


def test_validate_blend_file(tmp_path):
    import zstandard
    # healthy uncompressed
    ok = tmp_path / "ok.blend"
    ok.write_bytes(b"BLENDER-v405" + b"\x00" * 2048)
    assert tasks.validate_blend_file(str(ok)) is None
    # healthy zstd-compressed
    okz = tmp_path / "okz.blend"
    okz.write_bytes(zstandard.ZstdCompressor().compress(b"BLENDER" + b"\x00" * 4096))
    assert tasks.validate_blend_file(str(okz)) is None
    # truncated zstd (the orso v021 case): cut the stream mid-frame
    bad = tmp_path / "bad.blend"
    full = zstandard.ZstdCompressor().compress(b"BLENDER" + b"\x01\x02\x03" * 100000)
    bad.write_bytes(full[: len(full) // 2] + b"\x00" * 2048)
    err = tasks.validate_blend_file(str(bad))
    assert err and ("truncat" in err or "corrupt" in err)
    # garbage header
    junk = tmp_path / "junk.blend"
    junk.write_bytes(b"NOTABLEND" * 100)
    assert "unknown header" in tasks.validate_blend_file(str(junk))
    # missing file: not this check's job (the upload fails loudly)
    assert tasks.validate_blend_file(str(tmp_path / "nope.blend")) is None


def test_publish_task_refuses_corrupt_blend(tmp_path):
    import pytest
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/orso", "model"))
    bad = tmp_path / "orso_model_v021.blend"
    bad.write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 4096)   # broken zstd
    with pytest.raises(ValueError, match="publish aborted"):
        tasks.publish_task(s, "/r", "elisa", [str(bad)], t["id"])
