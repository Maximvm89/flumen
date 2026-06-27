"""Tests for animpipe.elements — the shot assembly / breakdown model (no network)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import elements as E, tasks
from test_tasks import FakeSrv


# ---- pure helpers ----------------------------------------------------------

def test_assembly_rel():
    assert E.assembly_rel("SEQ010/SH0010") == \
        "04_sequences/SEQ010/SH0010/assembly.json"


def test_element_id_collision():
    asm = E.empty_assembly("SEQ010/SH0010")
    for _ in range(3):
        E.add_element(asm, E.new_element("characters/frankenstein"))
    ids = [e["id"] for e in asm["elements"]]
    assert ids == ["frankenstein", "frankenstein_1", "frankenstein_2"]
    # cameras namespace independently
    E.add_element(asm, E.new_element("", "camera"))
    E.add_element(asm, E.new_element("", "camera"))
    cam_ids = [e["id"] for e in asm["elements"] if e["kind"] == "camera"]
    assert cam_ids == ["camera", "camera_1"]


def test_new_element_camera_clears_asset():
    e = E.new_element("characters/frankenstein", "camera")
    assert e["kind"] == "camera" and e["asset"] == "" and e["label"] == "camera"
    a = E.new_element("characters/frankenstein")
    assert a["asset"] == "characters/frankenstein" and a["label"] == "frankenstein"


def test_new_element_rejects_unknown_kind():
    try:
        E.new_element("x", "prop")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown kind")


def test_add_remove_element():
    asm = E.empty_assembly("SEQ010/SH0010")
    E.add_element(asm, E.new_element("characters/frankenstein"))
    E.add_element(asm, E.new_element("props/lantern"))
    E.remove_element(asm, "frankenstein")
    assert [e["id"] for e in asm["elements"]] == ["lantern"]


def test_normalize_backfills_ids_and_drops_dupes_and_unknown():
    raw = {"shot": "SEQ010/SH0010", "elements": [
        {"kind": "asset", "asset": "characters/frankenstein"},        # no id
        {"id": "frankenstein", "kind": "asset", "asset": "x"},        # dup id
        {"id": "cam", "kind": "camera", "asset": "leftover"},          # camera clears asset
        {"id": "bad", "kind": "prop", "asset": "y"},                   # unknown kind dropped
    ]}
    norm = E.normalize(raw)
    ids = [e["id"] for e in norm["elements"]]
    assert ids == ["frankenstein", "frankenstein_1", "cam"]
    cam = norm["elements"][2]
    assert cam["kind"] == "camera" and cam["asset"] == ""


def test_frame_range_defaults_and_custom():
    asm = E.empty_assembly("SEQ010/SH0010")
    assert asm["frame_start"] == 1001 and asm["duration"] == 100
    assert E.frame_range(asm) == (1001, 1100)         # default
    asm["duration"] = 240
    assert E.frame_range(asm) == (1001, 1240)


def test_normalize_preserves_and_defaults_frame_range():
    # missing -> defaults
    n1 = E.normalize({"shot": "SEQ010/SH0010", "elements": []})
    assert n1["frame_start"] == 1001 and n1["duration"] == 100
    # present -> preserved; roundtrips through save/load
    s = FakeSrv()
    asm = E.empty_assembly("SEQ010/SH0010")
    asm["duration"] = 175
    E.save_assembly(s, "/r", "SEQ010/SH0010", asm)
    loaded = E.load_assembly(s, "/r", "SEQ010/SH0010")
    assert E.frame_range(loaded) == (1001, 1175)


def test_resolve_element_mapping_per_step():
    e = E.new_element("characters/frankenstein")
    for step in ("layout", "animation"):
        spec = E.resolve_element(e, step)
        assert spec["source_step"] == "rig" and spec["fallback_step"] == "model"
        assert spec["load"] == "link" and spec["apply_look"] is False
    light = E.resolve_element(e, "lighting")
    assert light["load"] == "alembic" and light["apply_look"] is True
    assert E.resolve_element(e, "comp") is None


def test_representations_override():
    settings = {"assembly": {"representations": {
        "layout": {"source_step": "rig", "fallback_step": "model",
                   "load": "append", "apply_look": False}}}}
    assert E.resolve_element({}, "layout", settings)["load"] == "append"
    # untouched steps keep their defaults
    assert E.resolve_element({}, "lighting", settings)["load"] == "alembic"


# ---- server I/O (FakeSrv) --------------------------------------------------

def test_save_load_roundtrip():
    s = FakeSrv()
    asm = E.empty_assembly("SEQ010/SH0010")
    E.add_element(asm, E.new_element("characters/frankenstein", look="default"))
    E.add_element(asm, E.new_element("", "camera"))
    E.save_assembly(s, "/r", "SEQ010/SH0010", asm, actor="marco")
    assert "/r/04_sequences/SEQ010/SH0010/assembly.json" in s.files
    loaded = E.load_assembly(s, "/r", "SEQ010/SH0010")
    assert [e["id"] for e in loaded["elements"]] == ["frankenstein", "camera"]
    assert loaded["updated_by"] == "marco"
    assert loaded["elements"][0]["look"] == "default"


def test_load_missing_returns_empty():
    s = FakeSrv()
    asm = E.load_assembly(s, "/r", "SEQ010/SH0010")
    assert asm == E.empty_assembly("SEQ010/SH0010")
    assert asm["elements"] == []


# ---- resolved_elements (FakeSrv + tasks.publish_task) ----------------------

def test_resolved_elements_rig_with_model_fallback():
    s = FakeSrv()
    ent = "characters/frankenstein"
    tasks.save_task(s, "/r", tasks.new_task("asset", ent, "model"))
    tasks.save_task(s, "/r", tasks.new_task("asset", ent, "rig"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"],
                       tasks.make_id("asset", ent, "model"))
    asm = E.empty_assembly("SEQ010/SH0010")
    E.add_element(asm, E.new_element(ent))
    E.add_element(asm, E.new_element("props/lantern"))            # nothing published
    dis = E.new_element("characters/ghost")
    dis["enabled"] = False
    asm["elements"].append(dis)
    E.save_assembly(s, "/r", "SEQ010/SH0010", asm)

    res = E.resolved_elements(s, "/r", "SEQ010/SH0010", "layout")
    # only frankenstein resolves (via model fallback); lantern + disabled skipped
    assert [r["id"] for r in res] == ["frankenstein"]
    assert res[0]["source_step"] == "model"
    assert res[0]["blend_rel"].endswith("frankenstein_model_v001.blend")

    # publish a rig -> now it resolves via rig (preferred over model)
    tasks.publish_task(s, "/r", "marco", ["/tmp/frankenstein_rig_v001.blend"],
                       tasks.make_id("asset", ent, "rig"))
    res2 = E.resolved_elements(s, "/r", "SEQ010/SH0010", "layout")
    assert res2[0]["source_step"] == "rig"
    assert res2[0]["blend_rel"].endswith("frankenstein_rig_v001.blend")


def test_resolved_elements_available_steps_and_pick():
    s = FakeSrv()
    ent = "characters/frankenstein"
    tasks.save_task(s, "/r", tasks.new_task("asset", ent, "model"))
    tasks.save_task(s, "/r", tasks.new_task("asset", ent, "rig"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"],
                       tasks.make_id("asset", ent, "model"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/frankenstein_rig_v001.blend"],
                       tasks.make_id("asset", ent, "rig"))
    asm = E.empty_assembly("SEQ010/SH0010")
    E.add_element(asm, E.new_element(ent))
    E.save_assembly(s, "/r", "SEQ010/SH0010", asm)

    res = E.resolved_elements(s, "/r", "SEQ010/SH0010", "layout")
    r = res[0]
    assert r["available_steps"] == ["rig", "model"]   # preference order
    assert r["source_step"] == "rig"                  # default = first
    # explicit pick of 'model' overrides the default
    res2 = E.resolved_elements(s, "/r", "SEQ010/SH0010", "layout",
                               picks={"frankenstein": "model"})
    assert res2[0]["source_step"] == "model"
    assert res2[0]["blend_rel"].endswith("frankenstein_model_v001.blend")
    # a pick with no publish is ignored -> falls back to the default
    res3 = E.resolved_elements(s, "/r", "SEQ010/SH0010", "layout",
                               picks={"frankenstein": "surface"})
    assert res3[0]["source_step"] == "rig"


def test_resolved_elements_camera_appends_published():
    s = FakeSrv()
    shot = "SEQ010/SH0010"
    tasks.save_task(s, "/r", tasks.new_task("shot", shot, "layout"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/SH0010_layout_v001.blend"],
                       tasks.make_id("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element("", "camera"))
    E.save_assembly(s, "/r", shot, asm)
    res = E.resolved_elements(s, "/r", shot, "layout")
    assert len(res) == 1 and res[0]["kind"] == "camera"
    assert res[0]["source_step"] == "layout" and res[0]["load"] == "append"
    assert res[0]["blend_rel"].endswith("SH0010_layout_v001.blend")
    assert res[0]["camera_name"] == "SEQ010_SH0010"


def test_resolved_elements_camera_builds_rig_when_unpublished():
    s = FakeSrv()
    shot = "SEQ010/SH0010"
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element("", "camera"))
    E.save_assembly(s, "/r", shot, asm)
    res = E.resolved_elements(s, "/r", shot, "layout")
    # no published camera -> emit a build-a-Dolly-rig element, named after the shot
    assert len(res) == 1 and res[0]["load"] == "create_rig"
    assert res[0]["blend_rel"] == "" and res[0]["camera_name"] == "SEQ010_SH0010"
