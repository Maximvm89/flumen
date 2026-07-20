"""Tests for the Blender addon that don't need a running Blender.

A minimal fake `bpy` is injected so the addon modules import, then apply_settings
is exercised against a fake scene to confirm the JSON->scene mapping is correct.
"""

import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "blender_addon"))


# --- inject a fake bpy so addon modules import outside Blender ---------------
def _install_fake_bpy():
    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Operator=object, Panel=object,
                                      AddonPreferences=object, Menu=object,
                                      PropertyGroup=object)
    def _prop(*a, **k):
        return None
    bpy.props = types.SimpleNamespace(
        BoolProperty=_prop, StringProperty=_prop, IntProperty=_prop,
        FloatProperty=_prop, EnumProperty=_prop, CollectionProperty=_prop,
        PointerProperty=_prop)
    bpy.utils = types.SimpleNamespace(
        user_resource=lambda *a, **k: "/tmp/flumen_modules")
    bpy.context = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace(scenes=[])
    sys.modules["bpy"] = bpy


_install_fake_bpy()

from flumen_pipeline import settings_io, operators  # noqa: E402


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _fake_scene():
    return _ns(
        display_settings=_ns(display_device=None),
        view_settings=_ns(view_transform=None, look=None, exposure=None, gamma=None),
        sequencer_colorspace_settings=_ns(name=None),
        render=_ns(engine=None, film_transparent=None, resolution_x=None,
                   resolution_y=None, resolution_percentage=None, fps=None,
                   fps_base=None, filepath=None,
                   image_settings=_ns(file_format=None, color_depth=None, exr_codec=None)),
        cycles=_ns(device=None, samples=None, use_denoising=None),
        eevee=_ns(taa_render_samples=None, use_raytracing=None),
        unit_settings=_ns(system=None, scale_length=None, length_unit=None),
        frame_start=None, frame_end=None,
    )


SAMPLE = json.loads((ROOT / "pipeline_config" / "project_settings.json").read_text())


def test_settings_loader(tmp_path):
    # Build a fake project root with the settings file in place.
    d = tmp_path / "02_pipeline"
    d.mkdir(parents=True)
    (d / "project_settings.json").write_text(json.dumps(SAMPLE))
    data = settings_io.load_settings(str(tmp_path))
    assert settings_io.get(data, "render.fps") == 24
    assert settings_io.get(data, "color_management.working_space") == "ACEScg"
    assert settings_io.get(data, "missing.key", "x") == "x"


def test_apply_settings_maps_all_fields():
    scene = _fake_scene()
    warnings = []
    operators.apply_settings(scene, SAMPLE, "/proj/LEGAMI", warnings)

    assert warnings == [], warnings  # nothing should fail against a fake scene
    assert scene.render.engine == "BLENDER_EEVEE"   # project finals: EEVEE
    assert scene.render.fps == 24
    assert scene.render.resolution_x == 1920
    assert scene.view_settings.view_transform == "ACES 1.0 - SDR Video"
    assert scene.display_settings.display_device == "sRGB - Display"
    assert scene.unit_settings.system == "METRIC"
    assert scene.frame_end == 250
    assert scene.eevee.taa_render_samples == 64      # EEVEE finals settings
    assert scene.eevee.use_raytracing is True
    assert scene.render.image_settings.file_format == "OPEN_EXR_MULTILAYER"
    # output path joined under the project root + the rel path
    assert scene.render.filepath.startswith(os.path.join("/proj/LEGAMI", "06_renders"))


def test_apply_skips_cycles_when_engine_not_cycles():
    scene = _fake_scene()
    data = json.loads(json.dumps(SAMPLE))
    data["render"]["engine"] = "BLENDER_EEVEE_NEXT"
    warnings = []
    operators.apply_settings(scene, data, "/proj/LEGAMI", warnings)
    assert scene.cycles.samples is None  # cycles block skipped
    assert scene.render.engine == "BLENDER_EEVEE_NEXT"


def test_parse_progress_matches_toolkit_format():
    # The add-on parser must agree with flumen.progress (separate Pythons).
    from flumen import progress as P
    line = P.format_line(50, 100, 5, "uploading panda_model_v001.blend")
    assert operators._parse_progress(line) == (50, 5.0, "uploading panda_model_v001.blend")
    assert operators._parse_progress("not a progress line") is None
    # blank eta early on
    pct, eta, _ = operators._parse_progress(P.format_line(0, 100, 0, "x"))
    assert pct == 0 and eta is None


