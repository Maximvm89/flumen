"""Tests for the addon's pre-publish checks (no bpy needed — pure attr reads)."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blender_addon"))

from legami_pipeline import checks


def _scene(system="METRIC", scale=1.0):
    return types.SimpleNamespace(
        unit_settings=types.SimpleNamespace(system=system, scale_length=scale))


def _mesh(name, scale=(1.0, 1.0, 1.0)):
    return types.SimpleNamespace(type="MESH", name=name, scale=scale)


def test_model_clean_passes():
    issues = checks.run_checks("model", _scene(), [_mesh("Body")])
    assert issues == []
    assert not checks.has_errors(issues)


def test_model_wrong_units_errors():
    issues = checks.run_checks("model", _scene(system="IMPERIAL"), [_mesh("Body")])
    assert checks.has_errors(issues)


def test_model_scale_not_one_warns():
    issues = checks.run_checks("model", _scene(), [_mesh("Body", scale=(2.0, 2.0, 2.0))])
    assert not checks.has_errors(issues)             # warning, not error
    assert any(lvl == checks.WARNING for lvl, _ in issues)


def test_model_no_mesh_errors():
    issues = checks.run_checks("model", _scene(), [])
    assert checks.has_errors(issues)


def test_unit_scale_off_errors():
    issues = checks.run_checks("model", _scene(scale=0.01), [_mesh("Body")])
    assert checks.has_errors(issues)
