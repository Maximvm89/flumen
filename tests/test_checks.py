"""Tests for the addon's pre-publish checks (no bpy needed — pure attr reads)."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blender_addon"))

from flumen_pipeline import checks


def _scene(system="METRIC", scale=1.0):
    return types.SimpleNamespace(
        unit_settings=types.SimpleNamespace(system=system, scale_length=scale))


def _mesh(name, scale=(1.0, 1.0, 1.0), parent=None):
    return types.SimpleNamespace(type="MESH", name=name, scale=scale, parent=parent)


def _empty(name, parent=None):
    return types.SimpleNamespace(type="EMPTY", name=name, scale=(1.0, 1.0, 1.0),
                                 parent=parent)


def _rig():
    """A valid scene: a PUBLISH locator with a mesh parented under it."""
    loc = _empty("PUBLISH")
    geo = _mesh("Body", parent=loc)
    return loc, geo


def test_model_clean_passes():
    loc, geo = _rig()
    issues = checks.run_checks("model", _scene(), [loc, geo])
    assert issues == []
    assert not checks.has_errors(issues)


def test_model_wrong_units_errors():
    loc, geo = _rig()
    issues = checks.run_checks("model", _scene(system="IMPERIAL"), [loc, geo])
    assert checks.has_errors(issues)


def test_model_scale_not_one_warns():
    loc = _empty("PUBLISH")
    geo = _mesh("Body", scale=(2.0, 2.0, 2.0), parent=loc)
    issues = checks.run_checks("model", _scene(), [loc, geo])
    assert not checks.has_errors(issues)             # warning, not error
    assert any(lvl == checks.WARNING for lvl, _ in issues)


def test_model_no_mesh_errors():
    issues = checks.run_checks("model", _scene(), [])
    assert checks.has_errors(issues)


def test_unit_scale_off_errors():
    loc, geo = _rig()
    issues = checks.run_checks("model", _scene(scale=0.01), [loc, geo])
    assert checks.has_errors(issues)


def test_missing_locator_errors():
    issues = checks.check_publish_locator([_mesh("Cube")], "PUBLISH")
    assert checks.has_errors(issues)


def test_empty_locator_errors():
    loc = _empty("PUBLISH")
    issues = checks.check_publish_locator([loc], "PUBLISH")  # nothing parented
    assert checks.has_errors(issues)


def test_populated_locator_ok():
    loc = _empty("PUBLISH")
    geo = _mesh("Body", parent=loc)
    issues = checks.check_publish_locator([loc, geo], "PUBLISH")
    assert issues == []


def _pub_coll(*objs, name="PUBLISH"):
    return types.SimpleNamespace(name=name, all_objects=list(objs))


def test_publish_collection_ok():
    # Environments: a PUBLISH collection holding the set's collections works
    # exactly like the empty — no locator object anywhere.
    coll = _pub_coll(_mesh("Wall"), _mesh("Floor"))
    issues = checks.check_publish_locator([_mesh("Wall"), _mesh("Floor")],
                                          "PUBLISH", collections=[coll])
    assert issues == []


def test_publish_collection_empty_errors():
    issues = checks.check_publish_locator([], "PUBLISH",
                                          collections=[_pub_coll()])
    assert checks.has_errors(issues)
    assert "collection is empty" in issues[0][1]


def test_neither_root_mentions_both_forms():
    issues = checks.check_publish_locator([_mesh("Cube")], "PUBLISH",
                                          collections=[_pub_coll(name="props")])
    assert checks.has_errors(issues)
    assert "locator" in issues[0][1].lower() or "empty" in issues[0][1].lower()
    assert "collection" in issues[0][1]


def test_locator_empty_wins_over_collection():
    # Back-compat: when both exist, the empty is the root — an unpopulated
    # empty is an error even if a populated PUBLISH collection also exists.
    loc = _empty("PUBLISH")
    coll = _pub_coll(_mesh("Wall"))
    issues = checks.check_publish_locator([loc], "PUBLISH", collections=[coll])
    assert checks.has_errors(issues)


def test_run_checks_model_accepts_collection_root():
    m = _mesh("Wall")
    issues = checks.run_checks("model", _scene(), [m],
                               collections=[_pub_coll(m)])
    assert not checks.has_errors(issues)


def test_run_checks_blocks_without_locator():
    # a clean model but NO locator must now fail
    issues = checks.run_checks("model", _scene(), [_mesh("Body")])
    assert checks.has_errors(issues)


# --- surface (shading/texturing) checks -------------------------------------

def _shaded_mesh(name, parent=None, material=True):
    slots = [types.SimpleNamespace(material=object() if material else None)]
    return types.SimpleNamespace(type="MESH", name=name, scale=(1.0, 1.0, 1.0),
                                 parent=parent, material_slots=slots)


def _tex(name, missing):
    return types.SimpleNamespace(name=name, is_missing=missing)


def test_surface_clean_passes():
    loc = _empty("PUBLISH")
    geo = _shaded_mesh("Body", parent=loc)
    issues = checks.run_checks("surface", _scene(), [loc, geo],
                               textures=[_tex("diffuse", False)])
    assert issues == []
    assert not checks.has_errors(issues)


def test_surface_missing_texture_errors():
    loc = _empty("PUBLISH")
    geo = _shaded_mesh("Body", parent=loc)
    issues = checks.run_checks("surface", _scene(), [loc, geo],
                               textures=[_tex("diffuse", False),
                                         _tex("normal", True)])
    assert checks.has_errors(issues)
    assert any("normal" in m for lvl, m in issues if lvl == checks.ERROR)


def test_surface_mesh_without_material_errors():
    loc = _empty("PUBLISH")
    geo = _shaded_mesh("Body", parent=loc, material=False)
    issues = checks.run_checks("surface", _scene(), [loc, geo], textures=[])
    assert checks.has_errors(issues)
    assert any("material" in m for lvl, m in issues if lvl == checks.ERROR)


def _shot_scene(camera=True, frame_start=1001, frame_end=1100, system="METRIC"):
    s = _scene(system=system)
    s.camera = object() if camera else None
    s.frame_start = frame_start
    s.frame_end = frame_end
    return s


def test_check_shot_ok():
    issues = checks.check_shot(_shot_scene(), [])
    assert issues == []                                  # camera + 1001-1100 range


def test_check_shot_requires_camera():
    issues = checks.check_shot(_shot_scene(camera=False), [])
    assert checks.has_errors(issues)
    assert any("camera" in m.lower() for _, m in issues)


def test_check_shot_empty_range_errors():
    issues = checks.check_shot(_shot_scene(frame_start=1001, frame_end=1001), [])
    assert checks.has_errors(issues)


def test_check_shot_wrong_start_warns():
    issues = checks.check_shot(_shot_scene(frame_start=1, frame_end=100), [])
    assert not checks.has_errors(issues)                 # warning, not error
    assert any(lvl == checks.WARNING and "1001" in m for lvl, m in issues)


def test_run_checks_shot_skips_publish_locator():
    # a shot has no PUBLISH locator; the shot gate must not demand one
    issues = checks.run_checks("layout", _shot_scene(), [], ttype="shot")
    assert not checks.has_errors(issues)


# ---- profiler (WARN-only) ----------------------------------------------------

def _ptex(name="t", w=1024, h=1024, channels=4, is_float=False):
    return {"name": name, "width": w, "height": h,
            "channels": channels, "is_float": is_float}


def _stats(**kw):
    base = {"poly_count": 1000, "object_count": 10,
            "textures": [_ptex()], "heavy_modifiers": []}
    base.update(kw)
    return base


def test_profile_thresholds_defaults_and_override():
    t = checks.profile_thresholds(None)
    assert t["max_polycount"] == 2_000_000
    t = checks.profile_thresholds({"profiling": {"max_polycount": 5}})
    assert t["max_polycount"] == 5
    assert t["max_objects"] == 1500                    # untouched default


def test_check_profile_under_budget_is_clean():
    assert checks.check_profile(_stats(), checks.DEFAULT_PROFILING) == []
    assert checks.check_profile(None, checks.DEFAULT_PROFILING) == []


def test_check_profile_each_threshold_warns_never_errors():
    t = dict(checks.DEFAULT_PROFILING, max_polycount=100, max_objects=5,
             max_textures=0, max_texture_size=512, max_texture_memory_mb=1)
    issues = checks.check_profile(_stats(
        poly_count=200, object_count=6,
        textures=[_ptex(w=1024, h=1024)],
        heavy_modifiers=[("wall", "SUBSURF")]), t)
    assert issues and all(lvl == checks.WARNING for lvl, _ in issues)
    assert not checks.has_errors(issues)                    # profiler NEVER blocks
    text = " ".join(m for _, m in issues)
    assert "polys" in text and "objects" in text and "1024x1024" in text
    assert "SUBSURF" in text


def test_texture_memory_math_float_vs_8bit():
    # 4K RGBA 8-bit = 4096*4096*4*1 = 64 MB; float32 = x4 = 256 MB
    assert checks.texture_memory_bytes(_ptex(w=4096, h=4096)) == 4096 * 4096 * 4
    assert checks.texture_memory_bytes(_ptex(w=4096, h=4096, is_float=True)) == \
        4096 * 4096 * 4 * 4
    t = dict(checks.DEFAULT_PROFILING, max_texture_memory_mb=100)
    hot = checks.check_profile(_stats(textures=[_ptex(w=4096, h=4096, is_float=True)]), t)
    assert any("texture memory" in m for _, m in hot)
    cold = checks.check_profile(_stats(textures=[_ptex(w=4096, h=4096)]), t)
    assert not any("texture memory" in m for _, m in cold)


def test_texture_size_list_capped_with_summary():
    t = dict(checks.DEFAULT_PROFILING, max_texture_size=512)
    many = [_ptex(name=f"tex{i}", w=1024, h=1024) for i in range(8)]
    issues = checks.check_profile(_stats(textures=many), t)
    per_tex = [m for _, m in issues if "Texture '" in m]
    assert len(per_tex) == 5                            # capped
    assert any("and 3 more" in m for _, m in issues)    # summary line


def test_run_checks_appends_profile_and_missing_key_skips():
    scene = _scene()
    loc, geo = _rig()
    objs = [loc, geo]
    base = checks.run_checks("model", scene, objs, profile_stats=None)
    with_p = checks.run_checks("model", scene, objs,
                          profile_stats=_stats(poly_count=10**9),
                          profiling=checks.DEFAULT_PROFILING)
    assert len(with_p) == len(base) + 1
    assert with_p[-1][0] == checks.WARNING
    # a key omitted from the thresholds skips that check entirely
    no_poly = dict(checks.DEFAULT_PROFILING)
    no_poly.pop("max_polycount")
    assert checks.check_profile(_stats(poly_count=10**9), no_poly) == []


def test_model_unapplied_scale_aggregated():
    loc, geo = _rig()
    bad = [types.SimpleNamespace(type="MESH", name=f"glass_{i}",
                                 scale=(0.001, 0.001, 0.001), parent=loc,
                                 material_slots=[])
           for i in range(8)]
    issues = checks.check_model(_scene(), [loc, geo] + bad)
    scale_warns = [m for lvl, m in issues if "unapplied scale" in m]
    assert len(scale_warns) == 1                     # ONE aggregated line
    msg = scale_warns[0]
    assert "8 mesh(es)" in msg and "glass_0" in msg and "…and 3 more" in msg
    assert not checks.has_errors(issues)             # still just a warning


def test_fixable_scale_objects_triage():
    def mesh(name, scale=(0.5, 0.5, 0.5), users=1, anim=None):
        data = types.SimpleNamespace(users=users)
        return types.SimpleNamespace(type="MESH", name=name, scale=scale,
                                     data=data, animation_data=anim,
                                     material_slots=[], parent=None)
    ok = mesh("plain")
    inst = mesh("glass_instance", users=3)                    # shared mesh data
    keyed = mesh("pulsing_light", anim=object())              # keyframed
    clean = mesh("done", scale=(1.0, 1.0, 1.0))               # nothing to fix
    empty = types.SimpleNamespace(type="EMPTY", name="PUBLISH",
                                  scale=(2.0, 2.0, 2.0), parent=None)  # not a mesh
    lib = mesh("env_prop")
    lib.override_library = object()                           # shot/dressing element
    fixable, shared, animated, linked = checks.fixable_scale_objects(
        [ok, inst, keyed, clean, empty, lib])
    assert [o.name for o in fixable] == ["plain"]
    assert [o.name for o in shared] == ["glass_instance"]
    assert [o.name for o in animated] == ["pulsing_light"]
    assert [o.name for o in linked] == ["env_prop"]
