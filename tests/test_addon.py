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
                                      AddonPreferences=object)
    def _prop(*a, **k):
        return None
    bpy.props = types.SimpleNamespace(
        BoolProperty=_prop, StringProperty=_prop, IntProperty=_prop, FloatProperty=_prop)
    bpy.utils = types.SimpleNamespace(
        user_resource=lambda *a, **k: "/tmp/legami_modules")
    bpy.context = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace(scenes=[])
    sys.modules["bpy"] = bpy


_install_fake_bpy()

from legami_pipeline import settings_io, operators  # noqa: E402


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
    assert scene.render.engine == "CYCLES"
    assert scene.render.fps == 24
    assert scene.render.resolution_x == 1920
    assert scene.view_settings.view_transform == "ACES 1.0 - SDR Video"
    assert scene.display_settings.display_device == "sRGB - Display"
    assert scene.unit_settings.system == "METRIC"
    assert scene.frame_end == 250
    assert scene.cycles.samples == 256
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