def test_human_eta_formatting():
    assert operators._human_eta(None) == ""
    assert operators._human_eta(8) == "~8s left"
    assert operators._human_eta(125) == "~2m left"


def test_dressing_collect_prop_instances_and_environment():
    from types import SimpleNamespace as NS
    from flumen_pipeline import dressing as D

    class FakeObj:
        def __init__(self, name, props=None, matrix=None):
            self.name = name
            self._props = props or {}
            self.matrix_world = matrix or [[1, 0, 0, 0], [0, 1, 0, 0],
                                           [0, 0, 1, 0], [0, 0, 0, 1]]
        def get(self, key, default=None):
            return self._props.get(key, default)

    objs = [
        FakeObj("prop_root__lantern", {
            "flumen_prop_id": "lantern", "flumen_prop_asset": "props/lantern",
            "flumen_prop_step": "model",
            "flumen_prop_blend_rel": "03_assets/props/lantern/model/publish/l_v002.blend",
            "flumen_prop_collection": "lantern"},
            [[1, 0, 0, 2.5], [0, 1, 0, -1], [0, 0, 1, 0], [0, 0, 0, 1]]),
        FakeObj("some_mesh"),                       # ignored: not a prop root
        FakeObj("prop_root__crate", {}),            # minimal: id from the name
    ]
    props = D.collect_prop_instances(objs)
    assert [p["id"] for p in props] == ["crate", "lantern"]        # sorted
    lantern = props[1]
    assert lantern["asset"] == "props/lantern"
    assert lantern["object"] == "prop_root__lantern"
    assert lantern["matrix_world"][0][3] == 2.5
    assert props[0]["source_step"] == "model"                       # default

    colls = [FakeObj("element__x"), FakeObj("environment__market_square", {
        "flumen_env_asset": "environments/market_square",
        "flumen_env_blend_rel": "03_assets/environments/market_square/model/publish/m_v004.blend"})]
    env = D.collect_environment(colls)
    assert env["asset"] == "environments/market_square"
    assert env["source_step"] == "model"
    assert D.collect_environment([FakeObj("element__x")]) is None


def test_dressing_unmanaged_holders_and_ids_and_rel():
    from flumen_pipeline import dressing as D

    class N:
        def __init__(self, name):
            self.name = name
        def get(self, k, d=None):
            return d

    colls = [N("prop__lantern"), N("prop__crate"), N("environment__m")]
    objs = [N("prop_root__lantern")]
    assert D.unmanaged_prop_holders(colls, objs) == ["prop__crate"]

    assert D.prop_id_for("Lantern", set()) == "lantern"
    assert D.prop_id_for("lantern", {"lantern"}) == "lantern_2"
    assert D.prop_id_for("lantern", {"lantern", "lantern_2"}) == "lantern_3"

    assert D.rel_from_local("E:\\Legami_4\\03_assets\\props\\l.blend",
                            "E:/Legami_4") == "03_assets/props/l.blend"
    assert D.rel_from_local("/mnt/other/x.blend", "/home/me/proj") == ""


def test_dressing_naming_parity_with_toolkit():
    # The addon slug must agree with flumen.dressing (separate Pythons at runtime).
    from flumen_pipeline import dressing as AD
    from flumen import dressing as TD
    for raw in ("Night Market!", "", "  a--b  "):
        assert AD.normalize_dressing_name(raw) == TD.normalize_dressing_name(raw)


def test_dressing_collect_local_extras():
    from types import SimpleNamespace as NS
    from flumen_pipeline import dressing as D

    def obj(name, otype="MESH", lib=None, override=None, data_lib=None):
        return NS(name=name, type=otype, library=lib, override_library=override,
                  data=NS(library=data_lib))

    local_mesh = obj("crate_quick")                       # modeled in-scene
    local_curve = obj("cable", otype="CURVE")
    linked_env = obj("wall", lib=object())                # linked environment
    override_prop = obj("lantern", override=object())     # overridden prop
    linked_data = obj("glass", data_lib=object())         # override w/ linked mesh
    prop_root = obj("prop_root__lantern", otype="EMPTY")
    light = obj("key_light", otype="LIGHT")
    cam = obj("REVIEW_Camera", otype="CAMERA")

    out = D.collect_local_extras([local_mesh, local_curve, linked_env,
                                  override_prop, linked_data, prop_root,
                                  light, cam])
    assert [o.name for o in out] == ["cable", "crate_quick"]
