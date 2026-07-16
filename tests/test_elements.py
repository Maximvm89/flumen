"""Tests for flumen.elements — the shot assembly / breakdown model (no network)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import elements as E, tasks
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


def test_resolved_elements_camera_always_builds_rig():
    # The camera is always a fresh Dolly rig (named after the shot); its animation
    # comes back via the published anim Actions, not by appending a shot publish.
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
    assert res[0]["load"] == "create_rig" and res[0]["blend_rel"] == ""
    assert res[0]["camera_name"] == "SEQ010_SH0010"


def test_resolved_animation_per_element_newest():
    s = FakeSrv()
    shot = "SEQ010/SH0010"
    lt = tasks.make_id("shot", shot, "layout")
    tasks.save_task(s, "/r", tasks.new_task("shot", shot, "layout"))
    pub = "04_sequences/SEQ010/SH0010/layout/publish"
    # v001 published BOTH camera + frankenstein; v002 re-published ONLY the camera
    # (frankenstein unchanged). Each element must resolve to its newest version.
    manifests = {
        1: ('{"version":1,"elements":{"camera":{"SEQ010_SH0010":"CamA"},'
            '"frankenstein":{"frank_rig":"FrankA"}}}'),
        2: '{"version":2,"elements":{"camera":{"SEQ010_SH0010":"CamB"}}}',
    }
    for v in (1, 2):
        t = tasks.get_task(s, "/r", lt)
        man = pub + f"/anim/SH0010_layout_v{v:03d}_anim.manifest.json"
        t["publishes"] = (t.get("publishes") or []) + [{
            "files": [pub + f"/SH0010_layout_v{v:03d}.blend",
                      pub + f"/anim/SH0010_layout_v{v:03d}_anim.blend", man],
            "time": v, "by": "marco"}]
        tasks.save_task(s, "/r", t)
        s.files["/r/" + man] = manifests[v]

    ra = E.resolved_animation(s, "/r", shot, "layout")
    # camera from v002 (newest), frankenstein from v001 (its newest containing it)
    assert ra["elements"]["camera"]["objects"] == {"SEQ010_SH0010": "CamB"}
    assert ra["elements"]["camera"]["blend_rel"].endswith("v002_anim.blend")
    assert ra["elements"]["frankenstein"]["objects"] == {"frank_rig": "FrankA"}
    assert ra["elements"]["frankenstein"]["blend_rel"].endswith("v001_anim.blend")


def test_latest_anim_hashes_newest_per_element():
    anims = [
        {"version": "v002", "hashes": {"camera": "c2"}},                 # newest
        {"version": "v001", "hashes": {"camera": "c1", "frankenstein": "f1"}},
    ]
    assert E.latest_anim_hashes(anims) == {"camera": "c2", "frankenstein": "f1"}


def test_resolved_animation_none_when_unpublished():
    s = FakeSrv()
    tasks.save_task(s, "/r", tasks.new_task("shot", "SEQ010/SH0010", "layout"))
    assert E.resolved_animation(s, "/r", "SEQ010/SH0010", "layout") is None


def test_published_animations_lists_all_versions_with_elements():
    s = FakeSrv()
    shot = "SEQ010/SH0010"
    lt = tasks.make_id("shot", shot, "layout")
    tasks.save_task(s, "/r", tasks.new_task("shot", shot, "layout"))
    pub = "04_sequences/SEQ010/SH0010/layout/publish/anim"
    # two anim publishes: v005 has only the camera, v006 has camera + frankenstein
    specs = {
        5: '{"version":5,"elements":{"camera":{"SEQ010_SH0010":"CamA"}}}',
        6: ('{"version":6,"elements":{"camera":{"SEQ010_SH0010":"CamB"},'
            '"frankenstein":{"frank_rig":"FrankA"}}}'),
    }
    for v, man in specs.items():
        t = tasks.get_task(s, "/r", lt)
        t["publishes"] = (t.get("publishes") or []) + [{
            "files": [pub + f"/SH0010_layout_v{v:03d}_anim.blend",
                      pub + f"/SH0010_layout_v{v:03d}_anim.manifest.json"],
            "time": v, "by": "marco", "description": f"take {v}"}]
        tasks.save_task(s, "/r", t)
        s.files["/r/" + pub + f"/SH0010_layout_v{v:03d}_anim.manifest.json"] = man

    anims = E.published_animations(s, "/r", shot, "layout")
    assert [a["version"] for a in anims] == ["v006", "v005"]      # newest first
    assert anims[0]["elements"]["frankenstein"] == {"frank_rig": "FrankA"}
    assert anims[1]["elements"] == {"camera": {"SEQ010_SH0010": "CamA"}}
    assert anims[0]["by"] == "marco" and anims[0]["description"] == "take 6"


def test_anim_version_label():
    assert E.anim_version_label("SH0010_layout_v007_anim.blend") == "v007"


def test_element_dressing_field_round_trip():
    e = E.new_element("environments/market_square", "asset",
                      dressing="night_market")
    assert e["dressing"] == "night_market"
    # old assemblies without the field normalize cleanly to ""
    asm = {"shot": "SEQ010/SH0010",
           "elements": [{"id": "env", "kind": "asset",
                         "asset": "environments/market_square"},
                        {"id": "cam", "kind": "camera",
                         "dressing": "bogus"}]}   # cameras never carry a dressing
    out = E.normalize(asm)
    env = next(x for x in out["elements"] if x["id"] == "env")
    cam = next(x for x in out["elements"] if x["id"] == "cam")
    assert env["dressing"] == "" and cam["dressing"] == ""


def test_newest_dressing_resolves_named_version():
    from test_tasks import FakeSrv
    from flumen import tasks
    srv = FakeSrv()
    dt = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "environments/market_square",
                                        "dressing"))
    pub = "03_assets/environments/market_square/dressing/publish/"
    dt["publishes"] = [
        {"files": [pub + "market_square_dressing_night_market_v001.blend"], "time": 1},
        {"files": [pub + "market_square_dressing_night_market_v002.blend"], "time": 2},
        {"files": [pub + "market_square_dressing_day_v001.blend"], "time": 3},
    ]
    tasks.save_task(srv, "/r", dt)
    d = E.newest_dressing(srv, "/r", "environments/market_square", "night_market")
    assert d["version"] == 2 and d["blend_rel"].endswith("_v002.blend")
    assert d["manifest_rel"].endswith("_v002.manifest.json")
    assert E.newest_dressing(srv, "/r", "environments/market_square", "nope") is None
    assert E.newest_dressing(srv, "/r", "environments/other", "day") is None


# ---- anim source chain (animation step inherits layout) ---------------------

def _publish_anim(s, shot, step, version, manifest_json):
    """Register one anim publish (blend + manifest) on a shot step's task."""
    tid = tasks.make_id("shot", shot, step)
    if not tasks.get_task(s, "/r", tid):
        tasks.save_task(s, "/r", tasks.new_task("shot", shot, step))
    t = tasks.get_task(s, "/r", tid)
    pub = f"04_sequences/{shot}/{step}/publish/anim"
    stem = f"{shot.split('/')[-1]}_{step}_v{version:03d}_anim"
    man = pub + f"/{stem}.manifest.json"
    t["publishes"] = (t.get("publishes") or []) + [{
        "files": [pub + f"/{stem}.blend", man],
        "time": version, "by": "marco"}]
    tasks.save_task(s, "/r", t)
    s.files["/r/" + man] = manifest_json


