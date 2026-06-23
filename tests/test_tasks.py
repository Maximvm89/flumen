"""Tests for animpipe.tasks (no real FTP)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import tasks
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

    def upload(self, local, remote):
        self.files[remote] = f"<blend:{local}>"


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
    from animpipe import ledger
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