def test_anim_sources_default_chain_and_override():
    assert E.anim_sources("layout") == ["layout"]
    assert E.anim_sources("animation") == ["animation", "layout"]
    assert E.anim_sources("lighting") == ["lighting", "animation", "layout"]
    # project override; the step's own publishes always stay first
    cfg = {"assembly": {"anim_sources": {"animation": ["layout", "previz"]}}}
    assert E.anim_sources("animation", cfg) == ["animation", "layout", "previz"]


def test_resolved_animation_falls_back_to_layout():
    # THE animation-step workflow: a fresh animation task has no publishes of
    # its own — Build shot must inherit the layout's camera move + placements.
    s = FakeSrv()
    shot = "SEQ010/SH0010"
    tasks.save_task(s, "/r", tasks.new_task("shot", shot, "animation"))
    _publish_anim(s, shot, "layout", 7,
                  '{"version":7,"elements":{"camera":{"SEQ010_SH0010":"CamL"},'
                  '"skeleton":{"skel_rig":"SkelL"}}}')
    ra = E.resolved_animation(s, "/r", shot, "animation")
    assert ra["elements"]["camera"]["objects"] == {"SEQ010_SH0010": "CamL"}
    assert ra["elements"]["camera"]["version"] == "layout v007"
    assert ra["elements"]["skeleton"]["blend_rel"].endswith(
        "layout/publish/anim/SH0010_layout_v007_anim.blend")


def test_resolved_animation_own_step_wins_over_layout():
    # Once the animator publishes, their camera beats layout's — but elements
    # they never touched still resolve from layout.
    s = FakeSrv()
    shot = "SEQ010/SH0010"
    _publish_anim(s, shot, "layout", 7,
                  '{"version":7,"elements":{"camera":{"SEQ010_SH0010":"CamL"},'
                  '"skeleton":{"skel_rig":"SkelL"}}}')
    _publish_anim(s, shot, "animation", 1,
                  '{"version":1,"elements":{"camera":{"SEQ010_SH0010":"CamA"}}}')
    ra = E.resolved_animation(s, "/r", shot, "animation")
    assert ra["elements"]["camera"]["objects"] == {"SEQ010_SH0010": "CamA"}
    assert ra["elements"]["camera"]["version"] == "v001"
    assert ra["elements"]["skeleton"]["objects"] == {"skel_rig": "SkelL"}
    assert ra["elements"]["skeleton"]["version"] == "layout v007"


def test_published_animations_chain_order_and_labels():
    s = FakeSrv()
    shot = "SEQ010/SH0010"
    _publish_anim(s, shot, "layout", 1, '{"version":1,"elements":{}}')
    _publish_anim(s, shot, "layout", 2, '{"version":2,"elements":{}}')
    _publish_anim(s, shot, "animation", 1, '{"version":1,"elements":{}}')
    anims = E.published_animations(s, "/r", shot, "animation")
    # own step first (newest first), then upstream; labels unique across steps
    assert [a["version"] for a in anims] == ["v001", "layout v002", "layout v001"]
    assert [a["step"] for a in anims] == ["animation", "layout", "layout"]
    # the layout step itself is unaffected: no chain, plain labels
    lay = E.published_animations(s, "/r", shot, "layout")
    assert [a["version"] for a in lay] == ["v002", "v001"]


def test_latest_anim_hashes_prefers_own_step():
    # Dedup baseline for the publish dialog: an untouched element hashes against
    # layout, an already-republished one against the animation step's newest.
    anims = [
        {"version": "v001", "step": "animation", "hashes": {"camera": "cA"}},
        {"version": "layout v007", "step": "layout",
         "hashes": {"camera": "cL", "skeleton": "sL"}},
    ]
    assert E.latest_anim_hashes(anims) == {"camera": "cA", "skeleton": "sL"}
